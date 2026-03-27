from __future__ import annotations

import copy
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Sequence
from zoneinfo import ZoneInfo

import yaml

from .look_presets import DEFAULT_PREVIEW_PRESETS


METADATA_FILENAME = "project_metadata.yaml"
LEGACY_BRIEF_FILENAME = "project_brief.yaml"
PROMPT_FILENAME = "project_prompt.md"

DEFAULT_BRIEF: Dict[str, object] = {
    "version": 3,
    "project": {
        "description": "여행 브이로그",
        "default_timezone": "UTC",
    },
    "people": [
        {
            "id": "narrator",
            "name": "",
            "aliases": [],
            "role": "",
            "importance": "primary",
        }
    ],
    "places": [
        {
            "id": "origin_home",
            "name": "",
            "kind": "home",
            "timezone": "",
            "aliases": [],
            "description": "",
        }
    ],
    "editorial_brief": {
        "summary": "",
        "tone": "",
        "must_include": [],
        "avoid": [],
        "story_goals": [],
        "target_runtime": "",
        "meal_guidance": "",
        "pacing_guidance": "",
        "opener_guidance": "",
        "closer_guidance": "",
    },
    "time_anchors": [],
    "story_anchors": [],
    "transcription": {
        "prompt_terms": [],
        "replacement_rules": [],
    },
    "render": {
        "preview_presets": DEFAULT_PREVIEW_PRESETS,
        "selected_preset": "",
        "final_render_mode": "chunked_direct_mp4",
        "final_feedback": [],
    },
}


def build_project_metadata(project_name: str, timezone_name: str | None = None) -> Dict[str, object]:
    brief = copy.deepcopy(DEFAULT_BRIEF)
    if timezone_name:
        brief["project"]["default_timezone"] = timezone_name  # type: ignore[index]
    return brief


def ensure_project_metadata(build_dir: Path, project_name: str, timezone_name: str | None = None) -> Path:
    build_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = build_dir / METADATA_FILENAME
    legacy_path = build_dir / LEGACY_BRIEF_FILENAME
    if metadata_path.exists():
        return metadata_path
    if legacy_path.exists():
        metadata_path.write_text(legacy_path.read_text(encoding="utf-8"), encoding="utf-8")
        legacy_path.unlink(missing_ok=True)
        return metadata_path
    metadata = build_project_metadata(project_name, timezone_name=timezone_name)
    metadata_path.write_text(yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return metadata_path


def write_project_prompt(build_dir: Path, prompt_text: str) -> Path:
    build_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = build_dir / PROMPT_FILENAME
    prompt_path.write_text(prompt_text.strip() + "\n", encoding="utf-8")
    return prompt_path


def load_project_prompt(build_dir: Path, prompt_path: str | Path | None = None) -> str:
    path = Path(prompt_path).expanduser() if prompt_path is not None else build_dir / PROMPT_FILENAME
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def apply_prompt_to_brief(
    metadata: Dict[str, object],
    prompt_text: str,
    project_name: str,
    timezone_name: str | None = None,
) -> Dict[str, object]:
    merged = copy.deepcopy(metadata)
    prompt = prompt_text.strip()
    if not prompt:
        return merged

    project = merged.setdefault("project", {})
    if isinstance(project, dict):
        if timezone_name and not str(project.get("default_timezone", "")).strip():
            project["default_timezone"] = timezone_name

    editorial = merged.setdefault("editorial_brief", {})
    if not isinstance(editorial, dict):
        editorial = {}
        merged["editorial_brief"] = editorial

    existing_summary = str(editorial.get("summary", "")).strip()
    editorial["summary"] = prompt if not existing_summary else f"{existing_summary}\n\nUser prompt:\n{prompt}"

    lowered = prompt.lower()
    if not str(editorial.get("tone", "")).strip():
        if any(word in lowered for word in ("cinematic", "시네마틱", "영화처럼")):
            editorial["tone"] = "cinematic"
        elif any(word in lowered for word in ("warm", "따뜻", "포근")):
            editorial["tone"] = "warm"
        elif any(word in lowered for word in ("playful", "경쾌", "발랄")):
            editorial["tone"] = "playful"

    target_runtime = str(editorial.get("target_runtime", "")).strip()
    if not target_runtime:
        runtime_match = re.search(r"(\d+\s*(?:~|-)\s*\d+\s*(?:분|minutes|min)|\d+\s*(?:시간|hours|hour)\s*\d*\s*(?:분|minutes|min)?|\d+\s*(?:분|minutes|min))", prompt, re.IGNORECASE)
        if runtime_match:
            editorial["target_runtime"] = runtime_match.group(1).strip()

    pacing = str(editorial.get("pacing_guidance", "")).strip()
    if not pacing:
        if "천천히" in prompt or "여유" in prompt or "linger" in lowered or "generous" in lowered:
            editorial["pacing_guidance"] = "Favor slightly longer holds and gentle transitions."
        elif "빠르게" in prompt or "fast-paced" in lowered:
            editorial["pacing_guidance"] = "Keep the pacing brisk while preserving chronological clarity."

    return merged


def ensure_prompt_backed_brief(
    build_dir: Path,
    project_name: str,
    prompt_text: str,
    timezone_name: str | None = None,
    base_metadata_path: str | Path | None = None,
) -> Path:
    if base_metadata_path is not None:
        brief = load_project_metadata(build_dir, project_name, metadata_path=base_metadata_path)
    else:
        brief = build_project_metadata(project_name, timezone_name=timezone_name)
    write_project_prompt(build_dir, prompt_text)
    merged = apply_prompt_to_brief(brief, prompt_text, project_name, timezone_name=timezone_name)
    metadata_path = build_dir / METADATA_FILENAME
    metadata_path.write_text(yaml.safe_dump(merged, allow_unicode=True, sort_keys=False), encoding="utf-8")
    legacy_path = build_dir / LEGACY_BRIEF_FILENAME
    legacy_path.unlink(missing_ok=True)
    return metadata_path


def load_project_metadata(build_dir: Path, project_name: str, metadata_path: str | Path | None = None) -> Dict[str, object]:
    path = Path(metadata_path).expanduser() if metadata_path is not None else ensure_project_metadata(build_dir, project_name)
    if not path.exists():
        raise FileNotFoundError(f"Project metadata does not exist: {path}")
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Invalid project metadata: {path}")
    version = int(data.get("version", 0)) if str(data.get("version", "")).isdigit() else 0
    if version != 3:
        raise ValueError(f"Unsupported project metadata version {data.get('version')}. Expected version 3.")
    return data


def brief_people_terms(brief: Dict[str, object]) -> List[Dict[str, object]]:
    people = brief.get("people", []) if isinstance(brief, dict) else []
    terms = []
    for person in people:
        if not isinstance(person, dict):
            continue
        name = str(person.get("name", "")).strip()
        if not name:
            continue
        aliases = [str(alias).strip() for alias in person.get("aliases", []) if str(alias).strip()]
        terms.append({"canonical": name, "category": "person", "aliases": aliases})
    return terms


def brief_places(brief: Dict[str, object]) -> List[Dict[str, object]]:
    places = brief.get("places", []) if isinstance(brief, dict) else []
    return [place for place in places if isinstance(place, dict)]


def brief_story_anchors(brief: Dict[str, object]) -> List[Dict[str, object]]:
    anchors = brief.get("story_anchors", []) if isinstance(brief, dict) else []
    return [anchor for anchor in anchors if isinstance(anchor, dict)]


def brief_time_anchors(brief: Dict[str, object]) -> List[Dict[str, object]]:
    anchors = brief.get("time_anchors", []) if isinstance(brief, dict) else []
    return [anchor for anchor in anchors if isinstance(anchor, dict)]


def brief_editorial_brief_text(brief: Dict[str, object]) -> str:
    editorial = brief.get("editorial_brief", {}) if isinstance(brief, dict) else {}
    if isinstance(editorial, str):
        return editorial.strip()
    if not isinstance(editorial, dict):
        return ""

    lines: List[str] = []
    for key in (
        "summary",
        "tone",
        "target_runtime",
        "meal_guidance",
        "pacing_guidance",
        "opener_guidance",
        "closer_guidance",
    ):
        value = str(editorial.get(key, "")).strip()
        if value:
            lines.append(f"{key}: {value}")

    for key in ("must_include", "avoid", "story_goals"):
        values = editorial.get(key, [])
        if not isinstance(values, list):
            continue
        cleaned = [str(item).strip() for item in values if str(item).strip()]
        if cleaned:
            lines.append(f"{key}: " + "; ".join(cleaned))

    return "\n".join(lines).strip()


def brief_target_runtime_seconds(brief: Dict[str, object], default_minutes: int = 40) -> int:
    editorial = brief.get("editorial_brief", {}) if isinstance(brief, dict) else {}
    runtime_hint = str(editorial.get("target_runtime", "")).strip() if isinstance(editorial, dict) else ""
    text = runtime_hint or brief_editorial_brief_text(brief)
    if not text:
        return default_minutes * 60

    range_match = re.search(r"(\d+)\s*(?:~|-)\s*(\d+)\s*(?:분|minutes|min)", text, re.IGNORECASE)
    if range_match:
        start = int(range_match.group(1))
        end = int(range_match.group(2))
        return max(60, int(((start + end) / 2) * 60))

    hour_min_match = re.search(r"(\d+)\s*(?:시간|hours|hour)\s*(\d+)\s*(?:분|minutes|min)", text, re.IGNORECASE)
    if hour_min_match:
        return int(hour_min_match.group(1)) * 3600 + int(hour_min_match.group(2)) * 60

    hour_match = re.search(r"(\d+)\s*(?:시간|hours|hour)", text, re.IGNORECASE)
    if hour_match:
        return int(hour_match.group(1)) * 3600

    min_match = re.search(r"(\d+)\s*(?:분|minutes|min)", text, re.IGNORECASE)
    if min_match:
        return int(min_match.group(1)) * 60

    return default_minutes * 60


def brief_prompt_terms(brief: Dict[str, object]) -> List[str]:
    prompt_terms: List[str] = []
    for person in brief_people_terms(brief):
        prompt_terms.append(str(person["canonical"]))
        prompt_terms.extend(list(person.get("aliases", [])))
    for place in brief_places(brief):
        name = str(place.get("name", "")).strip()
        if name:
            prompt_terms.append(name)
        prompt_terms.extend(str(alias).strip() for alias in place.get("aliases", []) if str(alias).strip())
    for anchor in brief_story_anchors(brief):
        label = str(anchor.get("label", "")).strip()
        if label:
            prompt_terms.append(label)
        prompt_terms.extend(str(keyword).strip() for keyword in anchor.get("keywords", []) if str(keyword).strip())
    transcription = brief.get("transcription", {}) if isinstance(brief, dict) else {}
    if isinstance(transcription, dict):
        prompt_terms.extend(str(term).strip() for term in transcription.get("prompt_terms", []) if str(term).strip())
    return list(dict.fromkeys(term for term in prompt_terms if term))


def brief_replacement_rules(brief: Dict[str, object]) -> List[Dict[str, object]]:
    transcription = brief.get("transcription", {}) if isinstance(brief, dict) else {}
    if not isinstance(transcription, dict):
        return []
    return [copy.deepcopy(rule) for rule in transcription.get("replacement_rules", []) if isinstance(rule, dict)]


def brief_default_timezone(brief: Dict[str, object]) -> str | None:
    project = brief.get("project", {}) if isinstance(brief, dict) else {}
    timezone_name = str(project.get("default_timezone", "")).strip() if isinstance(project, dict) else ""
    return timezone_name or None


def _first_nonempty_line(text: str) -> str:
    for line in text.splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned
    return ""


def _clean_derived_title(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip(" .,-")
    cleaned = re.sub(r"(로|으로)?\s*(편집|정리|요약|만들어)\s*해\s*줘\.?$", "", cleaned)
    cleaned = re.sub(r"\s*please\s+(edit|make|turn).*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip(" .,-")
    return cleaned


def _extract_explicit_title(prompt: str) -> str:
    patterns = [
        r"(?im)(?:제목|타이틀|프로젝트명)\s*[:：]\s*[\"“']?([^\n\"”']+)",
        r"(?im)(?:title|project name)\s*[:：]\s*[\"“']?([^\n\"”']+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, prompt)
        if match:
            return _clean_derived_title(match.group(1))
    return ""


def _normalize_location_candidate(text: str) -> str:
    cleaned = _clean_derived_title(text)
    if re.search(r"[가-힣]", cleaned) and " " in cleaned:
        cleaned = cleaned.split()[-1]
    cleaned = re.sub(r"(한국|현지|여행지)\s+(공항|숙소)$", r"\1 \2", cleaned)
    cleaned = re.sub(r"\s*(출발|도착|복귀|귀가)$", "", cleaned).strip()
    return cleaned


def _extract_location_candidates(prompt: str) -> List[str]:
    patterns = [
        r"([A-Za-z][A-Za-z0-9&'. -]{1,24}|[가-힣A-Za-z0-9]+(?:\s*[가-힣A-Za-z0-9]+){0,2})\s*(?:에서|으로|로)",
        r"([A-Za-z][A-Za-z0-9&'. -]{1,24}|[가-힣A-Za-z0-9]+(?:\s*[가-힣A-Za-z0-9]+){0,2})\s*(?:가족\s*)?(?:여행|브이로그)",
        r"(?:to|in)\s+([A-Za-z][A-Za-z0-9&'. -]{1,24})",
    ]
    generic = {"가족", "여행", "브이로그", "영상", "장면", "공항", "숙소", "리조트", "호텔", "한국", "현지", "집"}
    results: List[str] = []
    for pattern in patterns:
        for match in re.findall(pattern, prompt, flags=re.IGNORECASE):
            candidate = _normalize_location_candidate(match)
            if not candidate:
                continue
            if candidate in generic:
                continue
            if re.search(r"(하는|가는|오는|있는|같은|보내는|돌아오는|떠나는|출발해|도착해|따뜻한|따뜻하게|여유로운|즐거운)$", candidate):
                continue
            if len(candidate) > 24:
                continue
            if candidate not in results:
                results.append(candidate)
    return results


def _seasonal_prefix(prompt: str) -> str:
    lowered = prompt.lower()
    mapping = [
        ("연말", "연말"),
        ("연초", "연초"),
        ("겨울", "겨울"),
        ("여름", "여름"),
        ("봄", "봄"),
        ("가을", "가을"),
        ("신혼여행", "신혼여행"),
    ]
    for needle, label in mapping:
        if needle in lowered:
            return label
    return ""


def _format_date(value: date) -> str:
    return value.strftime("%Y.%m.%d")


def _metadata_year(source_times: Sequence[str]) -> int | None:
    values = [datetime.fromisoformat(value).date() for value in source_times if value]
    if not values:
        return None
    return sorted(values)[0].year


def _metadata_date_bounds(source_times: Sequence[str]) -> tuple[date | None, date | None]:
    values = sorted(datetime.fromisoformat(value).date() for value in source_times if value)
    if not values:
        return None, None
    return values[0], values[-1]


def _resolve_month_day(month_value: str, day_value: str, source_times: Sequence[str]) -> date | None:
    start_date, end_date = _metadata_date_bounds(source_times)
    candidate_years: List[int] = []
    if start_date is not None:
        candidate_years.append(start_date.year)
    if end_date is not None and end_date.year not in candidate_years:
        candidate_years.append(end_date.year)
    if not candidate_years:
        metadata_year = _metadata_year(source_times)
        if metadata_year is not None:
            candidate_years.append(metadata_year)
    candidates: List[date] = []
    for year_value in candidate_years:
        try:
            candidates.append(date(year_value, int(month_value), int(day_value)))
        except ValueError:
            continue
    if not candidates:
        return None
    if start_date is not None and end_date is not None:
        in_range = [candidate for candidate in candidates if start_date <= candidate <= end_date]
        if in_range:
            return sorted(in_range)[0]
    return sorted(candidates)[0]


def derive_prompt_date_span(prompt_text: str | None, source_times: Sequence[str] = ()) -> str:
    prompt = (prompt_text or "").strip()
    if not prompt:
        return ""

    matches: List[date] = []
    metadata_year = _metadata_year(source_times)

    full_patterns = [
        r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})",
        r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일",
    ]
    for pattern in full_patterns:
        for year_value, month_value, day_value in re.findall(pattern, prompt):
            try:
                parsed = date(int(year_value), int(month_value), int(day_value))
            except ValueError:
                continue
            if parsed not in matches:
                matches.append(parsed)

    if not matches and metadata_year is not None:
        for month_value, day_value in re.findall(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", prompt):
            parsed = _resolve_month_day(month_value, day_value, source_times)
            if parsed is not None and parsed not in matches:
                matches.append(parsed)

    if not matches:
        return ""
    ordered = sorted(matches)
    return _format_date(ordered[0]) if len(ordered) == 1 else f"{_format_date(ordered[0])} - {_format_date(ordered[-1])}"


def derive_video_title(brief: Dict[str, object], prompt_text: str | None = None) -> str:
    prompt = (prompt_text or "").strip()
    editorial = brief.get("editorial_brief", {}) if isinstance(brief, dict) else {}
    summary = str(editorial.get("summary", "")).strip() if isinstance(editorial, dict) else ""

    if prompt:
        explicit = _extract_explicit_title(prompt)
        if explicit:
            return explicit

    locations = _extract_location_candidates(prompt or summary)
    title_kind = "그룹 여행" if any(word in (prompt or summary) for word in ("가족", "family", "friends", "커플", "동행")) else "여행"
    prefix = _seasonal_prefix(prompt or summary)
    if locations:
        destination = locations[-1]
        title = f"{destination} {title_kind}".strip()
        if prefix and prefix not in title:
            title = f"{prefix} {title}"
        if len(title) <= 24:
            return title

    candidates: List[str] = [_first_nonempty_line(prompt), _first_nonempty_line(summary)]
    for candidate in candidates:
        cleaned = _clean_derived_title(candidate)
        if 2 <= len(cleaned) <= 28:
            return cleaned

    description = str(brief.get("project", {}).get("description", "")).strip() if isinstance(brief.get("project", {}), dict) else ""
    return description or "여행 영상"


def brief_preview_preset_ids(brief: Dict[str, object]) -> List[str]:
    render = brief.get("render", {}) if isinstance(brief, dict) else {}
    values = render.get("preview_presets", []) if isinstance(render, dict) else []
    if not isinstance(values, list):
        return list(DEFAULT_PREVIEW_PRESETS)
    presets = [str(item).strip() for item in values if str(item).strip()]
    return presets or list(DEFAULT_PREVIEW_PRESETS)


def brief_selected_preset(brief: Dict[str, object]) -> str:
    render = brief.get("render", {}) if isinstance(brief, dict) else {}
    value = str(render.get("selected_preset", "")).strip() if isinstance(render, dict) else ""
    return value


def brief_final_render_mode(brief: Dict[str, object]) -> str:
    render = brief.get("render", {}) if isinstance(brief, dict) else {}
    value = str(render.get("final_render_mode", "")).strip() if isinstance(render, dict) else ""
    return value or "chunked_direct_mp4"


def brief_final_feedback(brief: Dict[str, object]) -> List[str]:
    render = brief.get("render", {}) if isinstance(brief, dict) else {}
    values = render.get("final_feedback", []) if isinstance(render, dict) else []
    if not isinstance(values, list):
        return []
    return [str(item).strip() for item in values if str(item).strip()]


def brief_render_config(brief: Dict[str, object]) -> Dict[str, object]:
    render = brief.get("render", {}) if isinstance(brief, dict) else {}
    return render if isinstance(render, dict) else {}


def brief_location_timezones(brief: Dict[str, object]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for place in brief_places(brief):
        place_id = str(place.get("id", "")).strip()
        timezone_name = str(place.get("timezone", "")).strip()
        if place_id and timezone_name:
            result[place_id] = timezone_name
    return result


def _story_anchor_lookup(brief: Dict[str, object]) -> Dict[str, Dict[str, object]]:
    return {
        str(anchor.get("id", "")).strip(): anchor
        for anchor in brief_story_anchors(brief)
        if str(anchor.get("id", "")).strip()
    }


def resolve_brief_time_context(
    brief: Dict[str, object],
    creation_time: datetime | None,
) -> Dict[str, str]:
    fallback_timezone = brief_default_timezone(brief) or "UTC"
    if creation_time is None:
        return {
            "timezone_name": fallback_timezone,
            "location_id": "",
            "route_phase": "",
            "route_label": "",
        }

    locations = brief_location_timezones(brief)
    story_lookup = _story_anchor_lookup(brief)
    for anchor in brief_time_anchors(brief):
        timezone_name = str(anchor.get("timezone", "")).strip()
        location_id = str(anchor.get("location_id", "")).strip()
        story_anchor_id = str(anchor.get("story_anchor_id", "")).strip()
        if not timezone_name:
            timezone_name = locations.get(location_id, fallback_timezone)
        try:
            zone = ZoneInfo(timezone_name)
        except Exception:
            timezone_name = fallback_timezone
            zone = ZoneInfo(fallback_timezone)

        starts_at = str(anchor.get("starts_at", "")).strip()
        ends_at = str(anchor.get("ends_at", "")).strip()
        if not starts_at and not ends_at:
            continue

        local_time = creation_time.astimezone(zone)
        in_range = True
        if starts_at:
            start_dt = datetime.fromisoformat(starts_at).replace(tzinfo=zone)
            in_range = in_range and local_time >= start_dt
        if ends_at:
            end_dt = datetime.fromisoformat(ends_at).replace(tzinfo=zone)
            in_range = in_range and local_time <= end_dt
        if in_range:
            story_anchor = story_lookup.get(story_anchor_id, {})
            story_label = str(story_anchor.get("label", "")).strip() or str(anchor.get("label", "")).strip()
            return {
                "timezone_name": timezone_name,
                "location_id": location_id,
                "route_phase": story_anchor_id,
                "route_label": story_label,
            }

    return {
        "timezone_name": fallback_timezone,
        "location_id": "",
        "route_phase": "",
        "route_label": "",
    }
