from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from .utils import VideoSummaryError


INTRO_METADATA_POLICY_VERSION = 1

_GENERIC_TOKENS = {
    "camera",
    "clip",
    "clips",
    "dcim",
    "dji",
    "footage",
    "holiday",
    "media",
    "osmo",
    "raw",
    "summary",
    "travel",
    "trip",
    "vacation",
    "video",
    "videos",
    "영상",
    "여행",
    "원본",
}
_COUNTRY_TOKENS = {
    "america",
    "england",
    "france",
    "indonesia",
    "italy",
    "japan",
    "jp",
    "korea",
    "kr",
    "southkorea",
    "spain",
    "thailand",
    "uk",
    "us",
    "usa",
    "vietnam",
    "vn",
    "미국",
    "베트남",
    "일본",
    "한국",
}
_COUNTRY_PHRASES = {
    ("new", "zealand"),
    ("south", "korea"),
    ("united", "kingdom"),
    ("united", "states"),
}
_DESTINATION_ALIASES = {
    "danang": "Da Nang",
    "hcm": "Ho Chi Minh City",
    "hochiminh": "Ho Chi Minh City",
    "hochiminhcity": "Ho Chi Minh City",
    "la": "Los Angeles",
    "losangeles": "Los Angeles",
    "newyork": "New York",
    "nyc": "New York",
    "okinawa": "Okinawa",
    "phuquoc": "Phu Quoc",
    "sanfrancisco": "San Francisco",
    "sf": "San Francisco",
    "샌프란시스코": "샌프란시스코",
    "오키나와": "오키나와",
    "푸꾸옥": "푸꾸옥",
}
_DATE_TOKEN = re.compile(r"(?:19|20)\d{6}|(?:19|20)\d{4}|\d{4}")
_LEADING_DATE_PREFIX = re.compile(
    r"^\s*(?:"
    r"(?:19|20)\d{6}|"
    r"(?:19|20)\d{4}|"
    r"(?:19|20)\d{2}(?:[-_.](?:0?[1-9]|1[0-2]))?"
    r"(?:[-_.](?:0?[1-9]|[12]\d|3[01]))?|"
    r"\d{4}"
    r")(?:[-_.\s]+|$)"
)
_CAMERA_FOLDER_TOKEN = re.compile(r"\d{3}(?:apple|dji|gopro|media)")


@dataclass(frozen=True, slots=True)
class IntroMetadata:
    destination: str
    destination_source: str
    start_date: str
    end_date: str
    period: str
    period_source: str = "scan_day_key"
    policy_version: int = INTRO_METADATA_POLICY_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_intro_metadata(
    config: dict[str, Any],
    manifest: dict[str, Any],
    fallback_day_keys: Iterable[str] = (),
) -> IntroMetadata:
    project = config.get("project", {})
    configured = str(project.get("destination", ""))
    project_name = str(project.get("name", ""))
    source_dir = str(manifest.get("source_dir", ""))
    destination, destination_source = infer_destination(configured, source_dir, project_name)

    manifest_days = manifest.get("days", [])
    day_keys = [
        str(item.get("day_key", ""))
        for item in manifest_days
        if isinstance(item, dict) and item.get("day_key")
    ]
    if not day_keys:
        day_keys = [str(value) for value in fallback_day_keys if value]
        period_source = "edit_plan_day_key"
    else:
        period_source = "scan_day_key"
    start_date, end_date, period = infer_travel_period(day_keys)
    return IntroMetadata(
        destination=destination,
        destination_source=destination_source,
        start_date=start_date,
        end_date=end_date,
        period=period,
        period_source=period_source,
    )


def infer_destination(configured: str, source_dir: str, project_name: str) -> tuple[str, str]:
    explicit = _normalize_spaces(configured)
    if explicit:
        return explicit, "config"

    source_name = Path(source_dir).name if source_dir else ""
    source_destination = _humanize_destination(source_name)
    if source_destination:
        return source_destination, "source_dir"

    project_destination = _humanize_destination(project_name)
    if project_destination:
        return project_destination, "project"
    return "여행", "fallback"


def infer_travel_period(day_keys: Iterable[str]) -> tuple[str, str, str]:
    parsed: set[date] = set()
    for value in day_keys:
        normalized = str(value).strip()
        if not normalized:
            continue
        try:
            parsed.add(date.fromisoformat(normalized))
        except ValueError as exc:
            raise VideoSummaryError(f"여행기간을 계산할 수 없는 day_key입니다: {normalized}") from exc
    if not parsed:
        raise VideoSummaryError("여행기간을 계산할 day_key가 없습니다. scan을 다시 실행하세요.")
    ordered = sorted(parsed)
    start = ordered[0].isoformat()
    end = ordered[-1].isoformat()
    period = start if start == end else f"{start} — {end}"
    return start, end, period


def format_day_period(day_key: str) -> str:
    _start, _end, period = infer_travel_period([day_key])
    return period


def _humanize_destination(value: str) -> str:
    without_date_prefix = _LEADING_DATE_PREFIX.sub("", _normalize_spaces(value), count=1)
    tokens = [
        token
        for token in re.split(r"[_\W]+", without_date_prefix, flags=re.UNICODE)
        if token
    ]
    meaningful: list[str] = []
    for token in tokens:
        folded = token.casefold()
        if _DATE_TOKEN.fullmatch(folded):
            continue
        if folded in _GENERIC_TOKENS or _CAMERA_FOLDER_TOKEN.fullmatch(folded):
            continue
        meaningful.append(token)
    if not meaningful:
        return ""

    meaningful = _remove_country_tokens_when_specific(meaningful)

    collapsed = "".join(token.casefold() for token in meaningful)
    alias = _DESTINATION_ALIASES.get(collapsed)
    if alias:
        return alias
    return " ".join(_display_token(token) for token in meaningful)


def _remove_country_tokens_when_specific(tokens: list[str]) -> list[str]:
    folded = [token.casefold() for token in tokens]
    country_indexes = {index for index, token in enumerate(folded) if token in _COUNTRY_TOKENS}
    for phrase in _COUNTRY_PHRASES:
        phrase_length = len(phrase)
        for start in range(len(folded) - phrase_length + 1):
            if tuple(folded[start : start + phrase_length]) == phrase:
                country_indexes.update(range(start, start + phrase_length))
    non_country = [token for index, token in enumerate(tokens) if index not in country_indexes]
    return non_country or tokens


def _display_token(token: str) -> str:
    if any("가" <= character <= "힣" for character in token):
        return token
    if token.isupper() and 1 < len(token) <= 4:
        return token
    return token[:1].upper() + token[1:].lower()


def _normalize_spaces(value: str) -> str:
    return " ".join(str(value).split())
