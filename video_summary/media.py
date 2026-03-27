from __future__ import annotations

import json
import math
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Optional
from zoneinfo import ZoneInfo

from .brief import resolve_brief_time_context
from .models import ClipInfo


VIDEO_GLOB_EXTENSIONS = ("*.mp4", "*.MP4", "*.mov", "*.MOV", "*.m4v", "*.M4V")
FILENAME_TS = re.compile(r"(\d{8})_(\d{6})")


def _run_json(command: List[str]) -> dict:
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _parse_ratio(value: str) -> float:
    if not value or value == "0/0":
        return 30.0
    if "/" in value:
        left, right = value.split("/", 1)
        if float(right) == 0:
            return 30.0
        return float(left) / float(right)
    return float(value)


def _parse_filename_timestamp(filename: str, timezone_name: Optional[str] = None) -> Optional[datetime]:
    match = FILENAME_TS.search(filename)
    if not match:
        return None
    parsed = datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S")
    if timezone_name:
        return parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed


def _parse_creation_time(raw_value: str) -> Optional[datetime]:
    if not raw_value:
        return None
    cleaned = raw_value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def _display_dimensions(video_stream: dict) -> tuple[int, int]:
    width = int(video_stream.get("width", 0))
    height = int(video_stream.get("height", 0))
    rotation = 0
    tags = video_stream.get("tags") or {}
    if "rotate" in tags:
        try:
            rotation = int(tags["rotate"])
        except ValueError:
            rotation = 0
    for side_data in video_stream.get("side_data_list") or []:
        if "rotation" in side_data:
            rotation = int(side_data["rotation"])
            break
    if rotation in (90, 270, -90, -270):
        return height, width
    return width, height


def _project_datetime(
    path: Path,
    filename: str,
    creation_time: Optional[datetime],
    timezone_name: Optional[str],
) -> datetime:
    project_zone = ZoneInfo(timezone_name) if timezone_name else None
    if creation_time is not None:
        if project_zone is not None:
            return creation_time.astimezone(project_zone)
        return creation_time

    filename_time = _parse_filename_timestamp(filename, timezone_name)
    if filename_time is not None:
        return filename_time

    return datetime.fromtimestamp(path.stat().st_mtime, tz=project_zone)


def _travel_day_key(start_time: datetime, timezone_name: Optional[str], day_start_hour: int) -> str:
    if start_time.tzinfo is not None:
        local = start_time.astimezone(ZoneInfo(timezone_name)) if timezone_name else start_time
        local_naive = local.replace(tzinfo=None)
    else:
        local_naive = start_time
    return (local_naive - timedelta(hours=max(0, day_start_hour))).date().isoformat()


def probe_media_file(
    path: Path,
    timezone_name: Optional[str] = None,
    day_start_hour: int = 0,
    brief: Optional[dict] = None,
) -> ClipInfo:
    data = _run_json(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ]
    )
    streams = data.get("streams", [])
    format_info = data.get("format", {})
    video_stream = next(stream for stream in streams if stream.get("codec_type") == "video")
    audio_stream = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    width, height = _display_dimensions(video_stream)
    duration = float(format_info.get("duration") or video_stream.get("duration") or 0.0)
    fps = _parse_ratio(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate") or "30/1")
    filename = path.name
    size_bytes = int(format_info.get("size") or path.stat().st_size)
    creation_time = _parse_creation_time((format_info.get("tags") or {}).get("creation_time", ""))
    time_context = resolve_brief_time_context(brief, creation_time) if brief else {
        "timezone_name": timezone_name or "",
        "location_id": "",
        "route_phase": "",
        "route_label": "",
    }
    effective_timezone = str(time_context.get("timezone_name") or timezone_name or "")
    start_time = _project_datetime(path, filename, creation_time, effective_timezone or timezone_name)
    bitrate_mbps = round((size_bytes * 8.0) / max(duration, 0.001) / 1_000_000, 3)
    return ClipInfo(
        filename=filename,
        path=str(path.resolve()),
        start_time=start_time,
        duration=duration,
        width=width,
        height=height,
        fps=fps,
        has_audio=audio_stream is not None,
        size_bytes=size_bytes,
        bitrate_mbps=bitrate_mbps,
        date_key=_travel_day_key(start_time, effective_timezone or timezone_name, day_start_hour),
        timezone_name=effective_timezone,
        location_id=str(time_context.get("location_id", "")),
        route_phase=str(time_context.get("route_phase", "")),
        route_label=str(time_context.get("route_label", "")),
    )


def scan_media_directory(
    directory: Path,
    timezone_name: Optional[str] = None,
    day_start_hour: int = 0,
    brief: Optional[dict] = None,
) -> List[ClipInfo]:
    files: List[Path] = []
    for pattern in VIDEO_GLOB_EXTENSIONS:
        files.extend(directory.glob(pattern))
    clips = [
        probe_media_file(path, timezone_name=timezone_name, day_start_hour=day_start_hour, brief=brief)
        for path in sorted(files)
    ]
    clips.sort(key=lambda clip: (clip.start_time, clip.filename))
    return clips


def dominant_fps(clips: Iterable[ClipInfo]) -> float:
    buckets = {}
    for clip in clips:
        rounded = round(clip.fps, 3)
        buckets[rounded] = buckets.get(rounded, 0) + clip.duration
    if not buckets:
        return 29.97
    return max(buckets.items(), key=lambda item: item[1])[0]


def human_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"
