from __future__ import annotations

import json
import math
import subprocess
from datetime import datetime
from hashlib import sha1
from pathlib import Path
from typing import Dict, Iterable, List

from .brief import brief_editorial_brief_text, brief_target_runtime_seconds
from .clip_results import load_all_clip_results
from .models import ClipInfo


SEGMENT_CANDIDATES_FILENAME = "segment_candidates.json"
SEGMENT_SELECTION_FILENAME = "segment_selection.json"
FRAMES_PER_SEGMENT = 3
FRAME_LONG_EDGE = 1280
GROUP_MAX_GAP = 2.4
GROUP_MIN_DURATION = 2.0
GROUP_MAX_DURATION = 18.0
SEGMENT_PRE_ROLL = 0.35
SEGMENT_POST_ROLL = 0.85
INTRO_SECONDS = 3.6
OUTRO_SECONDS = 2.8
TITLE_SECONDS = 2.6


def _normalize_transcript_cues(cues: List[Dict[str, object]]) -> List[Dict[str, float | str]]:
    normalized: List[Dict[str, float | str]] = []
    for cue in cues:
        if not isinstance(cue, dict):
            continue
        try:
            start = float(cue.get("start", 0.0))
            end = float(cue.get("end", start))
        except (TypeError, ValueError):
            continue
        text = str(cue.get("text", "")).strip()
        if not text or end - start < 0.2:
            continue
        normalized.append(
            {
                "start": max(0.0, start),
                "end": max(start, end),
                "text": text,
            }
        )
    normalized.sort(key=lambda item: (float(item["start"]), float(item["end"])))
    return normalized


def _group_transcript_cues(cues: List[Dict[str, float | str]]) -> List[Dict[str, object]]:
    if not cues:
        return []

    groups: List[Dict[str, object]] = []
    current: Dict[str, object] = {
        "start": float(cues[0]["start"]),
        "end": float(cues[0]["end"]),
        "texts": [str(cues[0]["text"])],
    }

    for cue in cues[1:]:
        gap = float(cue["start"]) - float(current["end"])
        next_end = float(cue["end"])
        current_start = float(current["start"])
        if gap <= GROUP_MAX_GAP and next_end - current_start <= GROUP_MAX_DURATION:
            current["end"] = next_end
            current["texts"].append(str(cue["text"]))
            continue
        groups.append(current)
        current = {
            "start": float(cue["start"]),
            "end": float(cue["end"]),
            "texts": [str(cue["text"])],
        }
    groups.append(current)

    merged: List[Dict[str, object]] = []
    for group in groups:
        duration = float(group["end"]) - float(group["start"])
        if merged and duration < GROUP_MIN_DURATION:
            merged[-1]["end"] = float(group["end"])
            merged[-1]["texts"].extend(list(group["texts"]))
            continue
        merged.append(group)
    return merged


def _frame_offsets(duration: float) -> List[float]:
    anchors = [0.18, 0.50, 0.82][:FRAMES_PER_SEGMENT]
    return [max(0.0, min(duration, duration * anchor)) for anchor in anchors]


def _extract_frame(clip_path: str, source_time: float, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    scale_filter = (
        f"scale='if(gt(iw,ih),{FRAME_LONG_EDGE},-2)':'if(gt(iw,ih),-2,{FRAME_LONG_EDGE})':flags=lanczos"
    )
    command = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{max(0.0, source_time):.3f}",
        "-i",
        clip_path,
        "-frames:v",
        "1",
        "-q:v",
        "2",
        "-vf",
        scale_filter,
        str(output_path),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)


def _candidate_excerpt(texts: List[str]) -> str:
    merged = " ".join(text.strip() for text in texts if text.strip()).strip()
    return merged[:500].strip()


def _story_anchor_lookup(brief: Dict[str, object]) -> Dict[str, Dict[str, object]]:
    anchors = brief.get("story_anchors", []) if isinstance(brief, dict) else []
    return {
        str(anchor.get("id", "")).strip(): anchor
        for anchor in anchors
        if isinstance(anchor, dict) and str(anchor.get("id", "")).strip()
    }


def _must_include_terms(brief: Dict[str, object]) -> List[str]:
    editorial = brief.get("editorial_brief", {}) if isinstance(brief, dict) else {}
    if not isinstance(editorial, dict):
        return []
    terms: List[str] = []
    for item in editorial.get("must_include", []):
        text = str(item).strip()
        if text:
            terms.append(text.lower())
    return terms


def _candidate_search_text(entry: Dict[str, object]) -> str:
    return " ".join(
        [
            str(entry.get("transcript_excerpt", "")),
            str(entry.get("story_anchor_id", "")),
            str(entry.get("story_anchor_label", "")),
            str(entry.get("time_anchor_id", "")),
            str(entry.get("time_anchor_label", "")),
            str(entry.get("filename", "")),
        ]
    ).lower()


def _candidate_priority(entry: Dict[str, object], brief: Dict[str, object], day_index: int, total_days: int) -> float:
    text = _candidate_search_text(entry)
    score = 1.0
    if bool(entry.get("has_audio", False)):
        score += 0.25

    duration = float(entry.get("duration", 0.0))
    if 3.0 <= duration <= 12.0:
        score += 0.18
    elif duration > 16.0:
        score -= 0.08

    if any(term in text for term in _must_include_terms(brief)):
        score += 0.35

    editorial_text = brief_editorial_brief_text(brief).lower()
    if any(word in editorial_text for word in ("식사", "meal", "food", "음식")) and any(
        word in text for word in ("식사", "조식", "점심", "저녁", "커피", "카페", "디저트", "맛있", "meal", "food")
    ):
        score += 0.28
    if any(word in editorial_text for word in ("리액션", "reaction", "반응")) and any(
        word in text for word in ("우와", "웃", "하하", "반응", "reaction")
    ):
        score += 0.22

    anchor_id = str(entry.get("story_anchor_id", "")).lower()
    if day_index == 1 and any(word in anchor_id for word in ("departure", "airport")):
        score += 0.18
    if day_index == total_days and any(word in anchor_id for word in ("return", "closing", "home")):
        score += 0.18
    return score


def _infer_role(entry: Dict[str, object], day_index: int, total_days: int, ordinal: int, count: int) -> str:
    text = _candidate_search_text(entry)
    anchor_id = str(entry.get("story_anchor_id", "")).lower()
    if day_index == 1 and ordinal == 1:
        return "opener"
    if day_index == total_days and ordinal == count:
        return "closer"
    if "meal" in anchor_id or any(word in text for word in ("식사", "조식", "점심", "저녁", "커피", "카페", "디저트", "맛있")):
        return "meal"
    if "reaction" in anchor_id or any(word in text for word in ("웃", "우와", "하하", "반응")):
        return "reaction"
    if any(word in anchor_id for word in ("departure", "airport", "arrival", "return")):
        return "transition"
    return "moment"


def _selection_reason(entry: Dict[str, object], role: str, brief: Dict[str, object]) -> str:
    excerpt = str(entry.get("transcript_excerpt", "")).strip()
    anchor = str(entry.get("story_anchor_label", "")).strip() or str(entry.get("story_anchor_id", "")).strip()
    brief_text = brief_editorial_brief_text(brief)
    if role == "meal":
        return f"식사 리듬과 여행 분위기를 살리는 장면이라 선택했다. ({anchor})"
    if role == "reaction":
        return f"가족 반응과 감정선이 드러나는 장면이라 선택했다. ({anchor})"
    if role == "opener":
        return "여행이 시작되는 느낌과 기대감을 여는 장면이라 첫머리에 배치했다."
    if role == "closer":
        return "여행을 정리하고 마무리하는 감정선을 남기는 장면이라 마지막에 배치했다."
    if anchor:
        return f"프롬프트 의도와 하루 흐름을 살리는 핵심 장면이라 선택했다. ({anchor})"
    return f"프롬프트 의도에 맞는 흐름을 만들기 위해 선택했다. {excerpt[:80]}".strip()


def _allocate_day_budgets(grouped_days: List[List[Dict[str, object]]], target_seconds: int) -> List[float]:
    weights = [math.sqrt(sum(float(item.get('duration', 0.0)) for item in day) or 1.0) for day in grouped_days]
    total_weight = sum(weights) or 1.0
    return [target_seconds * weight / total_weight for weight in weights]


def _auto_select_day(
    entries: List[Dict[str, object]],
    brief: Dict[str, object],
    budget_seconds: float,
    day_index: int,
    total_days: int,
) -> List[Dict[str, object]]:
    ranked = sorted(
        entries,
        key=lambda entry: (
            -_candidate_priority(entry, brief, day_index, total_days),
            str(entry.get("source_time", "")),
            float(entry.get("start", 0.0)),
        ),
    )
    chosen_ids: List[str] = []
    used = 0.0
    for entry in ranked:
        duration = float(entry.get("duration", 0.0))
        if chosen_ids and used + duration > budget_seconds * 1.08:
            continue
        chosen_ids.append(str(entry["candidate_id"]))
        used += duration
        if used >= budget_seconds:
            break
    if not chosen_ids and ranked:
        chosen_ids.append(str(ranked[0]["candidate_id"]))

    selected = [entry for entry in entries if str(entry["candidate_id"]) in set(chosen_ids)]
    selected.sort(key=lambda entry: (datetime.fromisoformat(str(entry["source_time"])), float(entry["start"]), float(entry["end"])))
    output: List[Dict[str, object]] = []
    for ordinal, entry in enumerate(selected, start=1):
        role = _infer_role(entry, day_index, total_days, ordinal, len(selected))
        output.append(
            {
                "candidate_id": entry["candidate_id"],
                "clip_path": entry["clip_path"],
                "start": entry["start"],
                "end": entry["end"],
                "sequence_index": ordinal,
                "enabled": True,
                "role": role,
                "reason": _selection_reason(entry, role, brief),
            }
        )
    return output


def write_auto_segment_selection(
    build_dir: Path,
    project_title: str,
    brief: Dict[str, object],
) -> Path:
    candidates_payload = load_segment_candidates(build_dir)
    candidates = [item for item in candidates_payload.get("candidates", []) if isinstance(item, dict)]
    if not candidates:
        raise ValueError("No segment candidates could be generated from transcripts.")

    grouped: Dict[str, List[Dict[str, object]]] = {}
    for entry in candidates:
        grouped.setdefault(str(entry.get("date_key", "")), []).append(entry)
    ordered_date_keys = sorted(grouped)
    grouped_days = [
        sorted(grouped[date_key], key=lambda item: (datetime.fromisoformat(str(item["source_time"])), float(item["start"]), float(item["end"])))
        for date_key in ordered_date_keys
    ]
    overhead = int(round(INTRO_SECONDS + OUTRO_SECONDS + TITLE_SECONDS * len(grouped_days)))
    target_runtime = max(6 * 60, brief_target_runtime_seconds(brief))
    content_target = max(60, target_runtime - overhead)
    day_budgets = _allocate_day_budgets(grouped_days, content_target)

    selection_payload = {
        "project_title": project_title,
        "selection_mode": "auto",
        "source_candidates_path": str((build_dir / "segment_candidates" / SEGMENT_CANDIDATES_FILENAME).resolve()),
        "days": [],
    }
    for day_number, date_key in enumerate(ordered_date_keys, start=1):
        selections = _auto_select_day(grouped[date_key], brief, day_budgets[day_number - 1], day_number, len(ordered_date_keys))
        selection_payload["days"].append(
            {
                "travel_day": day_number,
                "label": f"Day {day_number} · {date_key}",
                "date_key": date_key,
                "selections": selections,
            }
        )

    selection_path = build_dir / "segment_candidates" / SEGMENT_SELECTION_FILENAME
    selection_path.write_text(json.dumps(selection_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return selection_path


def build_segment_candidates(
    build_dir: Path,
    project_title: str,
    clips: Iterable[ClipInfo],
    brief: Dict[str, object],
) -> Dict[str, object]:
    clip_lookup = {clip.path: clip for clip in clips}
    story_lookup = _story_anchor_lookup(brief)
    candidates_dir = build_dir / "segment_candidates"
    frames_dir = candidates_dir / "frames"
    entries: List[Dict[str, object]] = []

    for result in load_all_clip_results(build_dir):
        clip_path = str(result.get("clip_path", "")).strip()
        clip = clip_lookup.get(clip_path)
        if clip is None:
            continue
        cues = _normalize_transcript_cues(list(result.get("transcript", [])))
        grouped = _group_transcript_cues(cues)
        for group_index, group in enumerate(grouped, start=1):
            start = max(0.0, float(group["start"]) - SEGMENT_PRE_ROLL)
            end = min(clip.duration, float(group["end"]) + SEGMENT_POST_ROLL)
            if end - start < GROUP_MIN_DURATION:
                end = min(clip.duration, start + GROUP_MIN_DURATION)
            digest = sha1(f"{clip_path}:{start:.3f}:{end:.3f}".encode("utf-8")).hexdigest()[:12]
            frame_paths: List[str] = []
            for frame_index, offset in enumerate(_frame_offsets(end - start), start=1):
                frame_path = frames_dir / f"{len(entries)+1:03d}_{digest}_{frame_index}.jpg"
                if not frame_path.exists():
                    _extract_frame(clip_path, start + offset, frame_path)
                frame_paths.append(str(frame_path.resolve()))
            story_anchor = story_lookup.get(clip.route_phase, {})
            entries.append(
                {
                    "candidate_id": f"seg-{len(entries)+1:03d}-{digest}",
                    "clip_path": clip.path,
                    "filename": clip.filename,
                    "travel_day": clip.travel_day,
                    "date_key": clip.date_key,
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "duration": round(end - start, 3),
                    "transcript_excerpt": _candidate_excerpt(list(group["texts"])),
                    "time_anchor_id": clip.location_id,
                    "time_anchor_label": clip.location_id,
                    "story_anchor_id": clip.route_phase,
                    "story_anchor_label": str(story_anchor.get("label", "")).strip() or clip.route_label,
                    "frame_paths": frame_paths,
                    "source_time": clip.start_time.isoformat(),
                    "has_audio": clip.has_audio,
                    "group_index": group_index,
                }
            )

    if not entries:
        raise ValueError("No segment candidates could be generated from transcripts.")

    candidates_dir.mkdir(parents=True, exist_ok=True)
    candidates_path = candidates_dir / SEGMENT_CANDIDATES_FILENAME
    candidates_payload = {
        "project_title": project_title,
        "editorial_brief": brief_editorial_brief_text(brief),
        "candidate_count": len(entries),
        "candidates": entries,
    }
    candidates_path.write_text(json.dumps(candidates_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    selection_path = write_auto_segment_selection(build_dir, project_title, brief)

    return {
        "segment_candidates_path": str(candidates_path.resolve()),
        "segment_selection_path": str(selection_path.resolve()),
        "frames_dir": str(frames_dir.resolve()),
        "candidate_count": len(entries),
    }


def load_segment_candidates(build_dir: Path) -> Dict[str, object]:
    path = build_dir / "segment_candidates" / SEGMENT_CANDIDATES_FILENAME
    if not path.exists():
        raise FileNotFoundError(f"segment_candidates.json is missing: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Invalid segment candidates file: {path}")
    return data


def load_segment_selection(build_dir: Path) -> Dict[str, object]:
    path = build_dir / "segment_candidates" / SEGMENT_SELECTION_FILENAME
    if not path.exists():
        raise FileNotFoundError("Selection output is missing. Re-run `plan` to generate it.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Invalid generated selection data: {path}")
    return data


def validate_segment_selection(build_dir: Path) -> List[Dict[str, object]]:
    candidates_payload = load_segment_candidates(build_dir)
    selection_payload = load_segment_selection(build_dir)
    candidates = {
        str(item.get("candidate_id", "")).strip(): item
        for item in candidates_payload.get("candidates", [])
        if isinstance(item, dict) and str(item.get("candidate_id", "")).strip()
    }
    days = selection_payload.get("days", [])
    if not isinstance(days, list):
        raise ValueError("Selection output must contain a `days` array.")

    validated: List[Dict[str, object]] = []
    for day_entry in days:
        if not isinstance(day_entry, dict):
            continue
        travel_day = int(day_entry.get("travel_day", 0) or 0)
        date_key = str(day_entry.get("date_key", "")).strip()
        selections = day_entry.get("selections", [])
        if not isinstance(selections, list):
            raise ValueError(f"Day {travel_day} must contain a `selections` array.")
        local_sequence_indices: List[int] = []
        for item in selections:
            if not isinstance(item, dict):
                continue
            if not bool(item.get("enabled", True)):
                continue
            candidate_id = str(item.get("candidate_id", "")).strip()
            candidate = candidates.get(candidate_id)
            if candidate is None:
                raise ValueError(f"Selection item {candidate_id or '<missing>'} does not reference a known candidate.")
            clip_path = str(item.get("clip_path", "")).strip()
            if clip_path != str(candidate.get("clip_path", "")).strip():
                raise ValueError(f"Selection item {candidate_id} has clip_path mismatch.")
            candidate_date = str(candidate.get("date_key", "")).strip()
            if date_key and candidate_date and candidate_date != date_key:
                raise ValueError(f"Selection item {candidate_id} is assigned to the wrong date group.")
            try:
                start = float(item.get("start"))
                end = float(item.get("end"))
                sequence_index = int(item.get("sequence_index"))
            except (TypeError, ValueError):
                raise ValueError(f"Selection item {candidate_id} is missing required numeric fields.")
            candidate_start = float(candidate.get("start", 0.0))
            candidate_end = float(candidate.get("end", candidate_start))
            if start < candidate_start - 0.01 or end > candidate_end + 0.01 or end <= start:
                raise ValueError(f"Selection item {candidate_id} is outside candidate bounds.")
            role = str(item.get("role", "")).strip()
            reason = str(item.get("reason", "")).strip()
            if not role or not reason:
                raise ValueError(f"Selection item {candidate_id} must include `role` and `reason`.")
            local_sequence_indices.append(sequence_index)
            validated.append(
                {
                    **item,
                    "travel_day": travel_day,
                    "day_label": str(day_entry.get("label", "")).strip() or f"Day {travel_day}",
                    "date_key": date_key,
                    "sequence_index": sequence_index,
                }
            )
        if local_sequence_indices and sorted(local_sequence_indices) != list(range(1, len(local_sequence_indices) + 1)):
            raise ValueError(f"Selections for day {travel_day} must use contiguous sequence_index values starting at 1.")

    if not validated:
        raise ValueError("Selection output contains no enabled selections.")

    validated.sort(key=lambda item: (int(item["travel_day"]), int(item["sequence_index"])))
    return validated
