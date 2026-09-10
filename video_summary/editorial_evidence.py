"""Source-linked editorial observations for local human review.

This module does not perform visual recognition. Existing role/stage labels are
inferences, not proof that food was eaten or an action reached its result. A
``visual`` observation means a person reports watching the specified source
range. Validation checks source identity and temporal consistency, not whether
that person's description is true. No media, models, or network are accessed.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

from .utils import VideoSummaryError, stable_hash


OBSERVATION_KINDS = frozenset(
    {"meal_body", "play_action", "action_result", "interview", "meeting", "other"}
)
OBSERVATION_BASES = frozenset({"visual", "speech", "inferred"})


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if callable(getattr(value, "to_dict", None)):
        return value.to_dict()
    raise VideoSummaryError(f"{name}: 객체가 필요합니다.")


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VideoSummaryError(f"{name}: 유한한 숫자가 필요합니다.")
    try:
        result = float(value)
    except (ValueError, OverflowError):
        raise VideoSummaryError(f"{name}: 유한한 숫자가 필요합니다.") from None
    if not math.isfinite(result):
        raise VideoSummaryError(f"{name}: 유한한 숫자가 필요합니다.")
    return result


def _source(clip: Any) -> tuple[Mapping[str, Any], float]:
    source = _mapping(clip, "clip")
    for key in ("clip_id", "fingerprint"):
        if not isinstance(source.get(key), str) or not source[key].strip():
            raise VideoSummaryError(f"clip.{key}: 원본 식별 정보가 필요합니다.")
    duration = _number(source.get("duration"), "clip.duration")
    if duration <= 0:
        raise VideoSummaryError("clip.duration: 원본 길이는 0보다 커야 합니다.")
    return source, duration


def _range(value: Any, name: str, duration: float) -> dict[str, float]:
    value = _mapping(value, name)
    start = _number(value.get("start"), f"{name}.start")
    end = _number(value.get("end"), f"{name}.end")
    if not 0 <= start < end <= duration:
        raise VideoSummaryError(f"{name}: 0 <= start < end <= 원본 길이여야 합니다.")
    return {"start": start, "end": end}


def _overlaps(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return left["start"] < right["end"] and right["start"] < left["end"]


def _contains(outer: Mapping[str, Any], inner: Mapping[str, Any]) -> bool:
    return outer["start"] <= inner["start"] and outer["end"] >= inner["end"]


def _invalid_cue_may_intersect(raw: Any, bounds: Mapping[str, Any]) -> bool:
    """Only ignore invalid legacy cues whose finite span is clearly unrelated.

    No clipping or repair is performed. Non-numeric/NaN timings cannot safely
    be located, while an overrun entirely after a reviewed core cannot cut it.
    An inverted cue conservatively occupies its possible min/max span.
    """
    try:
        cue = _mapping(raw, "transcript cue")
        if not isinstance(cue.get("text"), str):
            return True
        start = _number(cue.get("start"), "transcript cue.start")
        end = _number(cue.get("end"), "transcript cue.end")
    except VideoSummaryError:
        return True
    if start == end:
        return bounds["start"] <= start < bounds["end"]
    return _overlaps(bounds, {"start": min(start, end), "end": max(start, end)})


def transcript_evidence(cues: Iterable[Any], clip: Any) -> list[dict[str, Any]]:
    """Return current, source-bound cue IDs; changing text/timing invalidates IDs.

    IDs do not depend on transcript ordering or candidate generation. Supplied
    cue IDs are not trusted. An empty caption has no spoken boundary to protect.
    """
    source, duration = _source(clip)
    result: dict[str, dict[str, Any]] = {}
    for raw in cues:
        cue = _mapping(raw, "transcript cue")
        if not isinstance(cue.get("text"), str):
            raise VideoSummaryError("transcript cue.text: 문자열이 필요합니다.")
        text = cue["text"].strip()
        if not text:
            continue
        bounds = _range(cue, "transcript cue", duration)
        cue_id = "cue_" + stable_hash(
            {"clip_id": source["clip_id"], "fingerprint": source["fingerprint"], **bounds, "text": text}
        )
        result[cue_id] = {"cue_id": cue_id, **bounds, "text": text, "basis": "speech"}
    return sorted(result.values(), key=lambda cue: (cue["start"], cue["end"], cue["cue_id"]))


def validate_human_observation(
    record: Any, clip: Any, cues: Iterable[Any] = ()
) -> dict[str, Any]:
    """Validate one human record without trusting roles or regenerated IDs.

    Core ranges must contain whole intersecting transcript cues. Malformed
    legacy cues may only be ignored when their finite span is clearly disjoint
    from the core. Speech-basis claims cite current valid IDs contained by that
    core. Visual and inferred observations need no caption, so silent iPhone
    footage is supported. This checks manifest fingerprints, not files on disk.
    """
    record = _mapping(record, "observation")
    source, duration = _source(clip)
    if record.get("clip_id") != source["clip_id"]:
        raise VideoSummaryError("observation.clip_id: 알 수 없는 원본입니다.")
    if record.get("source_fingerprint") != source["fingerprint"]:
        raise VideoSummaryError("observation.source_fingerprint: 원본이 변경되어 재검토가 필요합니다.")
    version = record.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise VideoSummaryError("observation.version: 지원하지 않는 형식입니다.")
    kind = record.get("kind")
    basis = record.get("basis")
    if not isinstance(kind, str) or kind not in OBSERVATION_KINDS:
        raise VideoSummaryError("observation.kind: 지원하지 않는 관찰 종류입니다.")
    if not isinstance(basis, str) or basis not in OBSERVATION_BASES:
        raise VideoSummaryError("observation.basis: visual, speech, inferred 중 하나여야 합니다.")
    description = record.get("description")
    if not isinstance(description, str) or not description.strip() or len(description) > 2000:
        raise VideoSummaryError("observation.description: 1~2000자 설명이 필요합니다.")
    core = _range(record.get("core_range"), "core_range", duration)
    context = _range(record.get("context_range", core), "context_range", duration)
    if not _contains(context, core):
        raise VideoSummaryError("context_range: 핵심 구간 전체를 포함해야 합니다.")
    by_id: dict[str, dict[str, Any]] = {}
    for raw_cue in cues:
        try:
            for cue in transcript_evidence([raw_cue], source):
                by_id[cue["cue_id"]] = cue
        except VideoSummaryError as exc:
            if _invalid_cue_may_intersect(raw_cue, core):
                raise VideoSummaryError(
                    "core_range: 핵심 구간과 겹치거나 위치를 알 수 없는 기존 발화 시간 오류가 있습니다. "
                    "전사 시간 정보를 확인하세요."
                ) from exc
    cue_ids = record.get("source_cue_ids", [])
    if not isinstance(cue_ids, list) or any(not isinstance(item, str) for item in cue_ids):
        raise VideoSummaryError("source_cue_ids: 발화 ID 문자열 목록이 필요합니다.")
    if len(cue_ids) != len(set(cue_ids)):
        raise VideoSummaryError("source_cue_ids: 중복된 발화 ID가 있습니다.")
    if basis == "speech" and not cue_ids:
        raise VideoSummaryError("source_cue_ids: speech 관찰에는 발화 근거가 필요합니다.")
    for cue_id in cue_ids:
        if cue_id not in by_id:
            raise VideoSummaryError("source_cue_ids: 알 수 없거나 변경된 발화 ID입니다.")
        if not _contains(core, by_id[cue_id]):
            raise VideoSummaryError("source_cue_ids: 인용 발화 전체가 핵심 구간 안에 있어야 합니다.")
    for cue in by_id.values():
        if _overlaps(core, cue) and not _contains(core, cue):
            raise VideoSummaryError(
                f"core_range: 발화를 중간에 자릅니다. {cue['start']:g}~{cue['end']:g}초 전체를 포함하세요."
            )
    return {
        "version": 1,
        "clip_id": source["clip_id"],
        "source_fingerprint": source["fingerprint"],
        "kind": kind,
        "basis": basis,
        "description": description.strip(),
        "core_range": core,
        "context_range": context,
        "source_cue_ids": list(cue_ids),
    }


def _whole_speech_range(bounds: Mapping[str, float], cues: list[dict[str, Any]]) -> dict[str, float]:
    expanded = dict(bounds)
    # ASR cues can overlap; repeat until a transitive boundary expansion settles.
    while True:
        previous = dict(expanded)
        for cue in cues:
            if _overlaps(expanded, cue):
                expanded["start"] = min(expanded["start"], cue["start"])
                expanded["end"] = max(expanded["end"], cue["end"])
        if expanded == previous:
            return expanded


def build_candidate_evidence(
    candidate: Any,
    clip: Any,
    cues: Iterable[Any] = (),
    observations: Iterable[Any] = (),
    *,
    context_seconds: float = 4.0,
) -> dict[str, Any]:
    """Build a JSON-compatible review view without making new semantic claims.

    Legacy dictionaries and current dataclasses both work. Existing human
    observations survive candidate re-generation because they target source
    ranges. Partial overlaps are shown but do not count as visual coverage.
    """
    candidate = _mapping(candidate, "candidate")
    source, duration = _source(clip)
    if candidate.get("clip_id") != source["clip_id"]:
        raise VideoSummaryError("candidate.clip_id: 알 수 없는 원본입니다.")
    candidate_range = _range(candidate, "candidate", duration)
    padding = _number(context_seconds, "context_seconds")
    if padding < 0:
        raise VideoSummaryError("context_seconds: 0 이상이어야 합니다.")
    raw_cues = list(cues)
    # Old sidecars can have invalid or out-of-source timings. Keep the catalog
    # usable, but never treat unavailable evidence as silence. A human core
    # can still be confirmed if the bad legacy cue is provably unrelated.
    current_by_id: dict[str, dict[str, Any]] = {}
    invalid_transcript = False
    for raw_cue in raw_cues:
        try:
            for cue in transcript_evidence([raw_cue], source):
                current_by_id[cue["cue_id"]] = cue
        except VideoSummaryError:
            invalid_transcript = True
    current_cues = sorted(
        current_by_id.values(), key=lambda cue: (cue["start"], cue["end"], cue["cue_id"])
    )
    core = _whole_speech_range(candidate_range, current_cues)
    context = _whole_speech_range(
        {"start": max(0.0, core["start"] - padding), "end": min(duration, core["end"] + padding)},
        current_cues,
    )
    flags: list[str] = []
    if invalid_transcript:
        flags.append("invalid_transcript_evidence")
    if core != candidate_range:
        flags.append("speech_boundary_cut")
    records: list[dict[str, Any]] = []
    for raw in observations:
        try:
            value = _mapping(raw, "observation")
            if value.get("clip_id") != source["clip_id"]:
                continue
            record = validate_human_observation(value, source, raw_cues)
        except VideoSummaryError:
            flags.append("stale_or_invalid_observation")
            if invalid_transcript:
                flags.append("observation_needs_transcript_review")
            continue
        if _overlaps(candidate_range, record["core_range"]):
            records.append(record)
            if not _contains(candidate_range, record["core_range"]):
                flags.append("observed_core_not_fully_in_candidate")
    confirmed = sorted(
        {record["kind"] for record in records if record["basis"] == "visual" and _contains(candidate_range, record["core_range"])}
    )
    roles = candidate.get("roles") or []
    classifications: list[dict[str, str]] = []
    if "food" in roles or candidate.get("required_meal_event_ids") or candidate.get("required_meal_context_ids"):
        classifications.append({"kind": "meal", "basis": "inferred", "reason": "기존 식사 역할·이벤트 분류이며 실제 먹는 행동의 확인은 아닙니다."})
        if "meal_body" not in confirmed:
            flags.append("meal_body_needs_visual_review")
    if "fun" in roles:
        classifications.append({"kind": "play", "basis": "inferred", "reason": "기존 놀이 역할 분류이며 행동·결과를 확인해야 합니다."})
    if "interview" in roles:
        classifications.append({"kind": "interview", "basis": "inferred", "reason": "기존 인터뷰 분류이며 질문·답변의 완결을 확인해야 합니다."})
    if candidate.get("story_stage"):
        classifications.append({"kind": str(candidate["story_stage"]), "basis": "inferred", "reason": "기존 서사 단계이며 시각적으로 검증된 행동 단계가 아닙니다."})
    if not invalid_transcript and not any(_overlaps(candidate_range, cue) for cue in current_cues):
        flags.append("no_transcript_evidence")
        source_kind = source.get("source_kind")
        if source_kind in {None, "unknown"}:
            source_kind = candidate.get("source_kind")
        if source_kind in {"phone", "shared"}:
            flags.append("phone_without_transcript_visual_review")
    if not records:
        flags.append("no_human_observation")
    return {
        "candidate_id": candidate.get("candidate_id"),
        "clip_id": source["clip_id"],
        "source_fingerprint": source["fingerprint"],
        "candidate_range": candidate_range,
        "suggested_core_range": core,
        "context_range": context,
        "transcript_cues": [cue for cue in current_cues if _overlaps(context, cue)],
        "classifications": classifications,
        "observations": records,
        "confirmed_visual_kinds": confirmed,
        "flags": list(dict.fromkeys(flags)),
        "review_status": "has_human_observations" if records else "needs_review",
    }
