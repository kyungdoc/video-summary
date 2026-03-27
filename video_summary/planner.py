from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Sequence

from .models import ClipInfo, TimelinePlan


INTRO_SECONDS = 3.6
OUTRO_SECONDS = 2.8
TITLE_SECONDS = 2.6


def _chapter_stamp(seconds: float) -> str:
    whole = int(round(seconds))
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _date_span_from_times(values: Sequence[str]) -> str:
    if not values:
        return ""
    dates = sorted(datetime.fromisoformat(value).date() for value in values if value)
    if not dates:
        return ""
    start = dates[0].strftime("%Y.%m.%d")
    end = dates[-1].strftime("%Y.%m.%d")
    return start if start == end else f"{start} - {end}"


def _selection_intro_label(title: str, source_times: Sequence[str]) -> str:
    date_span = _date_span_from_times(source_times)
    return f"{title} · {date_span}" if date_span else title


def _outro_label(trip_title: str) -> str:
    return f"여행의 끝 · {trip_title}"


def _selection_segment_item(selection: Dict[str, object], clip: ClipInfo) -> Dict[str, object]:
    start = float(selection["start"])
    end = float(selection["end"])
    selection_day = int(selection.get("travel_day", clip.travel_day))
    return {
        "kind": "segment",
        "clip_path": clip.path,
        "filename": clip.filename,
        "travel_day": selection_day,
        "label": f"{selection['role']} · {clip.filename}",
        "start": round(start, 3),
        "end": round(end, 3),
        "duration": round(end - start, 3),
        "source_time": clip.start_time.isoformat(),
        "score": 1.0,
        "has_audio": clip.has_audio,
        "location_id": clip.location_id,
        "route_phase": clip.route_phase,
        "route_label": clip.route_label,
        "selection_role": str(selection["role"]),
        "selection_reason": str(selection["reason"]),
        "candidate_id": str(selection["candidate_id"]),
    }


def build_timeline_from_selection(
    selections: Sequence[Dict[str, object]],
    clip_lookup: Dict[str, ClipInfo],
    trip_title: str,
    fps: float,
    intro_date_span: str | None = None,
) -> TimelinePlan:
    ordered = sorted(selections, key=lambda item: (int(item["travel_day"]), int(item["sequence_index"])))
    if not ordered:
        raise ValueError("No enabled selections were produced for the final timeline.")

    items: List[Dict[str, object]] = []
    chapters: List[Dict[str, object]] = []
    source_times = [clip_lookup[str(item["clip_path"])].start_time.isoformat() for item in ordered]
    cursor = 0.0

    intro_label = f"{trip_title} · {intro_date_span}" if intro_date_span else _selection_intro_label(trip_title, source_times)
    items.append({"kind": "title", "travel_day": 0, "label": intro_label, "duration": INTRO_SECONDS})
    chapters.append({"timecode": _chapter_stamp(cursor), "label": "Intro"})
    cursor += INTRO_SECONDS

    current_day = None
    for selection in ordered:
        clip = clip_lookup[str(selection["clip_path"])]
        selection_day = int(selection.get("travel_day", clip.travel_day))
        if current_day != selection_day:
            chapter_label = str(selection.get("day_label", "")).strip() or clip.date_group_label or f"Day {selection_day}"
            chapters.append({"timecode": _chapter_stamp(cursor), "label": chapter_label})
            items.append({"kind": "title", "travel_day": selection_day, "label": chapter_label, "duration": TITLE_SECONDS})
            cursor += TITLE_SECONDS
            current_day = selection_day
        item = _selection_segment_item(selection, clip)
        items.append(item)
        cursor += float(item["duration"])

    chapters.append({"timecode": _chapter_stamp(cursor), "label": "Outro"})
    items.append({"kind": "title", "travel_day": 0, "label": _outro_label(trip_title), "duration": OUTRO_SECONDS})
    cursor += OUTRO_SECONDS

    return TimelinePlan(
        title=trip_title,
        fps=fps,
        target_duration=round(cursor, 3),
        items=items,
        days=[],
        chapters=chapters,
        notes=[
            "Timeline built from the generated selection.",
            f"Selected segments: {len(ordered)}",
        ],
    )
