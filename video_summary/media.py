from __future__ import annotations

import fnmatch
import json
import math
import os
import re
import subprocess
import tempfile
from datetime import date, datetime, timedelta, timezone as datetime_timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import Clip
from .project import ProjectPaths
from .state import StateStore
from .utils import VideoSummaryError, file_fingerprint, print_status, read_json, run_command, stable_hash, write_json


MEDIA_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mts", ".m2ts", ".avi", ".mkv"}
_CAPTURE_TIME_POLICY_VERSION = 2
_FILENAME_PATTERNS = (
    re.compile(r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])([0-2]\d|3[01])[_-]?([0-2]\d)([0-5]\d)([0-5]\d)(?!\d)"),
    re.compile(r"(?<!\d)(20\d{2})[-_](0[1-9]|1[0-2])[-_]([0-2]\d|3[01])[ T_-]([0-2]\d)[-_:]?([0-5]\d)[-_:]?([0-5]\d)(?!\d)"),
)


def find_media(source_dir: Path) -> list[Path]:
    if not source_dir.exists() or not source_dir.is_dir():
        raise VideoSummaryError(f"원본 폴더가 없습니다: {source_dir}")
    media = [
        path.absolute()
        for path in source_dir.rglob("*")
        if path.is_file()
        and path.suffix.lower() in MEDIA_EXTENSIONS
        and ".video-summary" not in path.parts
        and "exports" not in path.parts
    ]
    if not media:
        raise VideoSummaryError(f"지원하는 영상 파일을 찾지 못했습니다: {source_dir}")
    return sorted(media, key=lambda path: str(path).casefold())


def scan_project(
    paths: ProjectPaths,
    source_dir: str | Path,
    config: dict[str, Any],
    *,
    force: bool = False,
) -> dict[str, Any]:
    source = Path(source_dir).expanduser().resolve()
    files = find_media(source)
    project_config = config["project"]
    cache_key = stable_hash(
        {
            "version": 2,
            "capture_time_policy": _CAPTURE_TIME_POLICY_VERSION,
            "project": config["project"]["name"],
            "source": str(source),
            "files": [
                (
                    str(path.relative_to(source)),
                    path.stat().st_size,
                    path.stat().st_mtime_ns,
                    file_fingerprint(path),
                )
                for path in files
            ],
            "timezone": project_config["timezone"],
            "day_start_hour": project_config["day_start_hour"],
            "date_overrides": config.get("date_overrides", []),
            "locations": config.get("locations", []),
        }
    )
    state = StateStore(paths.state)
    if not force and paths.manifest.exists() and state.is_complete("scan", cache_key):
        print_status("scan: 캐시 사용")
        return read_json(paths.manifest)

    state.mark_running("scan", cache_key, {"source_dir": str(source), "clip_count": len(files)})
    try:
        timezone = _timezone(str(project_config["timezone"]))
        probed: list[tuple[Path, dict[str, Any], datetime, str, list[str]]] = []
        for index, path in enumerate(files, start=1):
            print_status(f"scan {index}/{len(files)}: {path.name}")
            probe = probe_media(path)
            relative = path.relative_to(source)
            captured, capture_source, warnings = infer_capture_time(
                path,
                relative,
                probe,
                timezone,
                config.get("date_overrides", []),
            )
            probed.append((path, probe, captured, capture_source, warnings))

        probed.sort(key=lambda item: (item[2].timestamp(), str(item[0]).casefold()))
        day_start_hour = int(project_config["day_start_hour"])
        day_keys = sorted({_day_key(captured, day_start_hour) for _, _, captured, _, _ in probed})
        day_number = {key: index + 1 for index, key in enumerate(day_keys)}
        clips: list[Clip] = []
        for path, probe, captured, capture_source, warnings in probed:
            relative = path.relative_to(source)
            day_key = _day_key(captured, day_start_hour)
            video = _video_stream(probe)
            audio = _audio_stream(probe)
            duration = _duration(probe, video)
            if duration <= 0:
                warnings.append("영상 길이를 확인할 수 없습니다.")
            clip_id = "clip_" + stable_hash({"relative_path": str(relative)}, length=16)
            location = resolve_location(relative, day_key, config.get("locations", []))
            clips.append(
                Clip(
                    clip_id=clip_id,
                    path=str(path),
                    relative_path=str(relative),
                    fingerprint=file_fingerprint(path),
                    size_bytes=path.stat().st_size,
                    duration=round(duration, 3),
                    captured_at=captured.isoformat(),
                    capture_source=capture_source,
                    day_key=day_key,
                    travel_day=day_number[day_key],
                    width=int(video.get("width", 0) or 0),
                    height=int(video.get("height", 0) or 0),
                    fps=round(_fps(video), 4),
                    codec=str(video.get("codec_name", "unknown")),
                    rotation=_rotation(video),
                    has_audio=bool(audio),
                    audio_sample_rate=int(audio.get("sample_rate", 0) or 0) if audio else None,
                    location=location,
                    warnings=warnings,
                )
            )

        manifest = {
            "version": 1,
            "project": config["project"]["name"],
            "source_dir": str(source),
            "timezone": str(project_config["timezone"]),
            "day_start_hour": int(project_config["day_start_hour"]),
            "scan_config_hash": _scan_config_signature(config),
            "days": [{"day_key": key, "travel_day": day_number[key]} for key in day_keys],
            "clips": [clip.to_dict() for clip in clips],
            "manifest_hash": stable_hash([clip.to_dict() for clip in clips], length=32),
        }
        write_json(paths.manifest, manifest)
        state.mark_complete("scan", cache_key, {"clip_count": len(clips), "days": len(day_keys)})
        return manifest
    except BaseException as exc:
        state.mark_failed("scan", cache_key, str(exc))
        raise


def load_clips(paths: ProjectPaths, config: dict[str, Any] | None = None) -> list[Clip]:
    if not paths.manifest.exists():
        raise VideoSummaryError("먼저 scan 또는 run을 실행하세요.")
    manifest = read_json(paths.manifest)
    if config is not None and manifest.get("scan_config_hash") != _scan_config_signature(config):
        raise VideoSummaryError("촬영일/위치 설정이 scan 이후 바뀌었습니다. scan을 다시 실행하세요.")
    clips = [Clip.from_dict(item) for item in manifest.get("clips", [])]
    for clip in clips:
        source = Path(clip.path)
        if not source.exists() or not source.is_file():
            raise VideoSummaryError(f"원본 파일이 없어졌습니다. scan을 다시 실행하세요: {source}")
        if file_fingerprint(source) != clip.fingerprint:
            raise VideoSummaryError(f"원본 파일이 scan 이후 변경되었습니다. scan을 다시 실행하세요: {source}")
    return clips


def probe_media(path: Path) -> dict[str, Any]:
    completed = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-print_format",
            "json",
            str(path),
        ]
    )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise VideoSummaryError(f"ffprobe 결과를 읽을 수 없습니다: {path}") from exc


def infer_capture_time(
    path: Path,
    relative_path: Path,
    probe: dict[str, Any],
    timezone: ZoneInfo,
    overrides: list[dict[str, Any]] | Any,
) -> tuple[datetime, str, list[str]]:
    warnings: list[str] = []
    override = _date_override(relative_path, overrides)
    if override:
        override_timezone = timezone
        if override.get("timezone"):
            override_timezone = _timezone(str(override["timezone"]))
        captured_at = str(override.get("captured_at", "")).strip()
        if captured_at:
            return _parse_datetime(captured_at, override_timezone), "override", warnings

        override_date = _parse_override_date(str(override.get("date", "")))
        embedded_raw = _creation_time(probe)
        if embedded_raw:
            embedded = _parse_datetime_raw(embedded_raw)
            captured = _rebase_capture_date(embedded, override_date, override_timezone)
            return captured, "date_override:metadata", warnings

        filename_time = _filename_datetime(path.name, override_timezone)
        if filename_time:
            captured = _replace_wall_date(filename_time, override_date, override_timezone)
            return captured, "date_override:filename", warnings

        warnings.append("촬영 시각이 없어 파일 수정 시각의 시·분·초를 사용했습니다.")
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=override_timezone)
        captured = _replace_wall_date(modified, override_date, override_timezone)
        return captured, "date_override:mtime", warnings

    embedded_raw = _creation_time(probe)
    filename_time = _filename_datetime(path.name, timezone)
    if embedded_raw:
        embedded = _parse_datetime(embedded_raw, timezone)
        if filename_time and abs((embedded - filename_time).total_seconds()) > 12 * 3600:
            warnings.append("메타데이터 시각과 파일명 시각이 12시간 이상 다릅니다.")
        return embedded, "metadata", warnings
    if filename_time:
        return filename_time, "filename", warnings

    warnings.append("촬영 시각이 없어 파일 수정 시각을 사용했습니다.")
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone), "mtime", warnings


def resolve_location(relative_path: Path, day_key: str, rules: Any) -> str | None:
    if not isinstance(rules, list):
        return None
    relative = str(relative_path)
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if rule.get("day_key") and str(rule["day_key"]) != day_key:
            continue
        patterns = rule.get("match", ["*"])
        if isinstance(patterns, str):
            patterns = [patterns]
        if any(fnmatch.fnmatch(relative, str(pattern)) or fnmatch.fnmatch(relative_path.name, str(pattern)) for pattern in patterns):
            label = str(rule.get("label", "")).strip()
            if label:
                return label
    return None


def analyze_visual_signals(
    paths: ProjectPaths,
    clip: Clip,
    interval: float,
    *,
    force: bool = False,
) -> list[dict[str, float]]:
    cache_path = paths.signals / f"{clip.clip_id}.json"
    cache_key = stable_hash({"version": 1, "fingerprint": clip.fingerprint, "interval": interval})
    if not force and cache_path.exists():
        payload = read_json(cache_path)
        if payload.get("cache_key") == cache_key:
            return list(payload.get("samples", []))

    width, height = 96, 54
    frame_size = width * height
    vf = (
        f"fps=1/{max(0.5, interval):.3f},"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,format=gray"
    )
    args = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-threads",
        "1",
        "-i",
        clip.path,
        "-an",
        "-vf",
        vf,
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "pipe:1",
    ]
    samples: list[dict[str, float]] = []
    previous: bytes | None = None
    with tempfile.TemporaryFile() as error_log:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=error_log)
        assert process.stdout is not None
        try:
            index = 0
            while True:
                frame = process.stdout.read(frame_size)
                if not frame:
                    break
                if len(frame) != frame_size:
                    break
                total = sum(frame)
                mean = total / frame_size
                variance = max(0.0, sum(value * value for value in frame) / frame_size - mean * mean)
                contrast = math.sqrt(variance)
                motion = 0.0 if previous is None else sum(abs(a - b) for a, b in zip(frame, previous)) / (frame_size * 255)
                samples.append(
                    {
                        "time": round(index * interval, 3),
                        "brightness": round(mean / 255, 5),
                        "contrast": round(min(1.0, contrast / 80), 5),
                        "motion": round(min(1.0, motion * 3.0), 5),
                    }
                )
                previous = frame
                index += 1
        except BaseException:
            process.stdout.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
        finally:
            if not process.stdout.closed:
                process.stdout.close()
        return_code = process.wait()
        error_log.seek(0, os.SEEK_END)
        error_size = error_log.tell()
        error_log.seek(max(0, error_size - 8192))
        stderr = error_log.read().decode("utf-8", errors="replace")
    if return_code != 0:
        raise VideoSummaryError(f"프레임 분석 실패 ({Path(clip.path).name}): {stderr.strip()}")

    write_json(cache_path, {"version": 1, "cache_key": cache_key, "samples": samples})
    return samples


def extract_frame(clip: Clip, at: float, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}.partial{output.suffix}")
    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, at):.3f}",
            "-i",
            clip.path,
            "-frames:v",
            "1",
            "-vf",
            "scale=640:-2:force_original_aspect_ratio=decrease",
            "-q:v",
            "4",
            "-y",
            str(temporary),
        ]
    )
    os.replace(temporary, output)


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise VideoSummaryError(f"알 수 없는 timezone입니다: {name}") from exc


def _date_override(relative_path: Path, overrides: Any) -> dict[str, Any] | None:
    if not isinstance(overrides, list):
        return None
    relative = str(relative_path)
    for item in overrides:
        if not isinstance(item, dict):
            continue
        pattern = str(item.get("match", ""))
        if pattern and (fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(relative_path.name, pattern)):
            return item
    return None


def _creation_time(probe: dict[str, Any]) -> str | None:
    containers: list[dict[str, Any]] = []
    format_data = probe.get("format")
    if isinstance(format_data, dict):
        containers.append(format_data)
    containers.extend(stream for stream in probe.get("streams", []) if isinstance(stream, dict))
    for container in containers:
        tags = container.get("tags", {})
        if not isinstance(tags, dict):
            continue
        lowered = {str(key).casefold(): value for key, value in tags.items()}
        for key in ("creation_time", "com.apple.quicktime.creationdate", "date"):
            if lowered.get(key):
                return str(lowered[key])
    return None


def _parse_datetime(value: str, timezone: ZoneInfo) -> datetime:
    parsed = _parse_datetime_raw(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)


def _parse_datetime_raw(value: str) -> datetime:
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise VideoSummaryError(f"촬영 시각을 해석할 수 없습니다: {value}") from exc


def _parse_override_date(value: str) -> date:
    value = value.strip()
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise VideoSummaryError(f"보정 날짜를 해석할 수 없습니다: {value}") from exc
    if parsed.isoformat() != value:
        raise VideoSummaryError(f"보정 날짜는 YYYY-MM-DD 형식이어야 합니다: {value}")
    return parsed


def _rebase_capture_date(value: datetime, target_date: date, timezone: ZoneInfo) -> datetime:
    """Move a timestamp to a local calendar day while preserving its UTC clock.

    Osmo/QuickTime timestamps are offset-aware even when the camera's calendar was
    reset.  Rebuilding from the UTC clock avoids applying the reset year's DST
    offset to the real travel date.
    """
    if value.tzinfo is None:
        return _replace_wall_date(value, target_date, timezone)
    utc_value = value.astimezone(datetime_timezone.utc)
    matches: dict[float, datetime] = {}
    for offset in range(-2, 3):
        utc_date = target_date + timedelta(days=offset)
        candidate_utc = datetime(
            utc_date.year,
            utc_date.month,
            utc_date.day,
            utc_value.hour,
            utc_value.minute,
            utc_value.second,
            utc_value.microsecond,
            tzinfo=datetime_timezone.utc,
        )
        local = candidate_utc.astimezone(timezone)
        if local.date() == target_date:
            matches[candidate_utc.timestamp()] = local
    if len(matches) != 1:
        raise VideoSummaryError(
            f"보정 날짜에 현지 시각을 하나로 결정할 수 없습니다: {target_date}. "
            "파일별 captured_at을 사용하세요."
        )
    return next(iter(matches.values()))


def _replace_wall_date(value: datetime, target_date: date, timezone: ZoneInfo) -> datetime:
    local = value if value.tzinfo is None else value.astimezone(timezone)
    wall = datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        local.hour,
        local.minute,
        local.second,
        local.microsecond,
    )
    matches: dict[float, datetime] = {}
    for fold in (0, 1):
        candidate = wall.replace(tzinfo=timezone, fold=fold)
        round_trip = candidate.astimezone(datetime_timezone.utc).astimezone(timezone)
        if round_trip.replace(tzinfo=None) == wall:
            matches[candidate.timestamp()] = candidate
    if len(matches) != 1:
        raise VideoSummaryError(
            f"보정된 현지 시각이 DST 경계에서 하나로 결정되지 않습니다: {wall.isoformat()} {timezone.key}. "
            "offset이 포함된 captured_at을 사용하세요."
        )
    return next(iter(matches.values()))


def _day_key(captured_at: datetime, day_start_hour: int) -> str:
    local_date = captured_at.date()
    if captured_at.hour < day_start_hour:
        local_date -= timedelta(days=1)
    return local_date.isoformat()


def _filename_datetime(name: str, timezone: ZoneInfo) -> datetime | None:
    for pattern in _FILENAME_PATTERNS:
        match = pattern.search(name)
        if not match:
            continue
        try:
            return datetime(*(int(value) for value in match.groups()), tzinfo=timezone)
        except ValueError:
            continue
    return None


def _video_stream(probe: dict[str, Any]) -> dict[str, Any]:
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == "video":
            return stream
    raise VideoSummaryError("비디오 스트림이 없는 파일입니다.")


def _audio_stream(probe: dict[str, Any]) -> dict[str, Any]:
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == "audio":
            return stream
    return {}


def _duration(probe: dict[str, Any], video: dict[str, Any]) -> float:
    values = [video.get("duration"), probe.get("format", {}).get("duration")]
    for value in values:
        try:
            parsed = float(value)
            if math.isfinite(parsed) and parsed > 0:
                return parsed
        except (TypeError, ValueError):
            continue
    return 0.0


def _fps(video: dict[str, Any]) -> float:
    value = str(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1")
    try:
        numerator, denominator = value.split("/", 1)
        return float(numerator) / float(denominator) if float(denominator) else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def _rotation(video: dict[str, Any]) -> int:
    tags = video.get("tags", {}) if isinstance(video.get("tags"), dict) else {}
    if "rotate" in tags:
        try:
            return int(float(tags["rotate"])) % 360
        except (TypeError, ValueError):
            pass
    for side_data in video.get("side_data_list", []):
        if isinstance(side_data, dict) and "rotation" in side_data:
            try:
                return int(float(side_data["rotation"])) % 360
            except (TypeError, ValueError):
                continue
    return 0


def _scan_config_signature(config: dict[str, Any]) -> str:
    return stable_hash(
        {
            "capture_time_policy": _CAPTURE_TIME_POLICY_VERSION,
            "timezone": config["project"]["timezone"],
            "day_start_hour": config["project"]["day_start_hour"],
            "date_overrides": config.get("date_overrides", []),
            "locations": config.get("locations", []),
        }
    )
