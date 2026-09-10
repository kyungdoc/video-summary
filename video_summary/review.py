"""Local, source-bound human review; never runs analysis or modifies source media.

The SQLite journal commits a pending config snapshot before replacing YAML. A
restart can finish that write, but will never overwrite an intervening external
edit. Media access is a manifest allowlist, not a filesystem HTTP server.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import mimetypes
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import yaml

from .models import Candidate, Clip
from .project import ProjectPaths, load_config, _validate_config
from .state import project_lock
from .utils import VideoSummaryError, atomic_write_text, file_fingerprint, read_json, stable_hash

MAX_BODY = 64 * 1024
STATIC_ROOT = Path(__file__).with_name("review_static")


class ReviewConflict(VideoSummaryError):
    """An edit cannot safely be applied to the current revision."""


def _digest(path: Path) -> str:
    if not path.exists():
        return "missing"
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(256 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _journal(paths: ProjectPaths) -> sqlite3.Connection:
    connection = sqlite3.connect(paths.root / "review.sqlite3", timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("""CREATE TABLE IF NOT EXISTS revisions (
        revision TEXT PRIMARY KEY, created_at TEXT NOT NULL, action TEXT NOT NULL,
        clip_name TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
        reason TEXT NOT NULL, before_hash TEXT NOT NULL, after_hash TEXT NOT NULL,
        snapshot_yaml TEXT NOT NULL, applied INTEGER NOT NULL DEFAULT 0
    )""")
    connection.commit()
    return connection


def _history(paths: ProjectPaths) -> list[dict[str, Any]]:
    if not (paths.root / "review.sqlite3").exists():
        return []
    with closing(_journal(paths)) as connection:
        rows = connection.execute(
            "SELECT revision,created_at,action,clip_name,start,end,reason,applied "
            "FROM revisions ORDER BY rowid DESC LIMIT 30"
        ).fetchall()
    return [dict(row) for row in rows]


def _recover_pending(paths: ProjectPaths) -> None:
    """Call only while holding the shared pipeline lock."""
    with closing(_journal(paths)) as connection:
        pending = connection.execute("SELECT * FROM revisions WHERE applied=0 ORDER BY rowid").fetchall()
        for row in pending:
            current = _digest(paths.config)
            if current == row["before_hash"]:
                atomic_write_text(paths.config, row["snapshot_yaml"])
            elif current != row["after_hash"]:
                raise ReviewConflict("중단된 검수 저장 이후 설정이 변경되었습니다. review.sqlite3의 대기 버전을 확인하세요.")
            connection.execute("UPDATE revisions SET applied=1 WHERE revision=?", (row["revision"],))
            connection.commit()


def _revision(paths: ProjectPaths) -> str:
    manifest = read_json(paths.manifest) if paths.manifest.exists() else {}
    transcript_hashes = {}
    for value in manifest.get("clips", []):
        clip_id = value["clip_id"]
        if not isinstance(clip_id, str) or Path(clip_id).name != clip_id or clip_id in {".", ".."}:
            raise VideoSummaryError("원본 ID가 잘못되었습니다.")
        transcript_hashes[clip_id] = _digest(paths.transcripts / f"{clip_id}.json")
    return stable_hash({
        "config": _digest(paths.config), "manifest": _digest(paths.manifest),
        "candidates": _digest(paths.candidates), "plan": _digest(paths.plan),
        "transcripts": transcript_hashes,
        "head": (_history(paths) or [{}])[0].get("revision"),
    }, length=32)


def _cues(paths: ProjectPaths, clip: Clip) -> list[dict[str, Any]]:
    path = paths.transcripts / f"{clip.clip_id}.json"
    if not path.exists():
        return []
    payload = read_json(path)
    if payload.get("fingerprint", clip.fingerprint) != clip.fingerprint:
        raise ReviewConflict("전사 캐시의 원본이 변경되었습니다. analyze 후 다시 검수하세요.")
    return payload.get("cues", [])


def _safe_source(manifest: dict[str, Any], clip: Clip) -> Path:
    root = Path(manifest["source_dir"]).resolve()
    relative = Path(clip.relative_path)
    path = Path(clip.path).resolve()
    if relative.is_absolute() or ".." in relative.parts or not path.is_relative_to(root):
        raise VideoSummaryError("원본 허용 경로 밖의 파일은 열 수 없습니다.")
    if path != (root / relative).resolve() or not path.is_file():
        raise VideoSummaryError("원본 파일을 찾을 수 없습니다. scan을 다시 실행하세요.")
    return path


def _check_source(manifest: dict[str, Any], clip: Clip) -> Path:
    path = _safe_source(manifest, clip)
    if file_fingerprint(path) != clip.fingerprint:
        raise ReviewConflict("원본이 scan 이후 변경되었습니다. scan과 analyze를 다시 실행하세요.")
    return path


def _overlap(start: float, end: float, rule: dict[str, Any], duration: float) -> bool:
    rule_end = float(rule["end"]) if rule.get("end") is not None else duration
    return min(end, rule_end) - max(start, float(rule.get("start", 0))) > 0.001


def build_review_catalog(paths: ProjectPaths) -> dict[str, Any]:
    """Read cached artifacts, retaining omitted candidates and unanalysed clips."""
    from .candidates import _candidate_cache_key, _range_rule_matches_clip
    from .editorial_evidence import build_candidate_evidence
    from .media import _scan_config_signature

    before_revision = _revision(paths)
    config = load_config(paths)
    if not paths.manifest.exists():
        raise VideoSummaryError("먼저 scan 또는 run을 실행하세요.")
    manifest = read_json(paths.manifest)
    clips = [Clip.from_dict(value) for value in manifest.get("clips", [])]
    clips_by_id = {clip.clip_id: clip for clip in clips}
    payload = read_json(paths.candidates) if paths.candidates.exists() else {}
    plan = read_json(paths.plan) if paths.plan.exists() else {}
    selections = {
        segment["candidate_id"]: segment
        for episode in plan.get("episodes", []) for segment in episode.get("segments", [])
    }
    candidates = [Candidate.from_dict(value) for value in payload.get("candidates", [])]
    source_fingerprints = payload.get("source_fingerprints", {})
    analysis_current = bool(payload) and payload.get("cache_key") == _candidate_cache_key(paths, clips, config)

    def source_matches(candidate: Candidate) -> bool:
        clip = clips_by_id[candidate.clip_id]
        if candidate.clip_id in source_fingerprints:
            return source_fingerprints[candidate.clip_id] == clip.fingerprint
        if analysis_current:
            return True  # The legacy cache key includes all source fingerprints.
        # Older caches already bind generated IDs to the source bytes and range.
        return candidate.candidate_id == "cand_" + stable_hash({
            "clip_id": clip.clip_id, "fingerprint": clip.fingerprint,
            "start": round(candidate.start, 2), "end": round(candidate.end, 2),
        }, length=18)

    stale_clips = {
        candidate.clip_id for candidate in candidates if candidate.clip_id in clips_by_id and (
            not 0 <= candidate.start < candidate.end <= clips_by_id[candidate.clip_id].duration
            or not source_matches(candidate)
        )
    }
    candidates = [candidate for candidate in candidates if candidate.clip_id not in stale_clips]
    represented = {candidate.clip_id for candidate in candidates}
    for clip in clips:
        if clip.clip_id not in represented:
            candidates.append(Candidate(
                candidate_id=f"source-{clip.clip_id}", clip_id=clip.clip_id,
                day_key=clip.day_key, travel_day=clip.travel_day, start=0.0, end=clip.duration,
                captured_at=clip.captured_at, transcript="", roles=[], score=0.0,
                speech_ratio=0.0, motion_score=0.0, visual_quality=0.0,
                location=clip.location, frame_path="", origin="unanalysed",
            ))
    editing = config["editing"]
    warnings: list[str] = []
    if stale_clips:
        warnings.append("원본이 변경된 후보는 숨기고 원본 전체를 표시합니다. analyze를 다시 실행하세요.")
    if not payload:
        warnings.append("분석 결과가 없어 원본 목록을 표시합니다. analyze 후 이벤트가 구성됩니다.")
    cue_cache = {}
    stale_transcripts: set[str] = set()
    for clip in clips:
        try:
            cue_cache[clip.clip_id] = _cues(paths, clip)
        except ReviewConflict:
            cue_cache[clip.clip_id] = []
            stale_transcripts.add(clip.clip_id)
    if stale_transcripts:
        warnings.append("변경 전 원본의 전사 캐시는 표시하지 않습니다. analyze 후 다시 검수하세요.")
    observations = editing.get("reviewed_evidence", [])
    events: dict[tuple[str, str], dict[str, Any]] = {}
    for candidate in sorted(candidates, key=lambda value: (
        value.day_key, value.sequence_at or value.captured_at, value.start, value.candidate_id
    )):
        clip = clips_by_id.get(candidate.clip_id)
        if clip is None:
            warnings.append("현재 원본 목록에 없는 후보가 있습니다. analyze를 다시 실행하세요.")
            continue
        selected = selections.get(candidate.candidate_id)
        evidence = build_candidate_evidence(candidate, clip, cue_cache[clip.clip_id],
                                            [value for value in observations if value.get("clip_id") == clip.clip_id])
        if clip.clip_id in stale_transcripts:
            evidence["flags"].append("stale_transcript_cache")
            evidence["confirmed_visual_kinds"] = []
            evidence["review_status"] = "needs_review"
        reviewed = {"include": [], "exclude": []}
        for action, key in (("include", "reviewed_include_ranges"), ("exclude", "exclude_ranges")):
            for rule in editing.get(key, []):
                if _range_rule_matches_clip(clip, rule) and _overlap(candidate.start, candidate.end, rule, clip.duration):
                    if rule.get("source_fingerprint", clip.fingerprint) != clip.fingerprint:
                        warnings.append("원본 지문이 변경된 검수 규칙이 있습니다. 다시 검수하세요.")
                    else:
                        reviewed[action].append({key: rule.get(key) for key in ("start", "end", "reason")})
        item = {
            "candidate_id": candidate.candidate_id, "clip_id": clip.clip_id,
            "clip_name": clip.relative_path, "source_fingerprint": clip.fingerprint,
            "source_kind": clip.source_kind, "source_duration": clip.duration,
            "start": candidate.start, "end": candidate.end, "duration": candidate.duration,
            "transcript": candidate.transcript, "roles": candidate.roles,
            "story_stage": candidate.story_stage, "origin": candidate.origin,
            "selected": selected is not None, "speed": selected.get("speed", 1.0) if selected else 1.0,
            "reason": selected.get("reason", "") if selected else "현재 편집안에 포함되지 않음",
            "exclusion_reason": candidate.exclusion_reason,
            "reviewed_inclusion_reason": candidate.reviewed_inclusion_reason,
            "reviewed_ranges": reviewed, "captured_at": clip.captured_at,
            "sequence_at": clip.sequence_at or clip.captured_at,
            "sequence_source": clip.sequence_source, "capture_time_confidence": clip.capture_time_confidence,
            "capture_time_basis": clip.capture_time_basis,
            "media_url": f"/media/{quote(clip.clip_id, safe='')}",
            "frame_url": f"/frame/{quote(candidate.candidate_id, safe='')}" if candidate.frame_path else None,
            "evidence": evidence,
        }
        event_id = candidate.story_event_id or f"source-{clip.clip_id}"
        key = (candidate.day_key, event_id)
        event = events.setdefault(key, {
            "event_id": event_id, "day_key": candidate.day_key,
            "title": candidate.location or event_id, "candidates": [],
        })
        event["candidates"].append(item)
    days: dict[str, dict[str, Any]] = {}
    for (day_key, _), event in events.items():
        days.setdefault(day_key, {"day_key": day_key, "events": []})["events"].append(event)
    pending_scan = manifest.get("scan_config_hash") != _scan_config_signature(config)
    if pending_scan:
        warnings.append("촬영시각·위치 설정이 scan 이후 변경되었습니다. 표시된 시간은 이전 분석 기준입니다. scan을 다시 실행하세요.")
    pending = pending_scan or bool(stale_clips) or bool(stale_transcripts) or not analysis_current
    if before_revision != _revision(paths):
        raise ReviewConflict("검수 목록을 읽는 동안 프로젝트가 변경되었습니다. 새로고침하세요.")
    return {
        "project": config["project"]["name"], "revision": before_revision,
        "pending_scan": pending_scan,
        "pending_reanalysis": pending,
        "plan_stale": pending or not plan or plan.get("candidate_set_hash") != payload.get("candidate_set_hash"),
        "history": _history(paths), "days": list(days.values()),
        "clips": [{"clip_id": clip.clip_id, "clip_name": clip.relative_path,
                   "duration": clip.duration, "source_kind": clip.source_kind} for clip in clips],
        "warnings": list(dict.fromkeys(warnings)),
    }


def apply_review_action(paths: ProjectPaths, action: dict[str, Any]) -> dict[str, Any]:
    """Persist an exact source range and optional human evidence as one revision."""
    from .candidates import _range_rule_matches_clip
    from .editorial_evidence import validate_human_observation

    if not isinstance(action, dict) or set(action) - {
        "expected_revision", "action", "clip_id", "start", "end", "reason", "observation"
    }:
        raise VideoSummaryError("검수 요청 필드가 잘못되었습니다.")
    kind = action.get("action")
    if not isinstance(kind, str) or kind not in {"include", "exclude", "evidence"}:
        raise VideoSummaryError("검수 작업은 include, exclude, evidence 중 하나여야 합니다.")
    with project_lock(paths.root / ".pipeline.lock"):
        _recover_pending(paths)
        if action.get("expected_revision") != _revision(paths):
            raise ReviewConflict("다른 작업에서 프로젝트가 변경되었습니다. 새로고침 후 다시 검수하세요.")
        manifest = read_json(paths.manifest)
        clip = next((Clip.from_dict(value) for value in manifest.get("clips", [])
                     if value["clip_id"] == action.get("clip_id")), None)
        if clip is None:
            raise VideoSummaryError("알 수 없는 원본 ID입니다.")
        _check_source(manifest, clip)
        start, end = action.get("start"), action.get("end")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
               for value in (start, end)) or not 0 <= start < end <= clip.duration or end - start <= 0.001:
            raise VideoSummaryError("검수 범위는 원본 안의 유효한 시작·끝 초여야 합니다.")
        reason = action.get("reason")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 160:
            raise VideoSummaryError("검수 이유는 1~160자여야 합니다.")
        if kind == "include" and clip.capture_time_basis == "unplaced":
            raise ReviewConflict("촬영시각을 배치하지 못한 원본입니다. 먼저 date_overrides로 순서를 확인하세요.")
        if kind == "include" and action.get("observation") is None:
            # A manual include must not cut a known utterance even when the
            # reviewer chooses not to record semantic evidence. This temporary
            # inferred record is validation only, never persisted as a claim.
            validate_human_observation({
                "clip_id": clip.clip_id, "source_fingerprint": clip.fingerprint,
                "kind": "other", "basis": "inferred", "description": reason.strip(),
                "core_range": {"start": start, "end": end},
                "context_range": {"start": start, "end": end}, "source_cue_ids": [],
            }, clip, _cues(paths, clip))
        before_hash = _digest(paths.config)
        config = load_config(paths)
        editing = config["editing"]
        if kind != "evidence":
            other_key = "exclude_ranges" if kind == "include" else "reviewed_include_ranges"
            for rule in editing.get(other_key, []):
                if _range_rule_matches_clip(clip, rule) and _overlap(start, end, rule, clip.duration):
                    raise ReviewConflict("반대 검수 규칙과 겹칩니다. 기존 규칙을 확인해 project.yaml에서 충돌을 먼저 해결하세요.")
            key = "reviewed_include_ranges" if kind == "include" else "exclude_ranges"
            rule = {"match": clip.relative_path, "match_type": "exact", "source_fingerprint": clip.fingerprint,
                    "start": start, "end": end, "reason": reason.strip()}
            if rule in editing.setdefault(key, []):
                raise ReviewConflict("동일한 검수 규칙이 이미 저장되어 있습니다.")
            editing[key].append(rule)
        observation = action.get("observation")
        if kind == "evidence" and observation is None:
            raise VideoSummaryError("관찰 기록이 필요합니다.")
        if observation is not None:
            observation = validate_human_observation(observation, clip, _cues(paths, clip))
            if observation["core_range"] != {"start": float(start), "end": float(end)}:
                raise VideoSummaryError("관찰 핵심 범위와 검수 범위가 일치해야 합니다.")
            editing.setdefault("reviewed_evidence", []).append(observation)
        _validate_config(config)
        snapshot = yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
        if _digest(paths.config) != before_hash or _revision(paths) != action["expected_revision"]:
            raise ReviewConflict("검수 저장 준비 중 프로젝트가 변경되었습니다. 새로고침 후 다시 시도하세요.")
        after_hash = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
        revision = secrets.token_hex(16)
        with closing(_journal(paths)) as connection:
            connection.execute(
                "INSERT INTO revisions VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                (revision, datetime.now(timezone.utc).isoformat(), kind, clip.relative_path,
                 start, end, reason.strip(), before_hash, after_hash, snapshot),
            )
            connection.commit()
            atomic_write_text(paths.config, snapshot)
            connection.execute("UPDATE revisions SET applied=1 WHERE revision=?", (revision,))
            connection.commit()
        return build_review_catalog(paths)


def _byte_range(value: str | None, size: int) -> tuple[int, int, bool]:
    if value is None:
        return 0, size - 1, False
    if not value.startswith("bytes=") or "," in value:
        raise ValueError("Only one byte range is supported")
    left, right = value[6:].split("-", 1)
    if left:
        if not left.isascii() or not left.isdigit() or (right and (not right.isascii() or not right.isdigit())):
            raise ValueError("Invalid range")
        start, end = int(left), min(int(right), size - 1) if right else size - 1
    else:
        if not right.isascii() or not right.isdigit() or int(right) <= 0:
            raise ValueError("Invalid suffix")
        start, end = max(0, size - int(right)), size - 1
    if not 0 <= start <= end < size:
        raise ValueError("Unsatisfiable range")
    return start, end, True


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, paths: ProjectPaths, port: int):
        self.paths = paths
        self.token = secrets.token_urlsafe(32)
        self.csrf_token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), ReviewHandler)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.cookie_name = f"review_session_{self.server_port}"
        self.url = f"{self.origin}/?token={self.token}"


class ReviewHandler(BaseHTTPRequestHandler):
    server: ReviewServer
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format: str, *args: Any) -> None:
        # Source paths, search strings and the bootstrap secret never enter logs.
        pass

    def _headers(self, status: int, content_type: str, length: int, **extra: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for key, value in extra.items():
            self.send_header(key.replace("_", "-"), value)
        self.end_headers()

    def _json(self, status: int, value: Any) -> None:
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body))
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authorized(self) -> bool:
        if self.headers.get("Host") != urlsplit(self.server.origin).netloc:
            self._json(403, {"error": "로컬 검수 주소로 접속하세요."})
            return False
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            token = cookie.get(self.server.cookie_name)
            authenticated = token is not None and hmac.compare_digest(token.value, self.server.token)
        except (ValueError, TypeError, CookieError):
            authenticated = False
        if not authenticated:
            self._json(403, {"error": "터미널에 출력된 검수 링크로 접속하세요."})
        return authenticated

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        try:
            parsed = urlsplit(self.path)
            if parsed.path == "/" and parsed.query:
                token = parse_qs(parsed.query).get("token", [""])[0]
                if self.headers.get("Host") == urlsplit(self.server.origin).netloc and hmac.compare_digest(token, self.server.token):
                    self._headers(303, "text/plain", 0, Location="/",
                                  Set_Cookie=f"{self.server.cookie_name}={self.server.token}; HttpOnly; SameSite=Strict; Path=/")
                    return
            if not self._authorized():
                return
            if parsed.path == "/api/review":
                result = build_review_catalog(self.server.paths)
                result["csrf_token"] = self.server.csrf_token
                self._json(200, result)
            elif parsed.path in {"/", "/static/app.js", "/static/style.css"}:
                name = "index.html" if parsed.path == "/" else Path(parsed.path).name
                path = (STATIC_ROOT / name).resolve()
                if not path.is_relative_to(STATIC_ROOT.resolve()):
                    raise VideoSummaryError("허용되지 않은 파일입니다.")
                self._file(path, ranges=False)
            elif parsed.path.startswith("/media/"):
                clip_id = parsed.path.removeprefix("/media/")
                manifest = read_json(self.server.paths.manifest)
                value = next((item for item in manifest.get("clips", []) if item["clip_id"] == clip_id), None)
                if value is None:
                    self._json(404, {"error": "원본 ID를 찾을 수 없습니다."})
                    return
                self._file(_check_source(manifest, Clip.from_dict(value)), ranges=True)
            elif parsed.path.startswith("/frame/"):
                candidate_id = parsed.path.removeprefix("/frame/")
                payload = read_json(self.server.paths.candidates) if self.server.paths.candidates.exists() else {}
                item = next((item for item in payload.get("candidates", []) if item["candidate_id"] == candidate_id), None)
                path = Path(item.get("frame_path", "")) if item else None
                if path is not None:
                    path = (path if path.is_absolute() else self.server.paths.root / path).resolve()
                if path is None or not path.is_relative_to(self.server.paths.frames.resolve()) or path.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
                    self._json(404, {"error": "캐시 프레임을 찾을 수 없습니다."})
                    return
                self._file(path, ranges=False)
            else:
                self._json(404, {"error": "알 수 없는 경로입니다."})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except ReviewConflict as exc:
            self._json(409, {"error": str(exc)})
        except VideoSummaryError as exc:
            self._json(400, {"error": str(exc)})
        except (OSError, ValueError, KeyError, TypeError):
            self._json(400, {"error": "검수 데이터를 읽을 수 없습니다. 프로젝트 분석 상태를 확인하세요."})

    def _file(self, path: Path, *, ranges: bool) -> None:
        with path.open("rb") as handle:
            size = path.stat().st_size
            try:
                start, end, partial = _byte_range(self.headers.get("Range") if ranges else None, size)
            except ValueError:
                self._headers(416, "text/plain", 0, Content_Range=f"bytes */{size}")
                return
            extra = {"Accept_Ranges": "bytes"} if ranges else {}
            if partial:
                extra["Content_Range"] = f"bytes {start}-{end}/{size}"
            self._headers(206 if partial else 200, mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                          max(0, end - start + 1), **extra)
            if self.command == "HEAD":
                return
            handle.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = handle.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_POST(self) -> None:
        self.close_connection = True
        try:
            if not self._authorized():
                return
            if self.path != "/api/review":
                self._json(404, {"error": "알 수 없는 경로입니다."})
                return
            if self.headers.get("Origin") != self.server.origin or not hmac.compare_digest(
                self.headers.get("X-Review-Token", ""), self.server.csrf_token
            ):
                self._json(403, {"error": "검수 화면에서 다시 요청하세요."})
                return
            length = self.headers.get("Content-Length", "")
            if self.headers.get("Transfer-Encoding") or not length.isdigit() or not 0 < int(length) <= MAX_BODY:
                self._json(413, {"error": "검수 요청 크기가 잘못되었습니다."})
                return
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                self._json(415, {"error": "JSON 요청이 필요합니다."})
                return
            body = self.rfile.read(int(length))
            result = apply_review_action(self.server.paths, json.loads(body))
            result["csrf_token"] = self.server.csrf_token
            self._json(200, result)
        except ReviewConflict as exc:
            self._json(409, {"error": str(exc)})
        except VideoSummaryError as exc:
            self._json(400, {"error": str(exc)})
        except (ValueError, TypeError, KeyError):
            self._json(400, {"error": "검수 요청을 해석할 수 없습니다."})
        except OSError:
            self._json(500, {"error": "검수 저장이 완료되지 않았습니다. 재시작하면 대기 버전을 확인합니다."})


def create_review_server(paths: ProjectPaths, port: int = 0) -> ReviewServer:
    if type(port) is not int or not 0 <= port <= 65535:
        raise VideoSummaryError("검수 포트는 0~65535 정수여야 합니다.")
    if not paths.manifest.exists():
        raise VideoSummaryError("먼저 scan 또는 run을 실행하세요.")
    with project_lock(paths.root / ".pipeline.lock"):
        _recover_pending(paths)
        load_config(paths)
    try:
        return ReviewServer(paths, port)
    except OSError as exc:
        raise VideoSummaryError(
            f"로컬 검수 포트 {port}를 열 수 없습니다. --port 0 또는 다른 포트를 지정하세요."
        ) from exc


def serve_review(paths: ProjectPaths, port: int = 8765) -> dict[str, Any]:
    server = create_review_server(paths, port)
    print(f"로컬 검수: {server.url}", flush=True)
    print("원본과 분석을 자동 변경하지 않습니다. 종료: Ctrl-C", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return {"project": paths.slug, "review": "closed", "port": server.server_port}
