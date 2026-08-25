from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .utils import VideoSummaryError, atomic_write_text, slugify


DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "project": {
        "name": "여행 영상",
        "destination": "",
        "timezone": "Asia/Seoul",
        "day_start_hour": 4,
        "language": "ko",
    },
    "editing": {
        "prompt": "날짜 순서를 지키고 여정, 재미있는 대화, 음식과 풍경이 균형 있게 드러나는 여행 브이로그",
        "target_minutes_per_day": 4.0,
        "soft_max_minutes_per_day": 10.0,
        "pacing_profile": "gentle",
        "tone_profile": "playful",
        "selection_strategy": "event_flow",
        "adaptive_fast_forward": True,
        "max_fast_forward_speed": 3.0,
        "exclude_ranges": [],
        "cold_open": True,
        "preserve_family_interviews": True,
        "preserve_meal_events": True,
        "episode_mode": "daily",
    },
    "analysis": {
        "asr_backend": "auto",
        "asr_model": "small",
        "cpu_threads": 4,
        "sample_interval_seconds": 3.0,
        "max_candidates_per_clip": 8,
    },
    "render": {
        "resolution": "1080p",
        "fps": 30,
        "encoder": "auto",
        "video_bitrate": "14M",
        "audio_bitrate": "192k",
        "font_file": "/System/Library/Fonts/AppleSDGothicNeo.ttc",
        "trip_intro_style": "mosaic",
        "trip_intro_grid_size": 7,
        "trip_intro_animation": "flow",
        "trip_intro_candidate_ids": [],
        "portrait_layout": "blur",
        "transition_seconds": 0.18,
        "intro_seconds": 4.0,
        "date_card_seconds": 3.0,
        "outro_seconds": 5.0,
        "outro_text": "여행은 계속됩니다",
        "music_file": "",
        "music_volume": 0.08,
    },
    "locations": [],
    "date_overrides": [],
}


@dataclass(frozen=True, slots=True)
class ProjectPaths:
    workspace: Path
    slug: str

    @property
    def root(self) -> Path:
        return self.workspace / ".video-summary" / self.slug

    @property
    def config(self) -> Path:
        return self.root / "project.yaml"

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.json"

    @property
    def state(self) -> Path:
        return self.root / "state.sqlite3"

    @property
    def transcripts(self) -> Path:
        return self.root / "transcripts"

    @property
    def signals(self) -> Path:
        return self.root / "signals"

    @property
    def frames(self) -> Path:
        return self.root / "frames"

    @property
    def candidates(self) -> Path:
        return self.root / "candidates.json"

    @property
    def planner(self) -> Path:
        return self.root / "planner"

    @property
    def plan(self) -> Path:
        return self.root / "edit-plan.json"

    @property
    def render(self) -> Path:
        return self.root / "render"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def exports(self) -> Path:
        return self.workspace / "exports" / self.slug

    def ensure(self) -> None:
        for path in (self.root, self.transcripts, self.signals, self.frames, self.planner, self.render, self.logs, self.exports):
            path.mkdir(parents=True, exist_ok=True)


def project_paths(workspace: str | Path, project_name: str) -> ProjectPaths:
    return ProjectPaths(Path(workspace).expanduser().resolve(), slugify(project_name))


def ensure_config(paths: ProjectPaths, project_name: str) -> dict[str, Any]:
    paths.ensure()
    if paths.config.exists():
        return load_config(paths)
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["project"]["name"] = project_name
    atomic_write_text(paths.config, yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
    return config


def load_config(paths: ProjectPaths) -> dict[str, Any]:
    if not paths.config.exists():
        raise VideoSummaryError(f"프로젝트 설정이 없습니다: {paths.config}")
    loaded = yaml.safe_load(paths.config.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise VideoSummaryError(f"잘못된 YAML 설정입니다: {paths.config}")
    config = copy.deepcopy(DEFAULT_CONFIG)
    _deep_update(config, loaded)
    _validate_config(config)
    return config


def save_config(paths: ProjectPaths, config: dict[str, Any]) -> None:
    _validate_config(config)
    atomic_write_text(paths.config, yaml.safe_dump(config, allow_unicode=True, sort_keys=False))


def _deep_update(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value


def _validate_config(config: dict[str, Any]) -> None:
    if type(config.get("version")) is not int or config["version"] != 1:
        raise VideoSummaryError("project.yaml version은 1이어야 합니다.")
    project = config.get("project", {})
    editing = config.get("editing", {})
    analysis = config.get("analysis", {})
    render = config.get("render", {})
    for name, section in (("project", project), ("editing", editing), ("analysis", analysis), ("render", render)):
        if not isinstance(section, dict):
            raise VideoSummaryError(f"{name} 설정은 object여야 합니다.")
    hour = project.get("day_start_hour", 4)
    if isinstance(hour, bool) or not isinstance(hour, int):
        raise VideoSummaryError("day_start_hour는 정수여야 합니다.")
    if not 0 <= hour <= 12:
        raise VideoSummaryError("day_start_hour는 0~12 사이여야 합니다.")
    timezone_name = project.get("timezone")
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise VideoSummaryError("timezone은 IANA timezone 문자열이어야 합니다.")
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise VideoSummaryError(f"알 수 없는 timezone입니다: {timezone_name}") from exc
    destination = project.get("destination", "")
    if not isinstance(destination, str):
        raise VideoSummaryError("destination은 문자열이어야 합니다.")
    if len(" ".join(destination.split())) > 100:
        raise VideoSummaryError("destination은 100자 이하여야 합니다.")
    target = _finite_number(editing.get("target_minutes_per_day"), "target_minutes_per_day")
    if not 0.1 <= target <= 180:
        raise VideoSummaryError("target_minutes_per_day는 0.1~180 사이여야 합니다.")
    soft_max = _finite_number(
        editing.get("soft_max_minutes_per_day", 10.0),
        "soft_max_minutes_per_day",
    )
    if not 0.1 <= soft_max <= 180:
        raise VideoSummaryError("soft_max_minutes_per_day는 0.1~180 사이여야 합니다.")
    pacing_profile = editing.get("pacing_profile", "gentle")
    if not isinstance(pacing_profile, str) or pacing_profile not in {"gentle", "balanced"}:
        raise VideoSummaryError("pacing_profile은 gentle 또는 balanced여야 합니다.")
    tone_profile = editing.get("tone_profile", "playful")
    if not isinstance(tone_profile, str) or tone_profile not in {"calm", "playful"}:
        raise VideoSummaryError("tone_profile은 calm 또는 playful이어야 합니다.")
    if editing.get("selection_strategy", "event_flow") != "event_flow":
        raise VideoSummaryError("selection_strategy는 event_flow여야 합니다.")
    if type(editing.get("adaptive_fast_forward", True)) is not bool:
        raise VideoSummaryError("adaptive_fast_forward는 true 또는 false여야 합니다.")
    max_fast_forward = _finite_number(
        editing.get("max_fast_forward_speed", 3.0),
        "max_fast_forward_speed",
    )
    if not 1.0 <= max_fast_forward <= 4.0:
        raise VideoSummaryError("max_fast_forward_speed는 1~4 사이여야 합니다.")
    exclude_ranges = editing.get("exclude_ranges", [])
    if not isinstance(exclude_ranges, list):
        raise VideoSummaryError("exclude_ranges는 list여야 합니다.")
    for index, rule in enumerate(exclude_ranges, start=1):
        field = f"exclude_ranges[{index}]"
        if not isinstance(rule, dict):
            raise VideoSummaryError(f"{field}는 object여야 합니다.")
        unknown = set(rule) - {"match", "start", "end", "reason"}
        if unknown:
            raise VideoSummaryError(f"{field}에 허용되지 않은 필드가 있습니다: {sorted(unknown)}")
        match = rule.get("match")
        if not isinstance(match, str) or not match.strip():
            raise VideoSummaryError(f"{field}.match는 비어 있지 않은 문자열이어야 합니다.")
        reason = rule.get("reason")
        if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 160:
            raise VideoSummaryError(f"{field}.reason은 1~160자 문자열이어야 합니다.")
        start = _finite_number(rule.get("start", 0.0), f"{field}.start")
        end_value = rule.get("end")
        end = _finite_number(end_value, f"{field}.end") if end_value is not None else None
        if start < 0 or (end is not None and end <= start):
            raise VideoSummaryError(f"{field}의 start/end 범위가 잘못되었습니다.")
    if editing.get("episode_mode") not in {"daily", "trip"}:
        raise VideoSummaryError("episode_mode는 daily 또는 trip이어야 합니다.")
    if type(editing.get("preserve_family_interviews", True)) is not bool:
        raise VideoSummaryError("preserve_family_interviews는 true 또는 false여야 합니다.")
    if type(editing.get("preserve_meal_events", True)) is not bool:
        raise VideoSummaryError("preserve_meal_events는 true 또는 false여야 합니다.")
    if render.get("resolution") not in {"720p", "1080p", "2160p"}:
        raise VideoSummaryError("resolution은 720p, 1080p, 2160p 중 하나여야 합니다.")
    if render.get("trip_intro_style") not in {"card", "mosaic"}:
        raise VideoSummaryError("trip_intro_style은 card 또는 mosaic여야 합니다.")
    intro_grid_size = render.get("trip_intro_grid_size", 7)
    if isinstance(intro_grid_size, bool) or not isinstance(intro_grid_size, int) or intro_grid_size not in {6, 7, 8}:
        raise VideoSummaryError("trip_intro_grid_size는 6, 7, 8 중 하나여야 합니다.")
    if render.get("trip_intro_animation", "flow") not in {"static", "flow"}:
        raise VideoSummaryError("trip_intro_animation은 static 또는 flow여야 합니다.")
    if render.get("portrait_layout", "blur") not in {"blur", "pillarbox", "crop"}:
        raise VideoSummaryError("portrait_layout은 blur, pillarbox, crop 중 하나여야 합니다.")
    intro_candidate_ids = render.get("trip_intro_candidate_ids", [])
    if (
        not isinstance(intro_candidate_ids, list)
        or any(not isinstance(value, str) or not value.strip() for value in intro_candidate_ids)
        or len(set(intro_candidate_ids)) != len(intro_candidate_ids)
    ):
        raise VideoSummaryError("trip_intro_candidate_ids는 중복 없는 candidate ID 문자열 list여야 합니다.")
    transition_seconds = _finite_number(render.get("transition_seconds", 0.18), "transition_seconds")
    if not 0 <= transition_seconds <= 1.0:
        raise VideoSummaryError("transition_seconds는 0~1 사이여야 합니다.")
    interval = _finite_number(analysis.get("sample_interval_seconds"), "sample_interval_seconds")
    if not 0.5 <= interval <= 60:
        raise VideoSummaryError("sample_interval_seconds는 0.5~60 사이여야 합니다.")
    for key, minimum, maximum in (("cpu_threads", 1, 32), ("max_candidates_per_clip", 1, 50)):
        value = analysis.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise VideoSummaryError(f"{key}는 {minimum}~{maximum} 사이의 정수여야 합니다.")
    fps = render.get("fps")
    if isinstance(fps, bool) or not isinstance(fps, int) or not 1 <= fps <= 120:
        raise VideoSummaryError("fps는 1~120 사이의 정수여야 합니다.")
    music_volume = _finite_number(render.get("music_volume"), "music_volume")
    if not 0 <= music_volume <= 1:
        raise VideoSummaryError("music_volume은 0~1 사이여야 합니다.")
    date_overrides = config.get("date_overrides", [])
    if not isinstance(config.get("locations", []), list) or not isinstance(date_overrides, list):
        raise VideoSummaryError("locations와 date_overrides는 list여야 합니다.")
    for index, rule in enumerate(date_overrides, start=1):
        field = f"date_overrides[{index}]"
        if not isinstance(rule, dict):
            raise VideoSummaryError(f"{field}는 object여야 합니다.")
        match = rule.get("match")
        if not isinstance(match, str) or not match.strip():
            raise VideoSummaryError(f"{field}.match는 비어 있지 않은 문자열이어야 합니다.")
        captured_at = rule.get("captured_at")
        override_date = rule.get("date")
        has_captured_at = isinstance(captured_at, str) and bool(captured_at.strip())
        has_date = isinstance(override_date, str) and bool(override_date.strip())
        if has_captured_at == has_date:
            raise VideoSummaryError(f"{field}에는 captured_at 또는 date 중 하나만 있어야 합니다.")
        if has_captured_at:
            if "timezone" in rule:
                raise VideoSummaryError(f"{field}.captured_at에는 offset을 쓰고 timezone은 함께 쓰지 마세요.")
            try:
                datetime.fromisoformat(captured_at.strip().replace("Z", "+00:00"))
            except ValueError as exc:
                raise VideoSummaryError(f"{field}.captured_at을 해석할 수 없습니다.") from exc
        else:
            try:
                parsed_date = date.fromisoformat(override_date.strip())
            except ValueError as exc:
                raise VideoSummaryError(f"{field}.date는 YYYY-MM-DD 형식이어야 합니다.") from exc
            if parsed_date.isoformat() != override_date.strip():
                raise VideoSummaryError(f"{field}.date는 YYYY-MM-DD 형식이어야 합니다.")
            rule_timezone = rule.get("timezone", timezone_name)
            if not isinstance(rule_timezone, str) or not rule_timezone.strip():
                raise VideoSummaryError(f"{field}.timezone은 IANA timezone 문자열이어야 합니다.")
            try:
                ZoneInfo(rule_timezone)
            except ZoneInfoNotFoundError as exc:
                raise VideoSummaryError(f"알 수 없는 timezone입니다: {rule_timezone}") from exc
    for key in ("intro_seconds", "date_card_seconds", "outro_seconds"):
        duration = _finite_number(render.get(key), key)
        if not 0.1 <= duration <= 30:
            raise VideoSummaryError(f"{key}는 0.1~30 사이여야 합니다.")
    if not isinstance(render.get("font_file"), str):
        raise VideoSummaryError("font_file은 문자열 경로여야 합니다.")


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VideoSummaryError(f"{field}는 숫자여야 합니다.")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise VideoSummaryError(f"{field}는 유한한 숫자여야 합니다.")
    return parsed
