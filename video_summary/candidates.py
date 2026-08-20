from __future__ import annotations

import math
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .media import analyze_visual_signals, extract_frame, load_clips
from .models import Candidate, Clip, TranscriptCue
from .project import ProjectPaths
from .state import StateStore
from .transcribe import load_transcript
from .utils import VideoSummaryError, file_fingerprint, print_status, read_json, stable_hash, unique_preserving_order, write_json


JOURNEY_WORDS = {
    "출발", "도착", "공항", "비행기", "기차", "버스", "택시", "렌터카", "이동", "체크인", "체크아웃",
    "숙소", "호텔", "리조트", "귀가", "집으로", "departure", "arrival", "airport", "train", "bus", "hotel",
}
FUN_WORDS = {
    "웃", "ㅋㅋ", "ㅎㅎ", "대박", "헐", "우와", "미쳤", "신나", "재밌", "최고", "놀라", "웃기",
    "haha", "wow", "amazing", "funny", "awesome",
}
FOOD_WORDS = {
    "맛", "먹", "식당", "조식", "점심", "저녁", "카페", "커피", "디저트", "간식", "음식", "메뉴",
    "breakfast", "lunch", "dinner", "cafe", "coffee", "food", "delicious",
}
SCENERY_WORDS = {
    "바다", "해변", "등산", "산 정상", "노을", "야경", "풍경", "전망", "거리", "시장", "수영장", "하늘", "구름",
    "beach", "mountain", "sunset", "view", "street", "market", "pool",
}
MAX_CANDIDATE_DURATION_SECONDS = 18.0


def build_candidates(
    paths: ProjectPaths,
    clips: list[Clip],
    config: dict[str, Any],
    *,
    force: bool = False,
) -> dict[str, Any]:
    analysis = config["analysis"]
    interval = float(analysis.get("sample_interval_seconds", 3.0))
    max_per_clip = int(analysis.get("max_candidates_per_clip", 8))
    cache_key = _candidate_cache_key(paths, clips, config)
    state = StateStore(paths.state)
    if not force and paths.candidates.exists() and state.is_complete("candidates", cache_key):
        print_status("candidates: 캐시 사용")
        return read_json(paths.candidates)

    state.mark_running("candidates", cache_key)
    try:
        candidates: list[Candidate] = []
        for index, clip in enumerate(clips, start=1):
            if clip.duration <= 0:
                continue
            print_status(f"candidates {index}/{len(clips)}: {Path(clip.path).name}")
            cues = load_transcript(paths, clip.clip_id)
            signals = analyze_visual_signals(paths, clip, interval, force=force)
            windows = _candidate_windows(clip, cues, signals, max_per_clip)
            for start, end, origin in windows:
                text = _window_transcript(cues, start, end)
                roles = _roles(text, clip, start, end, origin)
                motion, quality = _window_signals(signals, start, end)
                speech_duration = sum(
                    max(0.0, min(cue.end, end) - max(cue.start, start))
                    for cue in cues
                    if cue.end > start and cue.start < end
                )
                speech_ratio = min(1.0, speech_duration / max(0.1, end - start))
                location = _candidate_location(clip, text, config.get("locations", []))
                score = _score_candidate(roles, speech_ratio, motion, quality, start, end, clip.duration)
                candidate_id = "cand_" + stable_hash(
                    {
                        "clip_id": clip.clip_id,
                        "fingerprint": clip.fingerprint,
                        "start": round(start, 2),
                        "end": round(end, 2),
                    },
                    length=18,
                )
                frame_path = paths.frames / f"{candidate_id}.jpg"
                if force or not frame_path.exists():
                    extract_frame(clip, (start + end) / 2.0, frame_path)
                clip_captured_at = datetime.fromisoformat(clip.captured_at)
                captured_at = (
                    clip_captured_at.astimezone(timezone.utc)
                    + timedelta(seconds=start)
                ).astimezone(clip_captured_at.tzinfo).isoformat()
                candidates.append(
                    Candidate(
                        candidate_id=candidate_id,
                        clip_id=clip.clip_id,
                        day_key=clip.day_key,
                        travel_day=clip.travel_day,
                        start=round(start, 3),
                        end=round(end, 3),
                        captured_at=captured_at,
                        transcript=text,
                        roles=roles,
                        score=round(score, 5),
                        speech_ratio=round(speech_ratio, 5),
                        motion_score=round(motion, 5),
                        visual_quality=round(quality, 5),
                        location=location,
                        frame_path=str(frame_path.relative_to(paths.root)),
                    )
                )

        candidates.sort(key=lambda item: (_candidate_timestamp(item), item.candidate_id))
        if not candidates:
            raise VideoSummaryError("편집 후보를 만들지 못했습니다.")
        payload = {
            "version": 1,
            "project": config["project"]["name"],
            "cache_key": cache_key,
            "candidate_set_hash": stable_hash([candidate.to_dict() for candidate in candidates], length=32),
            "count": len(candidates),
            "days": _day_summary(candidates),
            "candidates": [candidate.to_dict() for candidate in candidates],
        }
        write_json(paths.candidates, payload)
        state.mark_complete("candidates", cache_key, {"candidate_count": len(candidates)})
        return payload
    except BaseException as exc:
        state.mark_failed("candidates", cache_key, str(exc))
        raise


def load_candidates(paths: ProjectPaths, config: dict[str, Any] | None = None) -> list[Candidate]:
    if not paths.candidates.exists():
        raise VideoSummaryError("먼저 analyze 또는 run을 실행하세요.")
    payload = read_json(paths.candidates)
    if config is not None:
        clips = load_clips(paths, config)
        if payload.get("cache_key") != _candidate_cache_key(paths, clips, config):
            raise VideoSummaryError("분석 설정이나 전사가 변경되었습니다. analyze를 다시 실행하세요.")
    return [Candidate.from_dict(item) for item in payload.get("candidates", [])]


def _candidate_cache_key(paths: ProjectPaths, clips: list[Clip], config: dict[str, Any]) -> str:
    analysis = config["analysis"]
    transcript_keys: list[dict[str, str | None] | None] = []
    for clip in clips:
        path = paths.transcripts / f"{clip.clip_id}.json"
        if path.exists():
            transcript_keys.append(
                {
                    "cache_key": read_json(path).get("cache_key"),
                    "fingerprint": file_fingerprint(path),
                }
            )
        else:
            transcript_keys.append(None)
    return stable_hash(
        {
            "version": 8,
            "project": config["project"]["name"],
            "clips": [
                (
                    clip.clip_id,
                    clip.fingerprint,
                    clip.captured_at,
                    clip.day_key,
                    clip.travel_day,
                    clip.location,
                )
                for clip in clips
            ],
            "transcripts": transcript_keys,
            "transcription_settings": {
                "language": config["project"].get("language", "auto"),
                "asr_backend": analysis.get("asr_backend", "auto"),
                "asr_model": analysis.get("asr_model", "small"),
                "whisper_cpp_model": analysis.get("whisper_cpp_model") or os.environ.get("WHISPER_CPP_MODEL"),
                "whisper_cpp_vad_model": analysis.get("whisper_cpp_vad_model") or os.environ.get("WHISPER_CPP_VAD_MODEL"),
                "offline": analysis.get("offline", False),
            },
            "interval": float(analysis.get("sample_interval_seconds", 3.0)),
            "max_per_clip": int(analysis.get("max_candidates_per_clip", 8)),
            "locations": config.get("locations", []),
        }
    )


def _candidate_windows(
    clip: Clip,
    cues: list[TranscriptCue],
    signals: list[dict[str, float]],
    max_per_clip: int,
) -> list[tuple[float, float, str]]:
    windows: list[tuple[float, float, str]] = []
    for group in _group_cues(cues):
        start = max(0.0, group[0].start - 0.35)
        end = min(clip.duration, group[-1].end + 0.75)
        start, end = _ensure_duration(
            start,
            end,
            clip.duration,
            minimum=2.5,
            maximum=MAX_CANDIDATE_DURATION_SECONDS,
        )
        _append_window(windows, (start, end, "speech"))

    boundary_duration = min(6.0, clip.duration)
    if boundary_duration >= 0.75:
        windows.append((0.0, boundary_duration, "opener"))
    if clip.duration > 7:
        windows.append((max(0.0, clip.duration - boundary_duration), clip.duration, "closer"))

    ranked_signals = sorted(
        signals,
        key=lambda sample: (
            float(sample.get("motion", 0.0)) * 0.60
            + float(sample.get("contrast", 0.0)) * 0.25
            + _brightness_quality(float(sample.get("brightness", 0.5))) * 0.15
        ),
        reverse=True,
    )
    for sample in ranked_signals[: max(6, max_per_clip * 3)]:
        center = float(sample.get("time", 0.0))
        duration = min(7.0, clip.duration)
        start = max(0.0, min(clip.duration - duration, center - duration / 2.0))
        _append_window(windows, (start, min(clip.duration, start + duration), "visual"))
    if not windows:
        windows.append((0.0, min(clip.duration, 6.0), "visual"))
    return _merge_overlapping_windows(_select_windows(windows, clip, cues, signals, max(1, max_per_clip)))


def _merge_overlapping_windows(
    windows: list[tuple[float, float, str]],
    *,
    tolerance: float = 0.001,
    maximum: float = MAX_CANDIDATE_DURATION_SECONDS,
) -> list[tuple[float, float, str]]:
    """Preserve the selected union as disjoint candidates without creating long takes."""
    components: list[list[tuple[float, float, str]]] = []
    for start, end, origin in sorted(windows, key=lambda item: (item[0], item[1])):
        if not components or start > max(item[1] for item in components[-1]) + tolerance:
            components.append([(start, end, origin)])
            continue
        components[-1].append((start, end, origin))

    merged: list[tuple[float, float, str]] = []
    for component in components:
        component_start = min(item[0] for item in component)
        component_end = max(item[1] for item in component)
        duration = component_end - component_start
        part_count = max(1, math.ceil(duration / maximum))
        for part_index in range(part_count):
            start = component_start + duration * part_index / part_count
            end = component_start + duration * (part_index + 1) / part_count
            origin = _merged_window_origin(component, start, end, part_index, part_count)
            merged.append((start, end, origin))
    return merged


def _merged_window_origin(
    component: list[tuple[float, float, str]],
    start: float,
    end: float,
    part_index: int,
    part_count: int,
) -> str:
    if part_index == 0 and any(origin == "opener" for _, _, origin in component):
        return "opener"
    if part_index == part_count - 1 and any(origin == "closer" for _, _, origin in component):
        return "closer"
    priority = {"speech": 3, "visual": 2, "opener": 1, "closer": 1}
    return max(
        component,
        key=lambda item: (
            max(0.0, min(end, item[1]) - max(start, item[0])),
            priority.get(item[2], 0),
        ),
    )[2]


def _select_windows(
    windows: list[tuple[float, float, str]],
    clip: Clip,
    cues: list[TranscriptCue],
    signals: list[dict[str, float]],
    limit: int,
) -> list[tuple[float, float, str]]:
    if len(windows) <= limit:
        return sorted(windows, key=lambda item: (item[0], item[1]))

    def rank(window: tuple[float, float, str]) -> float:
        start, end, origin = window
        text = _window_transcript(cues, start, end)
        roles = _roles(text, clip, start, end, origin)
        motion, quality = _window_signals(signals, start, end)
        speech = sum(
            max(0.0, min(cue.end, end) - max(cue.start, start))
            for cue in cues
            if cue.end > start and cue.start < end
        ) / max(0.1, end - start)
        return _score_candidate(roles, min(1.0, speech), motion, quality, start, end, clip.duration)

    selected: list[tuple[float, float, str]] = []
    if limit >= 2:
        opener = next((item for item in windows if item[2] == "opener"), None)
        closer = next((item for item in windows if item[2] == "closer"), None)
        if opener:
            selected.append(opener)
        if closer and closer not in selected:
            selected.append(closer)
    remaining = [item for item in windows if item not in selected]
    while len(selected) < limit and remaining:
        def coverage_rank(window: tuple[float, float, str]) -> tuple[float, float]:
            center = (window[0] + window[1]) / 2.0
            distance = min((abs(center - (item[0] + item[1]) / 2.0) for item in selected), default=clip.duration)
            coverage = min(0.18, distance / max(1.0, clip.duration) * 0.36)
            return rank(window) + coverage, -window[0]

        chosen = max(remaining, key=coverage_rank)
        selected.append(chosen)
        remaining.remove(chosen)
    return sorted(selected, key=lambda item: (item[0], item[1]))


def _group_cues(cues: list[TranscriptCue]) -> list[list[TranscriptCue]]:
    groups: list[list[TranscriptCue]] = []
    for cue in sorted(cues, key=lambda item: (item.start, item.end)):
        if cue.end <= cue.start or not cue.text.strip():
            continue
        if not groups:
            groups.append([cue])
            continue
        current = groups[-1]
        if cue.start - current[-1].end <= 1.8 and cue.end - current[0].start <= 17.0:
            current.append(cue)
        else:
            groups.append([cue])
    return groups


def _ensure_duration(start: float, end: float, total: float, minimum: float, maximum: float) -> tuple[float, float]:
    if end - start > maximum:
        end = start + maximum
    if end - start < minimum:
        missing = minimum - (end - start)
        start = max(0.0, start - missing / 2.0)
        end = min(total, end + missing / 2.0)
        if end - start < minimum:
            start = max(0.0, end - minimum)
    return start, end


def _append_window(windows: list[tuple[float, float, str]], incoming: tuple[float, float, str]) -> None:
    start, end, origin = incoming
    if end - start < 0.75:
        return
    for current_start, current_end, _ in windows:
        overlap = max(0.0, min(end, current_end) - max(start, current_start))
        shorter = min(end - start, current_end - current_start)
        if shorter > 0 and overlap / shorter >= 0.72:
            return
    windows.append((start, end, origin))


def _window_transcript(cues: list[TranscriptCue], start: float, end: float) -> str:
    texts = [cue.text.strip() for cue in cues if cue.end > start and cue.start < end and cue.text.strip()]
    return " ".join(texts)


def _window_signals(samples: list[dict[str, float]], start: float, end: float) -> tuple[float, float]:
    selected = [sample for sample in samples if start <= float(sample.get("time", 0.0)) <= end]
    if not selected and samples:
        center = (start + end) / 2.0
        selected = [min(samples, key=lambda sample: abs(float(sample.get("time", 0.0)) - center))]
    if not selected:
        return 0.0, 0.5
    motion = sum(float(sample.get("motion", 0.0)) for sample in selected) / len(selected)
    quality = sum(
        0.50 * float(sample.get("contrast", 0.0))
        + 0.50 * _brightness_quality(float(sample.get("brightness", 0.5)))
        for sample in selected
    ) / len(selected)
    return min(1.0, motion), min(1.0, quality)


def _brightness_quality(brightness: float) -> float:
    return max(0.0, 1.0 - abs(brightness - 0.52) * 2.2)


def _roles(text: str, clip: Clip, start: float, end: float, origin: str) -> list[str]:
    normalized = text.casefold()
    roles: list[str] = []
    if any(word in normalized for word in JOURNEY_WORDS) or origin in {"opener", "closer"}:
        roles.append("journey")
    if any(word in normalized for word in FUN_WORDS) or text.count("!") >= 1:
        roles.append("fun")
    if any(word in normalized for word in FOOD_WORDS):
        roles.append("food")
    if any(word in normalized for word in SCENERY_WORDS) or (not text and origin == "visual"):
        roles.append("scenery")
    if text:
        roles.append("dialogue")
    if start <= 0.5:
        roles.append("opener")
    if clip.duration - end <= 0.75:
        roles.append("closer")
    return unique_preserving_order(roles or ["moment"])


def _score_candidate(
    roles: list[str],
    speech_ratio: float,
    motion: float,
    quality: float,
    start: float,
    end: float,
    clip_duration: float,
) -> float:
    role_bonus = {
        "fun": 0.22,
        "food": 0.15,
        "journey": 0.12,
        "dialogue": 0.09,
        "scenery": 0.10,
        "opener": 0.04,
        "closer": 0.04,
    }
    value = 0.18 + 0.22 * quality + 0.20 * motion + 0.18 * min(1.0, speech_ratio * 1.7)
    value += sum(role_bonus.get(role, 0.0) for role in set(roles))
    if end - start < 1.5:
        value -= 0.2
    if clip_duration > 0 and (start <= 0.25 or clip_duration - end <= 0.25):
        value += 0.03
    return max(0.0, min(1.0, value))


def _candidate_location(clip: Clip, transcript: str, rules: Any) -> str | None:
    if clip.location:
        return clip.location
    if not isinstance(rules, list):
        return None
    lowered = transcript.casefold()
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        keywords = rule.get("keywords", [])
        if isinstance(keywords, str):
            keywords = [keywords]
        if any(str(keyword).casefold() in lowered for keyword in keywords):
            label = str(rule.get("label", "")).strip()
            if label:
                return label
    return None


def _day_summary(candidates: list[Candidate]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[candidate.day_key].append(candidate)
    return [
        {
            "day_key": day_key,
            "travel_day": values[0].travel_day,
            "candidate_count": len(values),
            "source_duration": round(sum(value.duration for value in values), 2),
        }
        for day_key, values in sorted(grouped.items())
    ]


def _candidate_timestamp(candidate: Candidate) -> float:
    return datetime.fromisoformat(candidate.captured_at).astimezone(timezone.utc).timestamp()
