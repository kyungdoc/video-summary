from __future__ import annotations

import os
import math
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from .animated_mosaic import ANIMATED_MOSAIC_POLICY_VERSION, render_animated_mosaic
from .candidates import load_candidates
from .intro_metadata import format_day_period, resolve_intro_metadata
from .media import load_clips, probe_media
from .models import Candidate, Clip, EditPlan, Episode, PlanSegment
from .planner import validate_and_normalize_plan
from .project import ProjectPaths
from .render_assets import (
    FONT_CANDIDATES,
    MOSAIC_CAPACITY,
    MOSAIC_COLUMNS,
    MOSAIC_ROWS,
    chapter_time,
    create_card,
    create_lower_third,
    create_mosaic_card,
    vtt_time,
)
from .state import StateStore
from .transcribe import load_transcript
from .utils import (
    VideoSummaryError,
    atomic_write_text,
    file_fingerprint,
    print_status,
    read_json,
    run_command,
    stable_hash,
    write_json,
)


RENDER_POLICY_VERSION = 19
SOURCE_RENDER_POLICY_VERSION = 8
CARD_RENDER_POLICY_VERSION = 4
MOSAIC_CARD_POLICY_VERSION = 5
YOUTUBE_MIN_CHAPTERS = 3
YOUTUBE_MIN_CHAPTER_SECONDS = 10
AAC_FRAME_SAMPLES = 1024
DEFAULT_AUDIO_SAMPLE_RATE = 48000


@dataclass(slots=True)
class Piece:
    path: Path
    duration: float
    label: str
    candidate: Candidate | None = None
    segment: PlanSegment | None = None
    day_chapter: str | None = None
    source_members: tuple["SourceMember", ...] = ()


@dataclass(frozen=True, slots=True)
class SourceMember:
    candidate: Candidate
    segment: PlanSegment
    offset: float
    label: str


@dataclass(frozen=True, slots=True)
class SourceSelection:
    candidate: Candidate
    segment: PlanSegment


def render_cache_key(
    plan: EditPlan,
    clips: list[Clip],
    render_config: dict[str, Any],
    mode: str,
    draft: bool,
    width: int,
    height: int,
    fps: int,
    encoder: str,
    bitrate: str,
    *,
    version: int,
    font_signature: list[dict[str, str]] | None = None,
    trip_intro_signature: list[dict[str, str]] | None = None,
    intro_metadata: dict[str, Any] | None = None,
    moment_coverage: dict[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "version": version,
        "plan": plan.to_dict(),
        "clips": [(clip.clip_id, clip.fingerprint) for clip in clips],
        "render": render_config,
        "episode_mode": mode,
        "draft": draft,
        "format": [width, height, fps, encoder, bitrate],
    }
    if version >= 13:
        payload["music_signature"] = render_music_signature(render_config)
    if font_signature is not None:
        payload["font_signature"] = font_signature
    if trip_intro_signature is not None:
        grid_size = configured_mosaic_grid_size(render_config)
        payload["trip_intro_signature"] = {
            "grid": [grid_size, grid_size],
            "frames": trip_intro_signature,
        }
    if version >= 15:
        payload["intro_metadata"] = intro_metadata or {}
    if version >= 18:
        payload["moment_coverage"] = moment_coverage or {}
    return stable_hash(payload, length=32)


def render_music_signature(render_config: dict[str, Any]) -> dict[str, str] | None:
    configured = str(render_config.get("music_file", "")).strip()
    if not configured:
        return None
    path = Path(configured).expanduser().resolve()
    try:
        fingerprint = file_fingerprint(path) if path.is_file() else "missing"
    except OSError:
        fingerprint = "unreadable"
    return {"path": str(path), "fingerprint": fingerprint}


def render_font_signature(config: dict[str, Any]) -> list[dict[str, str]]:
    configured = Path(str(config["render"].get("font_file", ""))).expanduser()
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for path in (configured, *FONT_CANDIDATES):
        normalized = str(path)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        try:
            if path.is_file():
                result.append({"path": normalized, "fingerprint": file_fingerprint(path)})
        except OSError:
            result.append({"path": normalized, "fingerprint": "unreadable"})
    return result


def source_cache_namespace(
    config: dict[str, Any],
    width: int,
    height: int,
    fps: int,
    encoder: str,
    bitrate: str,
) -> str:
    return stable_hash(
        {
            "version": SOURCE_RENDER_POLICY_VERSION,
            "format": [width, height, fps, encoder, bitrate],
            "audio_bitrate": str(config["render"].get("audio_bitrate", "192k")),
            "font_signature": render_font_signature(config),
        },
        length=24,
    )


def report_outputs_exist(report: dict[str, Any]) -> bool:
    outputs = report.get("outputs", [])
    if not isinstance(outputs, list) or not outputs:
        return False
    required = ("path", "description", "chapters", "subtitles", "timeline")
    return all(
        isinstance(item, dict)
        and all(isinstance(item.get(key), str) and Path(item[key]).exists() for key in required)
        for item in outputs
    )


def _required_events_of_kind(
    candidates_payload: dict[str, Any],
    kind: str,
) -> list[dict[str, Any]]:
    raw_events = candidates_payload.get("required_events", [])
    if not isinstance(raw_events, list):
        raise VideoSummaryError("candidates.json의 required_events가 잘못되었습니다.")
    selected: list[dict[str, Any]] = []
    for raw_event in raw_events:
        if not isinstance(raw_event, dict):
            raise VideoSummaryError("candidates.json의 required_events가 잘못되었습니다.")
        event_kind = str(raw_event.get("kind", "family_interview")).strip()
        if event_kind not in {"family_interview", "meal"}:
            raise VideoSummaryError(
                f"candidates.json에 알 수 없는 required event kind가 있습니다: {event_kind}"
            )
        if event_kind == kind:
            selected.append(raw_event)
    return selected


def family_interview_coverage(
    candidates_payload: dict[str, Any],
    candidates: list[Candidate],
    plan: EditPlan,
    *,
    enabled: bool,
) -> dict[str, Any]:
    """Build privacy-minimal, auditable coverage for mandatory interview events."""
    selected_ids = {
        segment.candidate_id
        for episode in plan.episodes
        for segment in episode.segments
    }
    candidate_by_id = {candidate.candidate_id: candidate for candidate in candidates}
    if not enabled:
        return {
            "status": "disabled",
            "detected_event_count": 0,
            "required_candidate_count": 0,
            "selected_candidate_count": 0,
            "events": [],
        }
    raw_events = _required_events_of_kind(candidates_payload, "family_interview")

    events: list[dict[str, Any]] = []
    required_ids: set[str] = set()
    selected_required_ids: set[str] = set()
    for raw_event in raw_events:
        if not isinstance(raw_event, dict):
            raise VideoSummaryError("candidates.json의 가족 인터뷰 이벤트가 잘못되었습니다.")
        event_id = str(raw_event.get("event_id", "")).strip()
        candidate_ids_value = raw_event.get("candidate_ids", [])
        if (
            not event_id
            or not isinstance(candidate_ids_value, list)
            or any(not isinstance(value, str) or not value.strip() for value in candidate_ids_value)
        ):
            raise VideoSummaryError("candidates.json의 가족 인터뷰 이벤트가 잘못되었습니다.")
        candidate_ids = list(dict.fromkeys(value.strip() for value in candidate_ids_value))
        missing_catalog = [value for value in candidate_ids if value not in candidate_by_id]
        if missing_catalog:
            raise VideoSummaryError(
                f"가족 인터뷰 이벤트가 알 수 없는 후보를 참조합니다: {event_id}"
            )
        selected_event_ids = [value for value in candidate_ids if value in selected_ids]
        missing_selected = [value for value in candidate_ids if value not in selected_ids]
        if missing_selected:
            raise VideoSummaryError(
                "필수 가족 인터뷰 후보가 최종 plan에서 누락되었습니다: "
                + ", ".join(missing_selected)
            )
        required_ids.update(candidate_ids)
        selected_required_ids.update(selected_event_ids)
        signals_value = raw_event.get("signals", [])
        signals = (
            [str(value) for value in signals_value if isinstance(value, str)]
            if isinstance(signals_value, list)
            else []
        )
        confidence_value = raw_event.get("confidence")
        confidence = (
            round(float(confidence_value), 3)
            if isinstance(confidence_value, (int, float)) and not isinstance(confidence_value, bool)
            else None
        )
        events.append(
            {
                "event_id": event_id,
                "day_key": str(raw_event.get("day_key", "")),
                "source": "local_transcript",
                "confidence": confidence,
                "signals": signals,
                "candidate_ids": candidate_ids,
                "selected_candidate_ids": selected_event_ids,
            }
        )

    return {
        "status": "satisfied" if events else "not_detected",
        "detected_event_count": len(events),
        "required_candidate_count": len(required_ids),
        "selected_candidate_count": len(selected_required_ids),
        "events": events,
    }


def meal_event_coverage(
    candidates_payload: dict[str, Any],
    candidates: list[Candidate],
    plan: EditPlan,
    *,
    enabled: bool,
) -> dict[str, Any]:
    """Audit one-of coverage for locally detected meal events."""
    selected_ids = {
        segment.candidate_id
        for episode in plan.episodes
        for segment in episode.segments
    }
    candidate_by_id = {candidate.candidate_id: candidate for candidate in candidates}
    empty = {
        "detected_event_count": 0,
        "option_candidate_count": 0,
        "selected_event_count": 0,
        "selected_candidate_count": 0,
        "events": [],
    }
    if not enabled:
        return {"status": "disabled", **empty}
    meal_events = _required_events_of_kind(candidates_payload, "meal")
    if not meal_events:
        return {"status": "not_detected", **empty}

    events: list[dict[str, Any]] = []
    option_ids: set[str] = set()
    selected_option_ids: set[str] = set()
    for raw_event in meal_events:
        event_id = str(raw_event.get("event_id", "")).strip()
        selection_mode = str(raw_event.get("selection_mode", "")).strip()
        candidate_ids_value = raw_event.get("candidate_ids", [])
        if (
            not event_id
            or selection_mode != "one_of"
            or not isinstance(candidate_ids_value, list)
            or not candidate_ids_value
            or any(not isinstance(value, str) or not value.strip() for value in candidate_ids_value)
        ):
            raise VideoSummaryError("candidates.json의 식사 이벤트가 잘못되었습니다.")
        candidate_ids = list(dict.fromkeys(value.strip() for value in candidate_ids_value))
        missing_catalog = [value for value in candidate_ids if value not in candidate_by_id]
        if missing_catalog:
            raise VideoSummaryError(
                f"식사 이벤트가 알 수 없는 후보를 참조합니다: {event_id}"
            )
        selected_event_ids = [value for value in candidate_ids if value in selected_ids]
        if not selected_event_ids:
            raise VideoSummaryError(
                f"필수 식사 이벤트가 최종 plan에서 누락되었습니다: {event_id}"
            )
        option_ids.update(candidate_ids)
        selected_option_ids.update(selected_event_ids)
        signals_value = raw_event.get("signals", [])
        signals = (
            [str(value) for value in signals_value if isinstance(value, str)]
            if isinstance(signals_value, list)
            else []
        )
        confidence_value = raw_event.get("confidence")
        confidence = (
            round(float(confidence_value), 3)
            if isinstance(confidence_value, (int, float)) and not isinstance(confidence_value, bool)
            else None
        )
        events.append(
            {
                "event_id": event_id,
                "day_key": str(raw_event.get("day_key", "")),
                "subtype": str(raw_event.get("subtype", "meal")),
                "source": "local_timeline",
                "confidence": confidence,
                "signals": signals,
                "selection_mode": "one_of",
                "candidate_ids": candidate_ids,
                "selected_candidate_ids": selected_event_ids,
            }
        )

    return {
        "status": "satisfied",
        "detected_event_count": len(events),
        "option_candidate_count": len(option_ids),
        "selected_event_count": len(events),
        "selected_candidate_count": len(selected_option_ids),
        "events": events,
    }


def render_project(
    paths: ProjectPaths,
    config: dict[str, Any],
    *,
    draft: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    if not paths.plan.exists():
        raise VideoSummaryError("먼저 plan 또는 run을 실행하세요.")
    candidates = load_candidates(paths, config)
    clips = load_clips(paths, config)
    candidates_payload = read_json(paths.candidates)
    raw_plan = read_json(paths.plan)
    validated = validate_and_normalize_plan(
        raw_plan,
        config,
        str(raw_plan.get("prompt") or config["editing"].get("prompt", "")),
        candidates,
        str(candidates_payload["candidate_set_hash"]),
        str(raw_plan.get("planner", "file")),
    )
    render_config = dict(config["render"])
    width, height = resolution("720p" if draft else str(render_config["resolution"]))
    fps = int(render_config.get("fps", 30))
    bitrate = "4M" if draft else str(render_config.get("video_bitrate", "14M"))
    encoder = resolve_encoder(str(render_config.get("encoder", "auto")))
    mode = str(config["editing"].get("episode_mode", "daily"))
    candidate_by_id = {item.candidate_id: item for item in candidates}
    ordered_episodes = sorted(validated.episodes, key=lambda item: (item.travel_day, item.day_key))
    manifest = read_json(paths.manifest)
    intro_metadata = resolve_intro_metadata(
        config,
        manifest,
        (episode.day_key for episode in ordered_episodes),
    )
    moment_coverage = {
        "family_interviews": family_interview_coverage(
            candidates_payload,
            candidates,
            validated,
            enabled=bool(config["editing"].get("preserve_family_interviews", True)),
        ),
        "meals": meal_event_coverage(
            candidates_payload,
            candidates,
            validated,
            enabled=bool(config["editing"].get("preserve_meal_events", True)),
        ),
    }
    trip_intro_signature: list[dict[str, str]] | None = None
    if mode == "trip" and render_config.get("trip_intro_style") == "mosaic":
        grid_size = configured_mosaic_grid_size(render_config)
        selected_intro_frames = select_trip_intro_frames(
            ordered_episodes,
            candidate_by_id,
            paths.root,
            render_config.get("trip_intro_candidate_ids", []),
            limit=grid_size * grid_size,
        )
        trip_intro_signature = [
            {
                "candidate_id": candidate.candidate_id,
                "fingerprint": file_fingerprint(path),
            }
            for candidate, path in selected_intro_frames
        ]
    cache_key = render_cache_key(
        validated, clips, render_config, mode, draft, width, height, fps, encoder, bitrate,
        version=RENDER_POLICY_VERSION,
        font_signature=render_font_signature(config),
        trip_intro_signature=trip_intro_signature,
        intro_metadata=intro_metadata.to_dict(),
        moment_coverage=moment_coverage,
    )
    legacy_cache_key = render_cache_key(
        validated, clips, render_config, mode, draft, width, height, fps, encoder, bitrate,
        version=5,
    )
    report_path = paths.root / "render-report.json"
    state = StateStore(paths.state)
    if not force and report_path.exists() and state.is_complete("render", cache_key):
        report = read_json(report_path)
        if report.get("cache_key") == cache_key and report_outputs_exist(report):
            print_status("render: 캐시 사용")
            return report

    state.mark_running("render", cache_key, {"encoder": encoder, "resolution": [width, height]})
    try:
        render_root = paths.render / cache_key
        segments_dir = paths.render / "content" / "segments" / source_cache_namespace(
            config, width, height, fps, encoder, bitrate
        )
        legacy_segments_dirs = legacy_segment_directories(paths.render, legacy_cache_key)
        cards_dir = render_root / "cards"
        overlays_dir = render_root / "overlays"
        for directory in (segments_dir, cards_dir, overlays_dir):
            directory.mkdir(parents=True, exist_ok=True)
        clip_by_id = {item.clip_id: item for item in clips}
        outputs: list[dict[str, Any]] = []
        trip_intro_report: dict[str, Any] | None = None
        if mode == "daily":
            for episode in ordered_episodes:
                print_status(f"render DAY {episode.travel_day}: {episode.day_key}")
                pieces = episode_pieces(
                    episode,
                    validated,
                    candidate_by_id,
                    clip_by_id,
                    config,
                    segments_dir,
                    cards_dir,
                    overlays_dir,
                    width,
                    height,
                    fps,
                    encoder,
                    bitrate,
                    draft,
                    include_intro=True,
                    include_outro=True,
                    intro_title=intro_metadata.destination,
                    intro_subtitle=format_day_period(episode.day_key),
                    force=force,
                    legacy_segments_dirs=legacy_segments_dirs,
                )
                filename = f"{episode.day_key}-day-{episode.travel_day:02d}{'-draft' if draft else ''}.mp4"
                outputs.append(
                    assemble_output(
                        pieces,
                        paths.exports / filename,
                        episode,
                        paths,
                        config,
                        render_root / f"assembly-day-{episode.travel_day:02d}",
                        width,
                        height,
                        draft,
                    )
                )
        else:
            trip_intro, trip_intro_report = render_trip_intro_piece(
                ordered_episodes,
                candidate_by_id,
                paths.root,
                cards_dir,
                intro_metadata.destination,
                intro_metadata.period,
                float(render_config.get("intro_seconds", 4.0)),
                width,
                height,
                fps,
                encoder,
                bitrate,
                config,
                force,
            )
            pieces = [trip_intro]
            for episode in ordered_episodes:
                pieces.extend(
                    episode_pieces(
                        episode,
                        validated,
                        candidate_by_id,
                        clip_by_id,
                        config,
                        segments_dir,
                        cards_dir,
                        overlays_dir,
                        width,
                        height,
                        fps,
                        encoder,
                        bitrate,
                        draft,
                        include_intro=False,
                        include_outro=False,
                        force=force,
                        legacy_segments_dirs=legacy_segments_dirs,
                    )
                )
            pieces.append(
                render_card_piece(
                    cards_dir,
                    "trip-outro",
                    str(render_config.get("outro_text", "여행은 계속됩니다")),
                    intro_metadata.destination,
                    float(render_config.get("outro_seconds", 5.0)),
                    width,
                    height,
                    fps,
                    encoder,
                    bitrate,
                    config,
                    force,
                )
            )
            summary_episode = Episode(
                day_key=ordered_episodes[0].day_key,
                travel_day=1,
                title=intro_metadata.destination,
                subtitle=intro_metadata.period,
                summary=f"{intro_metadata.period} 동안의 여정과 재미있는 순간을 날짜 순서대로 담았습니다.",
                target_duration=sum(episode.target_duration for episode in ordered_episodes),
                segments=[],
            )
            outputs.append(
                assemble_output(
                    pieces,
                    paths.exports / f"trip-summary{'-draft' if draft else ''}.mp4",
                    summary_episode,
                    paths,
                    config,
                    render_root / "assembly-trip",
                    width,
                    height,
                    draft,
                )
            )
        report = {
            "version": 5,
            "cache_key": cache_key,
            "project": validated.project,
            "planner": validated.planner,
            "encoder": encoder,
            "resolution": [width, height],
            "fps": fps,
            "mode": mode,
            "draft": draft,
            "intro_metadata": intro_metadata.to_dict(),
            "moment_coverage": moment_coverage,
            "outputs": outputs,
        }
        if trip_intro_report is not None:
            report["trip_intro"] = trip_intro_report
        write_json(report_path, report)
        state.mark_complete("render", cache_key, {"output_count": len(outputs), "encoder": encoder})
        return report
    except BaseException as exc:
        state.mark_failed("render", cache_key, str(exc))
        raise


def resolve_encoder(requested: str) -> str:
    completed = run_command(["ffmpeg", "-hide_banner", "-encoders"])
    available = completed.stdout + completed.stderr
    if requested == "auto":
        return "h264_videotoolbox" if "h264_videotoolbox" in available else "libx264"
    if requested not in {"h264_videotoolbox", "libx264"}:
        raise VideoSummaryError("encoder는 auto, h264_videotoolbox, libx264 중 하나여야 합니다.")
    if requested not in available:
        raise VideoSummaryError(f"현재 FFmpeg가 {requested} encoder를 지원하지 않습니다.")
    return requested


def episode_pieces(
    episode: Episode,
    plan: EditPlan,
    candidate_by_id: dict[str, Candidate],
    clip_by_id: dict[str, Clip],
    config: dict[str, Any],
    segments_dir: Path,
    cards_dir: Path,
    overlays_dir: Path,
    width: int,
    height: int,
    fps: int,
    encoder: str,
    bitrate: str,
    draft: bool,
    *,
    include_intro: bool,
    include_outro: bool,
    force: bool,
    intro_title: str | None = None,
    intro_subtitle: str | None = None,
    legacy_segments_dirs: tuple[Path, ...] = (),
) -> list[Piece]:
    pieces: list[Piece] = []
    render_config = config["render"]
    segments = sorted(
        episode.segments,
        key=lambda segment: source_segment_sort_key(segment, candidate_by_id),
    )
    if include_intro:
        pieces.append(
            render_card_piece(
                cards_dir,
                f"intro-day-{episode.travel_day}",
                intro_title or plan.project,
                intro_subtitle or episode.subtitle,
                float(render_config.get("intro_seconds", 4.0)), width, height, fps, encoder, bitrate, config, force,
            )
        )
    date_piece = render_card_piece(
        cards_dir, f"date-day-{episode.travel_day}", episode.title, episode.subtitle,
        float(render_config.get("date_card_seconds", 3.0)), width, height, fps, encoder, bitrate, config, force,
    )
    date_piece.day_chapter = day_chapter_label(episode)
    pieces.append(date_piece)
    groups = coalesce_source_selections(segments, candidate_by_id)
    last_location: str | None = None
    for group_index, group in enumerate(groups):
        first = group[0]
        segment = first.segment
        candidate = first.candidate
        location = effective_source_location(segment, candidate)
        show_location = location if location and location != last_location else None
        if location:
            last_location = location
        member_labels = [
            show_location if member_index == 0 and show_location else member.segment.role
            for member_index, member in enumerate(group)
        ]
        pieces.append(
            render_source_piece(
                segment, candidate, clip_by_id[candidate.clip_id], segments_dir, overlays_dir,
                width, height, fps, encoder, bitrate, config, location_overlay=show_location, force=force,
                fade_in=True, fade_out=True,
                coalesced_selections=tuple(group), member_labels=tuple(member_labels),
                legacy_segments_dirs=legacy_segments_dirs,
            )
        )
    if include_outro:
        pieces.append(
            render_card_piece(
                cards_dir, f"outro-day-{episode.travel_day}", str(render_config.get("outro_text", "여행은 계속됩니다")),
                episode.title, float(render_config.get("outro_seconds", 5.0)), width, height, fps, encoder, bitrate, config, force,
            )
        )
    return pieces


def source_segment_sort_key(
    segment: PlanSegment,
    candidate_by_id: dict[str, Candidate],
) -> tuple[float, float, str]:
    candidate = candidate_by_id[segment.candidate_id]
    try:
        captured_at = datetime.fromisoformat(candidate.captured_at.replace("Z", "+00:00"))
    except ValueError:
        return (float("inf"), candidate.start, candidate.candidate_id)
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=timezone.utc)
    return (captured_at.astimezone(timezone.utc).timestamp(), candidate.start, candidate.candidate_id)


def normalized_optional_text(value: str | None) -> str | None:
    normalized = " ".join((value or "").split())
    return normalized or None


def is_required_event_candidate(candidate: Candidate) -> bool:
    return bool(candidate.required_event_ids)


def effective_source_location(segment: PlanSegment, candidate: Candidate) -> str | None:
    if is_required_event_candidate(candidate):
        return None
    return normalized_optional_text(segment.location or candidate.location)


def effective_source_caption(segment: PlanSegment, candidate: Candidate) -> str | None:
    if is_required_event_candidate(candidate):
        return None
    return normalized_optional_text(segment.caption)


def source_selections_are_contiguous(
    previous: SourceSelection,
    current: SourceSelection,
    *,
    tolerance: float = 0.001,
) -> bool:
    gap = current.candidate.start - previous.candidate.end
    return (
        previous.candidate.clip_id == current.candidate.clip_id
        and -tolerance - 1e-9 <= gap <= tolerance + 1e-9
        and previous.segment.speed == current.segment.speed
        and effective_source_location(previous.segment, previous.candidate)
        == effective_source_location(current.segment, current.candidate)
        and effective_source_caption(previous.segment, previous.candidate)
        == effective_source_caption(current.segment, current.candidate)
    )


def coalesce_source_selections(
    segments: list[PlanSegment],
    candidate_by_id: dict[str, Candidate],
) -> list[list[SourceSelection]]:
    groups: list[list[SourceSelection]] = []
    for segment in segments:
        selection = SourceSelection(candidate_by_id[segment.candidate_id], segment)
        if groups and source_selections_are_contiguous(groups[-1][-1], selection):
            groups[-1].append(selection)
        else:
            groups.append([selection])
    return groups


def day_chapter_label(episode: Episode) -> str:
    title = " ".join(episode.title.split())
    prefix = f"DAY {episode.travel_day}"
    detail = title
    if title.casefold().startswith(prefix.casefold()):
        detail = title[len(prefix):].lstrip(" ·-")
    if detail and detail != "여행의 하루":
        return f"{prefix} · {episode.day_key} · {detail}"
    return f"{prefix} · {episode.day_key}"


def legacy_segment_directories(render_root: Path, preferred_key: str) -> tuple[Path, ...]:
    # v3 segment names did not include the audio bitrate or a font fingerprint.
    # Only the exact v5 render root proves the rest of render_config matched.
    return (render_root / preferred_key / "segments",)


def resolution(value: str) -> tuple[int, int]:
    return {"720p": (1280, 720), "1080p": (1920, 1080), "2160p": (3840, 2160)}[value]


def configured_mosaic_grid_size(render_config: dict[str, Any]) -> int:
    return int(render_config.get("trip_intro_grid_size", MOSAIC_COLUMNS))


def render_trip_intro_piece(
    episodes: list[Episode],
    candidate_by_id: dict[str, Candidate],
    project_root: Path,
    cards_dir: Path,
    title: str,
    subtitle: str,
    duration: float,
    width: int,
    height: int,
    fps: int,
    encoder: str,
    bitrate: str,
    config: dict[str, Any],
    force: bool,
) -> tuple[Piece, dict[str, Any]]:
    requested_style = str(config["render"].get("trip_intro_style", "mosaic"))
    selected: list[tuple[Candidate, Path]] = []
    if requested_style == "mosaic":
        grid_size = configured_mosaic_grid_size(config["render"])
        selected = select_trip_intro_frames(
            episodes,
            candidate_by_id,
            project_root,
            config["render"].get("trip_intro_candidate_ids", []),
            limit=grid_size * grid_size,
        )
    if selected:
        mosaic_title = " ".join(title.replace("-", " ").split()).upper()
        requested_animation = str(config["render"].get("trip_intro_animation", "flow"))

        def mosaic_report(piece: Piece, motion: str, fallback_reason: str | None = None) -> dict[str, Any]:
            report: dict[str, Any] = {
                "requested_style": requested_style,
                "effective_style": "mosaic",
                "grid": [grid_size, grid_size],
                "frame_count": len(selected),
                "tile_count": len(selected),
                "video_frame_count": int(round(piece.duration * fps)),
                "requested_duration": duration,
                "rendered_duration": piece.duration,
                "requested_motion": requested_animation,
                "motion": motion,
                "flow_order": (
                    "chronological_serpentine" if motion == "flow" else "chronological_row_major"
                ),
                "candidate_ids": [candidate.candidate_id for candidate, _path in selected],
                "frames_per_day": mosaic_frames_per_day(selected),
            }
            if fallback_reason:
                report["fallback_reason"] = fallback_reason
            return report

        try:
            piece = render_mosaic_card_piece(
                cards_dir,
                "trip-intro-mosaic",
                mosaic_title,
                subtitle,
                duration,
                selected,
                width,
                height,
                fps,
                encoder,
                bitrate,
                config,
                force,
            )
            return piece, mosaic_report(piece, requested_animation)
        except (OSError, ValueError, VideoSummaryError) as exc:
            if requested_animation == "flow":
                print_status(f"trip intro: 동적 모자이크 생성 실패, 정적 모자이크 시도 ({exc})")
                fallback_config = {
                    **config,
                    "render": {**config["render"], "trip_intro_animation": "static"},
                }
                try:
                    piece = render_mosaic_card_piece(
                        cards_dir,
                        "trip-intro-mosaic",
                        mosaic_title,
                        subtitle,
                        duration,
                        selected,
                        width,
                        height,
                        fps,
                        encoder,
                        bitrate,
                        fallback_config,
                        force,
                    )
                except (OSError, ValueError, VideoSummaryError) as static_exc:
                    print_status(f"trip intro: 정적 모자이크 생성 실패, 제목 카드 사용 ({static_exc})")
                    fallback_reason = f"flow: {exc}; static: {static_exc}"
                else:
                    print_status("trip intro: 정적 모자이크 fallback 사용")
                    return piece, mosaic_report(piece, "static", f"flow: {exc}")
            else:
                print_status(f"trip intro: 정적 모자이크 생성 실패, 제목 카드 사용 ({exc})")
                fallback_reason = str(exc)
    elif requested_style == "mosaic":
        print_status("trip intro: 사용할 대표 프레임이 없어 제목 카드 사용")
        fallback_reason = "usable representative frame not found"
    else:
        fallback_reason = None

    piece = render_card_piece(
        cards_dir,
        "trip-intro",
        title,
        subtitle,
        duration,
        width,
        height,
        fps,
        encoder,
        bitrate,
        config,
        force,
    )
    report: dict[str, Any] = {
        "requested_style": requested_style,
        "effective_style": "card",
        "grid": [configured_mosaic_grid_size(config["render"])] * 2,
        "frame_count": 0,
        "tile_count": 0,
        "video_frame_count": int(round(piece.duration * fps)),
        "requested_duration": duration,
        "rendered_duration": piece.duration,
        "motion": "static",
        "candidate_ids": [],
        "frames_per_day": {},
    }
    if fallback_reason:
        report["fallback_reason"] = fallback_reason
    return piece, report


def mosaic_frames_per_day(selected: list[tuple[Candidate, Path]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for candidate, _path in selected:
        key = str(candidate.travel_day)
        counts[key] = counts.get(key, 0) + 1
    return counts


def select_trip_intro_frames(
    episodes: list[Episode],
    candidate_by_id: dict[str, Candidate],
    project_root: Path,
    configured_ids: Any = None,
    *,
    limit: int = MOSAIC_CAPACITY,
) -> list[tuple[Candidate, Path]]:
    if limit <= 0:
        return []
    ordered_episodes = sorted(episodes, key=lambda item: (item.travel_day, item.day_key))
    configured = [value.strip() for value in configured_ids if isinstance(value, str)] if isinstance(configured_ids, list) else []
    configured_rank = {value: index for index, value in enumerate(configured) if isinstance(value, str)}
    ranked_by_episode: list[list[Candidate]] = []
    candidate_plan_order: dict[str, tuple[int, int]] = {}
    for episode_index, episode in enumerate(ordered_episodes):
        episode_candidates: list[Candidate] = []
        seen: set[str] = set()
        for segment_index, segment in enumerate(episode.segments):
            candidate = candidate_by_id.get(segment.candidate_id)
            if (
                candidate is None
                or candidate.candidate_id in seen
                or candidate.travel_day != episode.travel_day
                or candidate.day_key != episode.day_key
            ):
                continue
            seen.add(candidate.candidate_id)
            episode_candidates.append(candidate)
            candidate_plan_order.setdefault(candidate.candidate_id, (episode_index, segment_index))
        ranked = sorted(episode_candidates, key=lambda candidate: mosaic_candidate_sort_key(candidate, configured_rank))
        ranked_by_episode.append(ranked)

    frame_cache: dict[str, Path | None] = {}

    def frame_for(candidate: Candidate) -> Path | None:
        if candidate.candidate_id not in frame_cache:
            frame_cache[candidate.candidate_id] = usable_candidate_frame(project_root, candidate)
        return frame_cache[candidate.candidate_id]

    coverage_by_episode: dict[int, tuple[Candidate, Path]] = {}
    for episode_index, ranked in enumerate(ranked_by_episode):
        for candidate in ranked:
            frame = frame_for(candidate)
            if frame is not None:
                coverage_by_episode[episode_index] = (candidate, frame)
                break

    usable_episode_indices = list(coverage_by_episode)
    if len(usable_episode_indices) > limit:
        selected_episode_indices = [
            usable_episode_indices[position]
            for position in evenly_spaced_indices(len(usable_episode_indices), limit)
        ]
    else:
        selected_episode_indices = usable_episode_indices

    selected: list[tuple[Candidate, Path]] = []
    selected_ids: set[str] = set()
    selected_clip_ids: set[str] = set()

    def add_candidate(candidate: Candidate) -> bool:
        if candidate.candidate_id in selected_ids or len(selected) >= limit:
            return False
        frame = frame_for(candidate)
        if frame is None:
            return False
        selected_ids.add(candidate.candidate_id)
        selected_clip_ids.add(candidate.clip_id)
        selected.append((candidate, frame))
        return True

    def next_from_episode(episode_index: int, *, unseen_clip_only: bool) -> Candidate | None:
        for candidate in ranked_by_episode[episode_index]:
            if (
                candidate.candidate_id not in selected_ids
                and (not unseen_clip_only or candidate.clip_id not in selected_clip_ids)
                and frame_for(candidate) is not None
            ):
                return candidate
        return None

    # First guarantee broad coverage: one usable frame from every sampled DAY.
    for episode_index in selected_episode_indices:
        candidate, _frame = coverage_by_episode[episode_index]
        add_candidate(candidate)

    # Fill remaining cells in balanced DAY rounds. Prefer a new source clip for
    # every tile so the overview shows more of the trip; repeat a clip only when
    # there are not enough distinct plan-selected clips to fill the grid.
    def fill_rounds(*, unseen_clip_only: bool) -> None:
        while len(selected) < limit:
            next_by_episode = {
                index: candidate
                for index in selected_episode_indices
                if (candidate := next_from_episode(index, unseen_clip_only=unseen_clip_only)) is not None
            }
            available_indices = list(next_by_episode)
            if not next_by_episode:
                break
            remaining = limit - len(selected)
            if remaining >= len(available_indices):
                round_indices = available_indices
            else:
                reviewed_indices = sorted(
                    (
                        index
                        for index in available_indices
                        if next_by_episode[index].candidate_id in configured_rank
                    ),
                    key=lambda index: configured_rank[next_by_episode[index].candidate_id],
                )
                round_indices = reviewed_indices[:remaining]
                slots = remaining - len(round_indices)
                if slots:
                    reviewed_set = set(round_indices)
                    automatic_indices = [index for index in available_indices if index not in reviewed_set]
                    positions = evenly_spaced_indices(len(automatic_indices), slots)
                    round_indices.extend(automatic_indices[position] for position in positions)
                round_indices.sort()
            added = sum(1 for episode_index in round_indices if add_candidate(next_by_episode[episode_index]))
            if added == 0:
                break

    fill_rounds(unseen_clip_only=True)
    fill_rounds(unseen_clip_only=False)

    return sorted(
        selected,
        key=lambda item: (
            candidate_plan_order.get(item[0].candidate_id, (len(ordered_episodes), 0)),
            item[0].candidate_id,
        ),
    )


def mosaic_candidate_sort_key(
    candidate: Candidate,
    configured_rank: dict[str, int],
) -> tuple[int, int, float, float, float, str]:
    return (
        0 if candidate.candidate_id in configured_rank else 1,
        configured_rank.get(candidate.candidate_id, len(configured_rank)),
        -candidate.visual_quality,
        -candidate.score,
        candidate_capture_timestamp(candidate),
        candidate.candidate_id,
    )


def evenly_spaced_indices(count: int, take: int) -> list[int]:
    if count <= 0 or take <= 0:
        return []
    if take >= count:
        return list(range(count))
    if take == 1:
        return [count // 2]
    return [index * (count - 1) // (take - 1) for index in range(take)]


def candidate_capture_timestamp(candidate: Candidate) -> float:
    try:
        captured_at = datetime.fromisoformat(candidate.captured_at.replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=timezone.utc)
    return captured_at.astimezone(timezone.utc).timestamp()


def usable_candidate_frame(project_root: Path, candidate: Candidate) -> Path | None:
    if not candidate.frame_path:
        return None
    root = project_root.resolve()
    try:
        frame = (root / candidate.frame_path).resolve()
        frame.relative_to(root)
        if not frame.is_file():
            return None
        with Image.open(frame) as source:
            oriented = ImageOps.exif_transpose(source)
            try:
                with oriented.convert("RGB") as decoded:
                    decoded.load()
            finally:
                if oriented is not source:
                    oriented.close()
        return frame
    except (OSError, ValueError):
        return None


def render_mosaic_card_piece(
    cards_dir: Path,
    card_id: str,
    title: str,
    subtitle: str,
    duration: float,
    selected_frames: list[tuple[Candidate, Path]],
    width: int,
    height: int,
    fps: int,
    encoder: str,
    bitrate: str,
    config: dict[str, Any],
    force: bool,
) -> Piece:
    grid_size = configured_mosaic_grid_size(config["render"])
    animation = str(config["render"].get("trip_intro_animation", "flow"))
    frame_count, effective_duration = source_output_timing(duration, fps)
    frame_records = [
        {
            "candidate_id": candidate.candidate_id,
            "path": str(path),
            "fingerprint": file_fingerprint(path),
        }
        for candidate, path in selected_frames
    ]
    key = stable_hash(
        {
            "version": MOSAIC_CARD_POLICY_VERSION,
            "id": card_id,
            "title": title,
            "subtitle": subtitle,
            "duration": effective_duration,
            "frame_count": frame_count,
            "grid": [grid_size, grid_size],
            "animation": animation,
            "animation_policy": ANIMATED_MOSAIC_POLICY_VERSION if animation == "flow" else None,
            "frames": frame_records,
            "format": [width, height, fps, encoder, bitrate],
            "audio_bitrate": str(config["render"].get("audio_bitrate", "192k")),
            "font_signature": render_font_signature(config),
        },
        length=28,
    )
    png = cards_dir / f"{key}.png"
    output = cards_dir / f"{key}.mp4"
    if animation == "flow":
        result = render_animated_mosaic(
            output,
            [path for _candidate, path in selected_frames],
            title,
            subtitle,
            duration,
            width,
            height,
            fps,
            encoder,
            bitrate,
            str(config["render"].get("audio_bitrate", "192k")),
            grid_size=grid_size,
            animation_style=animation,
            font_file=str(config["render"].get("font_file", "")),
            force=force,
        )
        return Piece(output, result.duration, title)
    if not force and cached_piece_is_usable(
        output,
        width,
        height,
        expected_frame_count=frame_count,
        expected_duration=effective_duration,
        expected_fps=fps,
    ):
        return Piece(output, effective_duration, title)
    create_mosaic_card(
        png,
        [path for _candidate, path in selected_frames],
        title,
        subtitle,
        width,
        height,
        config,
        columns=grid_size,
        rows=grid_size,
    )
    render_still_image_piece(
        png,
        output,
        card_id,
        effective_duration,
        width,
        height,
        fps,
        encoder,
        bitrate,
        config,
    )
    return Piece(output, effective_duration, title)


def render_source_piece(
    segment: PlanSegment,
    candidate: Candidate,
    clip: Clip,
    segments_dir: Path,
    overlays_dir: Path,
    width: int,
    height: int,
    fps: int,
    encoder: str,
    bitrate: str,
    config: dict[str, Any],
    *,
    location_overlay: str | None,
    fade_in: bool,
    fade_out: bool,
    force: bool,
    coalesced_selections: tuple[SourceSelection, ...] = (),
    member_labels: tuple[str, ...] = (),
    legacy_segments_dirs: tuple[Path, ...] = (),
) -> Piece:
    selections = coalesced_selections or (SourceSelection(candidate, segment),)
    if (
        selections[0].candidate.candidate_id != candidate.candidate_id
        or selections[0].segment != segment
        or any(selection.candidate.clip_id != clip.clip_id for selection in selections)
        or any(
            not source_selections_are_contiguous(previous, current)
            for previous, current in zip(selections, selections[1:])
        )
    ):
        raise VideoSummaryError("서로 호환되지 않는 원본 구간은 하나의 렌더 조각으로 합칠 수 없습니다.")
    if member_labels and len(member_labels) != len(selections):
        raise VideoSummaryError("합친 원본 구간의 타임라인 레이블 수가 일치하지 않습니다.")
    source_start = selections[0].candidate.start
    source_end = max(selection.candidate.end for selection in selections)
    frame_count, output_duration = source_output_timing((source_end - source_start) / segment.speed, fps)
    video_start_delay, audio_start_delay = source_stream_start_delays(clip, source_start, segment.speed)
    labels = member_labels or tuple(
        location_overlay if index == 0 and location_overlay else selection.segment.role
        for index, selection in enumerate(selections)
    )
    source_members = tuple(
        SourceMember(
            selection.candidate,
            selection.segment,
            (selection.candidate.start - source_start) / segment.speed,
            labels[index],
        )
        for index, selection in enumerate(selections)
    )
    transition = source_transition_seconds(config, output_duration)
    transition_in = transition if fade_in else 0.0
    transition_out = transition if fade_out else 0.0
    key = stable_hash(
        {
            "version": SOURCE_RENDER_POLICY_VERSION,
            "clip": clip.fingerprint,
            "source": [source_start, source_end, clip.has_audio],
            "members": [
                [member.candidate.candidate_id, member.candidate.start, member.candidate.end]
                for member in source_members
            ],
            "speed": segment.speed,
            "transitions": [transition_in, transition_out],
            "stream_start_delays": [video_start_delay, audio_start_delay],
            "location_overlay": location_overlay,
            "format": [width, height, fps, encoder, bitrate],
        },
        length=28,
    )
    output = segments_dir / f"{key}.mp4"
    if not force and cached_piece_is_usable(
        output,
        width,
        height,
        expected_frame_count=frame_count,
        expected_duration=output_duration,
        expected_fps=fps,
    ):
        return Piece(
            output,
            output_duration,
            source_members[0].label,
            candidate,
            segment,
            source_members=source_members,
        )
    # Earlier source artifacts can be one frame short and have no transition,
    # stream-offset, SAR, or contiguous-range contract, so v7 does not import them.

    args = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-threads", "2",
        "-ss", f"{source_start:.3f}", "-i", clip.path,
    ]
    overlay_input: int | None = None
    if location_overlay:
        overlay = overlays_dir / f"location-{stable_hash([location_overlay, width, height], 18)}.png"
        if force or not overlay.exists():
            create_lower_third(overlay, location_overlay, width, height, config)
        args.extend(["-loop", "1", "-framerate", str(fps), "-i", str(overlay)])
        overlay_input = 1
    silence_input: int | None = None
    if not clip.has_audio:
        silence_input = 2 if overlay_input is not None else 1
        args.extend(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"])

    video_pad = max(0.1, 3.0 / fps)
    video_start_pad = (
        f"tpad=start_mode=add:start_duration={video_start_delay:.6f}:color=0x101318,"
        if video_start_delay > 0.0
        else ""
    )
    filters = [
        f"[0:v:0]setpts=(PTS-STARTPTS)/{segment.speed:.6f},"
        "scale=w='max(2,trunc(iw*sar/2)*2)':h=ih,setsar=1,"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x101318,"
        "setsar=1,"
        f"{video_start_pad}"
        f"fps=fps={fps}:start_time=0:round=near,tpad=stop_mode=clone:stop_duration={video_pad:.6f},"
        f"trim=end_frame={frame_count},setpts=N/({fps}*TB),format=yuv420p[vbase]"
    ]
    if overlay_input is not None:
        filters.append(
            f"[{overlay_input}:v:0]format=rgba[overlay];"
            "[vbase][overlay]overlay=0:0:enable='between(t,0,2.8)':eof_action=pass[vcontent]"
        )
    else:
        filters.append("[vbase]null[vcontent]")
    video_transition_filters: list[str] = []
    if transition_in > 0:
        video_transition_filters.append(f"fade=t=in:st=0:d={transition_in:.6f}:color=black")
    if transition_out > 0:
        video_fade_out_start = max(0.0, (frame_count - 1) / fps - transition_out)
        video_transition_filters.append(
            f"fade=t=out:st={video_fade_out_start:.6f}:d={transition_out:.6f}:color=black"
        )
    if video_transition_filters:
        filters.append(f"[vcontent]{','.join(video_transition_filters)}[vout]")
    else:
        filters.append("[vcontent]null[vout]")
    audio_transition_parts: list[str] = []
    if transition_in > 0:
        audio_transition_parts.append(f"afade=t=in:st=0:d={transition_in:.6f}")
    if transition_out > 0:
        audio_transition_parts.append(
            f"afade=t=out:st={output_duration - transition_out:.6f}:d={transition_out:.6f}"
        )
    audio_transition = "," + ",".join(audio_transition_parts) if audio_transition_parts else ""
    if clip.has_audio:
        audio_start_pad = (
            f"adelay={audio_start_delay * 1000.0:.3f}:all=1,"
            if audio_start_delay > 0.0
            else ""
        )
        filters.append(
            f"[0:a:0]asetpts=PTS-STARTPTS,aresample=48000,"
            f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            f"atempo={segment.speed:.6f},{audio_start_pad}"
            "loudnorm=I=-16:LRA=11:TP=-1.5,"
            # Some FFmpeg loudnorm builds emit non-finite floats for digital silence.
            # Quantizing once prevents those values from reaching the AAC encoder.
            "aresample=48000:osf=s16,"
            "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            f"apad,atrim=duration={output_duration:.6f}"
            f"{audio_transition},asetpts=PTS-STARTPTS[aout]"
        )
    else:
        assert silence_input is not None
        filters.append(
            f"[{silence_input}:a:0]atrim=duration={output_duration:.6f},"
            "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
            f"{audio_transition},asetpts=PTS-STARTPTS[aout]"
        )
    temporary = output.with_name(f".{output.stem}.partial.mp4")
    args.extend(
        [
            "-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[aout]",
            *video_encode_args(encoder, bitrate),
            "-c:a", "aac", "-b:a", str(config["render"].get("audio_bitrate", "192k")),
            "-ar", "48000", "-ac", "2", "-video_track_timescale", "90000",
            "-map_metadata", "-1", "-map_chapters", "-1",
            "-movflags", "+faststart", "-y", str(temporary),
        ]
    )
    run_command(args)
    if not cached_piece_is_usable(
        temporary,
        width,
        height,
        expected_frame_count=frame_count,
        expected_duration=output_duration,
        expected_fps=fps,
    ):
        temporary.unlink(missing_ok=True)
        candidate_ids = ", ".join(member.candidate.candidate_id for member in source_members)
        raise VideoSummaryError(f"렌더 조각의 프레임 수나 길이가 예상과 다릅니다: {candidate_ids}")
    os.replace(temporary, output)
    return Piece(
        output,
        output_duration,
        source_members[0].label,
        candidate,
        segment,
        source_members=source_members,
    )


def source_output_timing(duration: float, fps: int) -> tuple[int, float]:
    frame_count = max(1, int(math.floor(max(0.0, duration) * fps + 0.5)))
    return frame_count, frame_count / fps


def source_stream_start_delays(clip: Clip, source_start: float, speed: float) -> tuple[float, float]:
    video_start, audio_start = source_stream_relative_starts(clip.path, clip.fingerprint)
    return (
        max(0.0, video_start - source_start) / speed,
        max(0.0, audio_start - source_start) / speed,
    )


@lru_cache(maxsize=256)
def source_stream_relative_starts(path: str, fingerprint: str) -> tuple[float, float]:
    del fingerprint  # The fingerprint makes the memoized probe content-sensitive.
    try:
        probe = probe_media(Path(path))
    except (OSError, VideoSummaryError):
        return (0.0, 0.0)
    streams = probe.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    video_start = finite_number(video.get("start_time")) if video else None
    audio_start = finite_number(audio.get("start_time")) if audio else None
    format_start = finite_number(probe.get("format", {}).get("start_time"))
    available = [value for value in (video_start, audio_start) if value is not None]
    origin = format_start if format_start is not None else (min(available) if available else 0.0)
    return (
        max(0.0, (video_start if video_start is not None else origin) - origin),
        max(0.0, (audio_start if audio_start is not None else origin) - origin),
    )


def source_transition_seconds(config: dict[str, Any], output_duration: float) -> float:
    requested = max(0.0, float(config["render"].get("transition_seconds", 0.18)))
    return min(requested, max(0.0, output_duration) / 3.0)


def positive_number(value: Any) -> float | None:
    parsed = finite_number(value)
    return parsed if parsed is not None and parsed > 0.0 else None


def finite_number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def positive_ratio(value: Any) -> float | None:
    raw = str(value or "").strip()
    if not raw or raw == "N/A":
        return None
    numerator, separator, denominator = raw.partition("/")
    try:
        parsed = float(numerator) / float(denominator) if separator else float(numerator)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0.0 else None


def stream_frame_rate(stream: dict[str, Any]) -> float | None:
    for key in ("r_frame_rate", "avg_frame_rate"):
        if (rate := positive_ratio(stream.get(key))) is not None:
            return rate
    return None


def stream_duration(stream: dict[str, Any], *, frame_rate: float | None = None) -> float | None:
    if (duration := positive_number(stream.get("duration"))) is not None:
        return duration
    duration_ts = positive_number(stream.get("duration_ts"))
    time_base = positive_ratio(stream.get("time_base"))
    if duration_ts is not None and time_base is not None:
        return duration_ts * time_base
    if stream.get("codec_type") == "video":
        frames = positive_number(stream.get("nb_frames"))
        rate = frame_rate or stream_frame_rate(stream)
        if frames is not None and rate is not None:
            return frames / rate
    return None


def audio_tail_tolerance(audio: dict[str, Any], frame_duration: float) -> float:
    sample_rate = positive_number(audio.get("sample_rate")) or DEFAULT_AUDIO_SAMPLE_RATE
    return AAC_FRAME_SAMPLES / sample_rate + frame_duration


def cached_piece_is_usable(
    path: Path,
    width: int,
    height: int,
    *,
    expected_frame_count: int | None = None,
    expected_duration: float | None = None,
    expected_fps: int | None = None,
) -> bool:
    try:
        if not path.is_file() or path.stat().st_size <= 1024:
            return False
        probe = probe_media(path)
        video = next((item for item in probe.get("streams", []) if item.get("codec_type") == "video"), None)
        audio = next((item for item in probe.get("streams", []) if item.get("codec_type") == "audio"), None)
        if (
            not video
            or not audio
            or int(video.get("width", 0)) != width
            or int(video.get("height", 0)) != height
        ):
            return False
        frame_rate = stream_frame_rate(video)
        video_duration = stream_duration(video, frame_rate=frame_rate)
        audio_duration = stream_duration(audio)
        format_duration = positive_number(probe.get("format", {}).get("duration"))
        if video_duration is None and audio_duration is None and format_duration is None:
            return False
        if expected_frame_count is not None:
            raw_frame_count = video.get("nb_frames")
            if raw_frame_count in {None, "", "N/A"} or int(raw_frame_count) != expected_frame_count:
                return False
        if expected_fps is not None:
            if frame_rate is None or abs(frame_rate - expected_fps) > 0.001:
                return False
        if expected_duration is not None:
            if video_duration is None:
                return False
            if expected_fps is not None:
                frame_duration = 1.0 / expected_fps
            elif expected_frame_count is not None:
                frame_duration = expected_duration / max(1, expected_frame_count)
            else:
                frame_duration = 1.0 / 30.0
            if abs(video_duration - expected_duration) > max(0.002, frame_duration / 3.0):
                return False
            mux_tolerance = audio_tail_tolerance(audio, frame_duration)
            if audio_duration is not None and abs(audio_duration - expected_duration) > mux_tolerance:
                return False
            if format_duration is not None and abs(format_duration - expected_duration) > mux_tolerance:
                return False
        return True
    except (OSError, TypeError, ValueError, VideoSummaryError):
        return False


def import_cached_piece(source: Path, output: Path, width: int, height: int) -> bool:
    if not cached_piece_is_usable(source, width, height):
        return False
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}.import.partial.mp4")
    temporary.unlink(missing_ok=True)
    try:
        try:
            os.link(source, temporary)
        except OSError:
            shutil.copy2(source, temporary)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def render_card_piece(
    cards_dir: Path,
    card_id: str,
    title: str,
    subtitle: str,
    duration: float,
    width: int,
    height: int,
    fps: int,
    encoder: str,
    bitrate: str,
    config: dict[str, Any],
    force: bool,
) -> Piece:
    frame_count, effective_duration = source_output_timing(duration, fps)
    key = stable_hash(
        {
            "version": CARD_RENDER_POLICY_VERSION, "id": card_id, "title": title, "subtitle": subtitle,
            "duration": effective_duration, "frame_count": frame_count,
            "format": [width, height, fps, encoder, bitrate],
        },
        length=28,
    )
    png = cards_dir / f"{key}.png"
    output = cards_dir / f"{key}.mp4"
    if not force and cached_piece_is_usable(
        output,
        width,
        height,
        expected_frame_count=frame_count,
        expected_duration=effective_duration,
        expected_fps=fps,
    ):
        return Piece(output, effective_duration, title)
    create_card(png, title, subtitle, width, height, config)
    render_still_image_piece(
        png,
        output,
        card_id,
        effective_duration,
        width,
        height,
        fps,
        encoder,
        bitrate,
        config,
    )
    return Piece(output, effective_duration, title)


def render_still_image_piece(
    png: Path,
    output: Path,
    card_id: str,
    duration: float,
    width: int,
    height: int,
    fps: int,
    encoder: str,
    bitrate: str,
    config: dict[str, Any],
) -> None:
    frame_count, effective_duration = source_output_timing(duration, fps)
    fade = card_fade_seconds(card_id, effective_duration)
    temporary = output.with_name(f".{output.stem}.partial.mp4")
    run_command(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-threads", "2",
            "-loop", "1", "-framerate", str(fps), "-i", str(png),
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-t", f"{effective_duration:.6f}",
            "-vf", f"fps={fps},format=yuv420p,fade=t=in:st=0:d={fade:.3f},"
            f"fade=t=out:st={max(0.0, effective_duration - fade):.6f}:d={fade:.3f},"
            f"trim=end_frame={frame_count},setpts=N/({fps}*TB)",
            "-af", f"atrim=duration={effective_duration:.6f},asetpts=PTS-STARTPTS",
            "-frames:v", str(frame_count),
            *video_encode_args(encoder, bitrate),
            "-c:a", "aac", "-b:a", str(config["render"].get("audio_bitrate", "192k")),
            "-ar", "48000", "-ac", "2", "-video_track_timescale", "90000",
            "-movflags", "+faststart", "-y", str(temporary),
        ]
    )
    if not cached_piece_is_usable(
        temporary,
        width,
        height,
        expected_frame_count=frame_count,
        expected_duration=effective_duration,
        expected_fps=fps,
    ):
        temporary.unlink(missing_ok=True)
        raise VideoSummaryError(f"카드 렌더의 프레임 수나 길이가 예상과 다릅니다: {card_id}")
    os.replace(temporary, output)


def card_fade_seconds(card_id: str, duration: float) -> float:
    cap = 0.5 if card_id.startswith("date-") else 0.65
    return min(cap, max(0.0, duration) / 4.0)


def video_encode_args(encoder: str, bitrate: str) -> list[str]:
    if encoder == "h264_videotoolbox":
        return ["-c:v", encoder, "-allow_sw", "1", "-b:v", bitrate, "-pix_fmt", "yuv420p", "-tag:v", "avc1"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-tag:v", "avc1"]


def validate_assembled_media(
    probe: dict[str, Any],
    output: Path,
    *,
    width: int,
    height: int,
    expected_duration: float,
    expected_fps: int,
) -> float:
    video = next((stream for stream in probe.get("streams", []) if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in probe.get("streams", []) if stream.get("codec_type") == "audio"), None)
    if not video or not audio:
        raise VideoSummaryError(f"완성 영상의 스트림 검증에 실패했습니다: {output}")
    if int(video.get("width", 0)) != width or int(video.get("height", 0)) != height:
        raise VideoSummaryError(f"완성 영상 해상도가 예상과 다릅니다: {output}")

    frame_duration = 1.0 / max(1, expected_fps)
    video_duration = stream_duration(video, frame_rate=stream_frame_rate(video))
    audio_duration = stream_duration(audio)
    format_duration = positive_number(probe.get("format", {}).get("duration"))
    if video_duration is None:
        raise VideoSummaryError(f"완성 영상의 재생 시간을 확인할 수 없습니다: {output}")
    if abs(video_duration - expected_duration) > frame_duration + 1e-6:
        raise VideoSummaryError(
            f"완성 영상 길이가 타임라인과 1프레임 이상 다릅니다: {output} "
            f"(예상 {expected_duration:.6f}초, 영상 {video_duration:.6f}초)"
        )
    checked_audio_duration = audio_duration or format_duration
    if checked_audio_duration is None:
        raise VideoSummaryError(f"완성 영상의 오디오 재생 시간을 확인할 수 없습니다: {output}")
    tolerance = audio_tail_tolerance(audio, frame_duration)
    if abs(video_duration - checked_audio_duration) > tolerance + 1e-6:
        raise VideoSummaryError(
            f"완성 영상의 오디오/비디오 길이 차이가 큽니다: {output} "
            f"(영상 {video_duration:.6f}초, 오디오 {checked_audio_duration:.6f}초)"
        )
    reported_duration = format_duration or max(
        duration for duration in (video_duration, audio_duration) if duration is not None
    )
    return reported_duration


def assemble_output(
    pieces: list[Piece],
    output: Path,
    episode: Episode,
    paths: ProjectPaths,
    config: dict[str, Any],
    assembly_dir: Path,
    width: int,
    height: int,
    draft: bool,
) -> dict[str, Any]:
    if not pieces:
        raise VideoSummaryError("조립할 영상 조각이 없습니다.")
    assembly_dir.mkdir(parents=True, exist_ok=True)
    linked: list[Path] = []
    for index, piece in enumerate(pieces):
        target = assembly_dir / f"piece-{index:04d}.mp4"
        target.unlink(missing_ok=True)
        try:
            os.link(piece.path, target)
        except OSError:
            shutil.copy2(piece.path, target)
        linked.append(target)
    concat_path = assembly_dir / "concat.txt"
    concat_lines = ["ffconcat version 1.0"]
    for piece, path in zip(pieces, linked):
        concat_lines.extend([f"file {path.name}", f"duration {piece.duration:.9f}"])
    atomic_write_text(concat_path, "\n".join(concat_lines) + "\n")
    output.parent.mkdir(parents=True, exist_ok=True)
    music_value = str(config["render"].get("music_file", "")).strip()
    expected_duration = sum(piece.duration for piece in pieces)
    candidate_output = output.with_name(f".{output.stem}.partial.mp4")
    assembled = assembly_dir / "assembled-audio-normalized.mp4" if music_value else candidate_output
    candidate_output.unlink(missing_ok=True)
    if assembled != candidate_output:
        assembled.unlink(missing_ok=True)
    try:
        run_command(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "1",
                "-i", concat_path.name,
                "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
                "-c:a", "aac", "-b:a", str(config["render"].get("audio_bitrate", "192k")),
                "-ar", "48000", "-ac", "2",
                "-af", (
                    # Compensate against concat-demuxer timestamps before rebuilding a
                    # continuous sample clock. Repacking decoded AAC directly with N/SR/TB
                    # drops each piece's priming/edit-list gap and makes audio run
                    # progressively ahead of video in long assemblies.
                    "aresample=48000:async=1:first_pts=0:min_hard_comp=0.0001,"
                    "asetpts=N/SR/TB,"
                    f"apad=whole_dur={expected_duration:.9f},atrim=duration={expected_duration:.9f}"
                ),
                "-movflags", "+faststart", "-y", str(assembled),
            ],
            cwd=assembly_dir,
        )
        if music_value:
            mix_music(assembled, candidate_output, pieces, config, music_value)

        probe = probe_media(candidate_output)
        duration = validate_assembled_media(
            probe,
            candidate_output,
            width=width,
            height=height,
            expected_duration=expected_duration,
            expected_fps=int(config["render"].get("fps", 30)),
        )

        metadata_base = output.with_suffix("")
        write_vtt(metadata_base.with_suffix(".vtt"), pieces, paths)
        timeline_path = metadata_base.with_suffix(".timeline.txt")
        write_timeline(timeline_path, pieces)
        chapters_path = metadata_base.with_suffix(".chapters.txt")
        if str(config["editing"].get("episode_mode", "daily")) == "trip":
            chapters = write_trip_day_chapters(chapters_path, pieces)
        else:
            chapters = write_chapters(chapters_path, pieces)
        description = (
            f"# {episode.title}\n\n{episode.summary}\n\n"
            + "\n".join(chapters)
            + "\n\n#여행브이로그 #여행\n"
        )
        atomic_write_text(metadata_base.with_suffix(".description.md"), description)
        os.replace(candidate_output, output)
    finally:
        candidate_output.unlink(missing_ok=True)
    return {
        "path": str(output),
        "duration": round(duration, 3),
        "size_bytes": output.stat().st_size,
        "title": episode.title,
        "description": str(metadata_base.with_suffix(".description.md")),
        "chapters": str(chapters_path),
        "timeline": str(timeline_path),
        "subtitles": str(metadata_base.with_suffix(".vtt")),
        "draft": draft,
    }


def mix_music(
    assembled: Path,
    output: Path,
    pieces: list[Piece],
    config: dict[str, Any],
    music_value: str,
) -> None:
    music = Path(music_value).expanduser().resolve()
    if not music.exists():
        raise VideoSummaryError(f"배경음악 파일이 없습니다: {music}")
    temporary = output.with_name(f".{output.stem}.partial.mp4")
    duration = sum(piece.duration for piece in pieces)
    volume = float(config["render"].get("music_volume", 0.08))
    filter_complex = (
        "[0:a]asplit=2[voice][side];"
        f"[1:a]aresample=48000,volume={volume:.4f},atrim=duration={duration:.3f},"
        f"afade=t=in:st=0:d=1,afade=t=out:st={max(0.0, duration - 2):.3f}:d=2[bg];"
        "[bg][side]sidechaincompress=threshold=0.035:ratio=8:attack=20:release=500[ducked];"
        "[voice][ducked]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-16:LRA=11:TP=-1.5[aout]"
    )
    run_command(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(assembled),
            "-stream_loop", "-1", "-i", str(music), "-filter_complex", filter_complex,
            "-map", "0:v:0", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac",
            "-b:a", str(config["render"].get("audio_bitrate", "192k")),
            "-t", f"{duration:.3f}", "-movflags", "+faststart", "-y", str(temporary),
        ]
    )
    os.replace(temporary, output)


VTT_BOUNDARY_TOLERANCE_SECONDS = 0.05


def append_vtt_cue(
    cues: list[tuple[float, float, str]],
    start: float,
    end: float,
    text: str,
    *,
    boundary_tolerance: float = VTT_BOUNDARY_TOLERANCE_SECONDS,
) -> None:
    normalized_text = text.strip()
    if end <= start or not normalized_text:
        return
    if cues:
        previous_start, previous_end, previous_text = cues[-1]
        boundary_gap = start - previous_end
        if (
            normalized_text == previous_text
            and start >= previous_start
            and boundary_gap <= boundary_tolerance
        ):
            cues[-1] = (previous_start, max(previous_end, end), previous_text)
            return
    cues.append((start, end, normalized_text))


def write_vtt(path: Path, pieces: list[Piece], paths: ProjectPaths) -> None:
    cues: list[tuple[float, float, str]] = []
    timeline = 0.0
    for piece in pieces:
        for member in piece_source_members(piece):
            candidate = member.candidate
            speed = member.segment.speed
            caption = effective_source_caption(member.segment, candidate)
            if caption:
                start = timeline + member.offset
                end = min(
                    timeline + piece.duration,
                    start + candidate.duration / speed,
                )
                append_vtt_cue(cues, start, end, caption)
                continue
            for cue in load_transcript(paths, candidate.clip_id):
                source_start = max(candidate.start, cue.start)
                source_end = min(candidate.end, cue.end)
                if source_end <= source_start or not cue.text.strip():
                    continue
                start = timeline + member.offset + (source_start - candidate.start) / speed
                end = min(
                    timeline + piece.duration,
                    timeline + member.offset + (source_end - candidate.start) / speed,
                )
                append_vtt_cue(cues, start, end, cue.text)
        timeline += piece.duration
    lines = ["WEBVTT", ""]
    for cue_index, (start, end, text) in enumerate(cues, start=1):
        lines.extend([str(cue_index), f"{vtt_time(start)} --> {vtt_time(end)}", text, ""])
    atomic_write_text(path, "\n".join(lines).rstrip() + "\n")


def write_chapters(path: Path, pieces: list[Piece]) -> list[str]:
    boundaries: list[tuple[int, str]] = []
    timeline = 0.0
    previous = ""
    previous_time = float("-inf")
    for piece in pieces:
        entries = piece_timeline_entries(piece)
        for offset, raw_label, _candidate in entries:
            entry_time = timeline + offset
            label = normalize_timeline_label(raw_label)
            if label and label != previous and (not boundaries or entry_time - previous_time >= 1.0):
                boundaries.append((int(entry_time), label))
                previous = label
                previous_time = entry_time
        timeline += piece.duration
    total_seconds = int(timeline)
    lines = youtube_chapter_lines(
        select_daily_youtube_boundaries(boundaries, total_seconds),
        total_seconds,
    )
    atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))
    return lines


def write_timeline(path: Path, pieces: list[Piece]) -> list[str]:
    lines: list[str] = []
    timeline = 0.0
    for piece in pieces:
        for offset, raw_label, candidate in piece_timeline_entries(piece):
            label = normalize_timeline_label(raw_label) or "untitled"
            if candidate is not None:
                label = f"{label} [{candidate.candidate_id}]"
            lines.append(f"{vtt_time(timeline + offset)} {label}")
        timeline += piece.duration
    atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))
    return lines


def piece_source_members(piece: Piece) -> tuple[SourceMember, ...]:
    if piece.source_members:
        return piece.source_members
    if piece.candidate is not None and piece.segment is not None:
        return (SourceMember(piece.candidate, piece.segment, 0.0, piece.label),)
    return ()


def piece_timeline_entries(piece: Piece) -> list[tuple[float, str, Candidate | None]]:
    members = piece_source_members(piece)
    if members:
        return [(member.offset, member.label, member.candidate) for member in members]
    return [(0.0, piece.label, None)]


def write_trip_day_chapters(path: Path, pieces: list[Piece]) -> list[str]:
    boundaries: list[tuple[int, str]] = []
    timeline = 0.0
    for piece in pieces:
        if piece.day_chapter:
            label = normalize_timeline_label(piece.day_chapter)
            if label:
                boundaries.append((int(timeline), label))
        timeline += piece.duration
    if boundaries:
        boundaries[0] = (0, boundaries[0][1])
    lines = youtube_chapter_lines(boundaries, int(timeline))
    atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))
    return lines


def youtube_chapter_lines(boundaries: list[tuple[int, str]], total_seconds: int) -> list[str]:
    valid = (
        len(boundaries) >= YOUTUBE_MIN_CHAPTERS
        and boundaries[0][0] == 0
        and all(
            current[0] - previous[0] >= YOUTUBE_MIN_CHAPTER_SECONDS
            for previous, current in zip(boundaries, boundaries[1:])
        )
        and total_seconds - boundaries[-1][0] >= YOUTUBE_MIN_CHAPTER_SECONDS
    )
    return [f"{chapter_time(seconds)} {label}" for seconds, label in boundaries] if valid else []


def select_daily_youtube_boundaries(
    boundaries: list[tuple[int, str]],
    total_seconds: int,
) -> list[tuple[int, str]]:
    if not boundaries or boundaries[0][0] != 0:
        return []
    selected = [boundaries[0]]
    for boundary in boundaries[1:]:
        if (
            boundary[0] - selected[-1][0] >= YOUTUBE_MIN_CHAPTER_SECONDS
            and total_seconds - boundary[0] >= YOUTUBE_MIN_CHAPTER_SECONDS
        ):
            selected.append(boundary)
    return selected


def normalize_timeline_label(value: str) -> str:
    return " ".join(value.split())
