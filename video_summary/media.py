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
_CAPTURE_TIME_POLICY_VERSION = 9
VISUAL_SIGNAL_POLICY_VERSION = 2
_UNPLACED_DAY_KEY = "unplaced"
_GLOBALLY_PLACED_BASES = frozenset({"absolute", "estimated"})
_FILENAME_PATTERNS = (
    re.compile(r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])([0-2]\d|3[01])[_-]?([0-2]\d)([0-5]\d)([0-5]\d)(?!\d)"),
    re.compile(r"(?<!\d)(20\d{2})[-_](0[1-9]|1[0-2])[-_]([0-2]\d|3[01])[ T_-]([0-2]\d)[-_:]?([0-5]\d)[-_:]?([0-5]\d)(?!\d)"),
)
_DJI_MIMO_TIMESTAMP_PATTERN = re.compile(
    r"^dji_mimo_\d{8}_\d{6}_(20\d{12})(?:_|\.)",
    re.IGNORECASE,
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
            "version": 3,
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
        probed: list[
            tuple[
                Path,
                dict[str, Any],
                datetime,
                str,
                datetime,
                str,
                str,
                str,
                str | None,
                str | None,
                list[str],
            ]
        ] = []
        for index, path in enumerate(files, start=1):
            print_status(f"scan {index}/{len(files)}: {path.name}")
            probe = probe_media(path)
            relative = path.relative_to(source)
            camera_make = _metadata_tag(probe, "com.apple.quicktime.make", "make")
            camera_model = _metadata_tag(probe, "com.apple.quicktime.model", "model")
            source_kind = _source_kind(relative, camera_make, camera_model)
            override = _date_override(relative, config.get("date_overrides", []))
            captured, capture_source, warnings = infer_capture_time(
                path,
                relative,
                probe,
                timezone,
                config.get("date_overrides", []),
            )
            sequence_at, sequence_source, sequence_warnings = infer_sequence_time(
                path,
                captured,
                capture_source,
                timezone,
                override,
                source_kind=source_kind,
            )
            warnings.extend(sequence_warnings)
            capture_time_basis = _capture_time_basis(
                capture_source,
                override,
                source_kind=source_kind,
                sequence_source=sequence_source,
            )
            capture_time_confidence = _capture_time_confidence(
                capture_source,
                override,
                source_kind=source_kind,
                sequence_source=sequence_source,
            )
            probed.append(
                (
                    path,
                    probe,
                    captured,
                    capture_source,
                    sequence_at,
                    sequence_source,
                    capture_time_basis,
                    capture_time_confidence,
                    camera_make,
                    camera_model,
                    warnings,
                )
            )

        probed.sort(
            key=lambda item: (
                item[6] not in _GLOBALLY_PLACED_BASES,
                item[4].timestamp() if item[6] in _GLOBALLY_PLACED_BASES else 0.0,
                str(item[0]).casefold(),
            )
        )
        day_start_hour = int(project_config["day_start_hour"])
        day_keys = sorted(
            {
                _day_key(item[4], day_start_hour)
                for item in probed
                if item[6] in _GLOBALLY_PLACED_BASES
            }
        )
        day_number = {key: index + 1 for index, key in enumerate(day_keys)}
        clips: list[Clip] = []
        for (
            path,
            probe,
            captured,
            capture_source,
            sequence_at,
            sequence_source,
            capture_time_basis,
            capture_time_confidence,
            camera_make,
            camera_model,
            warnings,
        ) in probed:
            relative = path.relative_to(source)
            if capture_time_basis in _GLOBALLY_PLACED_BASES:
                day_key = _day_key(sequence_at, day_start_hour)
                travel_day = day_number[day_key]
            else:
                day_key = _UNPLACED_DAY_KEY
                travel_day = 0
            video = _video_stream(probe)
            audio = _audio_stream(probe)
            source_kind = _source_kind(relative, camera_make, camera_model)
            source_stream_id = _source_stream_id(
                relative,
                source_kind=source_kind,
                camera_make=camera_make,
                camera_model=camera_model,
            )
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
                    travel_day=travel_day,
                    width=int(video.get("width", 0) or 0),
                    height=int(video.get("height", 0) or 0),
                    fps=round(_fps(video), 4),
                    codec=str(video.get("codec_name", "unknown")),
                    rotation=_rotation(video),
                    has_audio=bool(audio),
                    audio_sample_rate=int(audio.get("sample_rate", 0) or 0) if audio else None,
                    location=location,
                    warnings=warnings,
                    pix_fmt=_optional_probe_text(video.get("pix_fmt")),
                    color_space=_optional_probe_text(video.get("color_space")),
                    color_transfer=_optional_probe_text(video.get("color_transfer")),
                    color_primaries=_optional_probe_text(video.get("color_primaries")),
                    color_range=_optional_probe_text(video.get("color_range")),
                    dolby_vision_profile=_dolby_vision_profile(video),
                    camera_make=camera_make,
                    camera_model=camera_model,
                    source_kind=source_kind,
                    source_stream_id=source_stream_id,
                    capture_time_confidence=capture_time_confidence,
                    sequence_at=sequence_at.isoformat(),
                    sequence_source=sequence_source,
                    capture_time_basis=capture_time_basis,
                )
            )

        manifest = {
            "version": 2,
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
            try:
                embedded = _parse_datetime_raw(embedded_raw)
            except VideoSummaryError:
                warnings.append("메타데이터 촬영 시각을 해석할 수 없어 무시했습니다.")
            else:
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
        try:
            embedded = _parse_datetime(embedded_raw, timezone)
        except VideoSummaryError:
            warnings.append("메타데이터 촬영 시각을 해석할 수 없어 무시했습니다.")
        else:
            if filename_time and abs((embedded - filename_time).total_seconds()) > 12 * 3600:
                warnings.append("메타데이터 시각과 파일명 시각이 12시간 이상 다릅니다.")
            return embedded, "metadata", warnings
    if filename_time:
        return filename_time, "filename", warnings

    warnings.append("촬영 시각이 없어 파일 수정 시각을 사용했습니다.")
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone), "mtime", warnings


def infer_sequence_time(
    path: Path,
    captured_at: datetime,
    capture_source: str,
    timezone: ZoneInfo,
    override: dict[str, Any] | None,
    *,
    source_kind: str,
) -> tuple[datetime, str, list[str]]:
    """Return the auditable time used to order clips without rewriting capture metadata.

    Native phone timestamps remain fixed anchors.  A project may calibrate a
    reset camera clock with ``clock_offset_seconds``.  DJI Mimo exports have a
    second filename timestamp that represents the actual recording time.  Use
    that value consistently for sequencing, even when generic container
    metadata differs by only a second or two, while keeping the original
    ``captured_at`` available for audit.
    """
    warnings: list[str] = []
    override = override or None

    # Messenger exports may retain an arbitrary container timestamp or only the
    # local filesystem mtime.  Neither is a recording-time anchor.  Keep the
    # value for audit, but require an explicit per-file captured_at before this
    # source can contribute a trip day or be interleaved with native cameras.
    if source_kind == "shared" and capture_source != "override":
        warnings.append(
            "공유받은 영상의 촬영 시각 근거가 없어 자동 날짜/카메라 간 순서에서 제외했습니다."
        )
        return captured_at, "shared_unplaced", warnings

    # DJI Mimo's second datetime token is the recording clock, while the first
    # token and generic container creation time can describe an export.  A
    # date-only override must therefore rebase this second clock to the reviewed
    # local date instead of silently falling back to the container clock.
    sequence_base = captured_at
    sequence_base_source = capture_source
    if source_kind == "action_camera" and capture_source != "override":
        sequence_timezone = timezone
        if override and override.get("timezone"):
            sequence_timezone = _timezone(str(override["timezone"]))
        filename_time = _dji_mimo_capture_datetime(path.name, sequence_timezone)
        if filename_time is not None:
            if override and override.get("date"):
                filename_time = _replace_wall_date(
                    filename_time,
                    _parse_override_date(str(override["date"])),
                    sequence_timezone,
                )
                sequence_base_source = "date_override:dji_mimo_filename"
            else:
                sequence_base_source = "dji_mimo_filename"
            sequence_base = filename_time
            if abs((captured_at - filename_time).total_seconds()) > 60.0:
                warnings.append(
                    "DJI Mimo 파일명의 두 번째 촬영 시각과 컨테이너 시각이 달라 "
                    "파일명 시각을 영상 순서에 사용했습니다."
                )

    # The presence of the key is itself an auditable manual review decision.
    # An explicit zero offset means the relative camera clock was checked and
    # accepted, while omission means it must not be placed across streams.
    has_clock_offset = bool(override and "clock_offset_seconds" in override)
    offset_seconds = float((override or {}).get("clock_offset_seconds", 0.0))
    if has_clock_offset:
        warnings.append(
            f"카메라 시계 보정값 {offset_seconds:+g}초를 영상 순서에 적용했습니다."
        )
        return (
            sequence_base + timedelta(seconds=offset_seconds),
            (
                "manual_clock_offset:dji_mimo_filename"
                if sequence_base_source.endswith("dji_mimo_filename")
                else "manual_clock_offset"
            ),
            warnings,
        )

    if sequence_base_source != capture_source:
        return sequence_base, sequence_base_source, warnings

    if (
        source_kind == "action_camera"
        and override
        and override.get("date")
        and capture_source.startswith("date_override:")
    ):
        warnings.append(
            "날짜만 보정된 액션캠 시계는 카메라 내부 순서만 신뢰할 수 있어 "
            "자동 카메라 간 배치에서 제외했습니다."
        )
        return captured_at, "relative_clock_unplaced", warnings

    return captured_at, capture_source, warnings


def resolve_location(
    relative_path: Path,
    day_key: str,
    rules: Any,
    *,
    transcript: str | None = None,
) -> str | None:
    """Resolve the first matching path, day, or transcript location rule.

    ``day_key`` always scopes a rule. ``match`` and ``keywords`` are alternate
    selectors so a location can be recognized from either its source path or its
    spoken transcript. A rule without ``match`` is only an implicit wildcard when
    it also has no keywords; this keeps keyword-only rules from labeling clips
    before transcription is available.
    """
    if not isinstance(rules, list):
        return None
    relative = str(relative_path)
    lowered_transcript = transcript.casefold() if transcript is not None else None
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if rule.get("day_key") and str(rule["day_key"]) != day_key:
            continue

        keywords = _location_keywords(rule)
        has_keyword_selector = "keywords" in rule
        raw_patterns = rule.get("match")
        if raw_patterns is None:
            path_matches = not has_keyword_selector
        else:
            patterns = [raw_patterns] if isinstance(raw_patterns, str) else raw_patterns
            path_matches = isinstance(patterns, list) and any(
                fnmatch.fnmatch(relative, str(pattern))
                or fnmatch.fnmatch(relative_path.name, str(pattern))
                for pattern in patterns
            )
        transcript_matches = lowered_transcript is not None and any(
            keyword in lowered_transcript for keyword in keywords
        )
        if not (path_matches or transcript_matches):
            continue

        label = str(rule.get("label", "")).strip()
        if label:
            return label
    return None


def _location_keywords(rule: dict[str, Any]) -> list[str]:
    raw_keywords = rule.get("keywords", [])
    if isinstance(raw_keywords, str):
        raw_keywords = [raw_keywords]
    if not isinstance(raw_keywords, list):
        return []
    return [str(keyword).strip().casefold() for keyword in raw_keywords if str(keyword).strip()]


def analyze_visual_signals(
    paths: ProjectPaths,
    clip: Clip,
    interval: float,
    *,
    force: bool = False,
) -> list[dict[str, float]]:
    cache_path = paths.signals / f"{clip.clip_id}.json"
    cache_key = stable_hash(
        {
            "version": VISUAL_SIGNAL_POLICY_VERSION,
            "fingerprint": clip.fingerprint,
            "interval": interval,
        }
    )
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
    hardware_args = _visual_signal_ffmpeg_args(clip.path, vf, hardware_decode=True)
    samples, return_code, hardware_error = _decode_visual_signal_frames(
        hardware_args,
        frame_size=frame_size,
        interval=interval,
    )
    if return_code != 0:
        software_args = _visual_signal_ffmpeg_args(clip.path, vf, hardware_decode=False)
        samples, return_code, software_error = _decode_visual_signal_frames(
            software_args,
            frame_size=frame_size,
            interval=interval,
        )
        if return_code != 0:
            details = software_error.strip() or f"exit {return_code}"
            if hardware_error.strip():
                details = f"hardware: {hardware_error.strip()}\nsoftware: {details}"
            raise VideoSummaryError(f"프레임 분석 실패 ({Path(clip.path).name}): {details}")

    write_json(
        cache_path,
        {
            "version": VISUAL_SIGNAL_POLICY_VERSION,
            "cache_key": cache_key,
            "samples": samples,
        },
    )
    return samples


def _visual_signal_ffmpeg_args(path: str, vf: str, *, hardware_decode: bool) -> list[str]:
    args = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
    ]
    if hardware_decode:
        args.extend(["-hwaccel", "auto"])
    args.extend(
        [
            "-threads",
            "1",
            "-i",
            path,
            "-an",
            "-vf",
            vf,
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "pipe:1",
        ]
    )
    return args


def _decode_visual_signal_frames(
    args: list[str],
    *,
    frame_size: int,
    interval: float,
) -> tuple[list[dict[str, float]], int, str]:
    samples: list[dict[str, float]] = []
    previous: bytes | None = None
    with tempfile.TemporaryFile() as error_log:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=error_log)
        try:
            assert process.stdout is not None
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
            if process.stdout is not None:
                process.stdout.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
        finally:
            if process.stdout is not None and not process.stdout.closed:
                process.stdout.close()
        return_code = process.wait()
        error_log.seek(0, os.SEEK_END)
        error_size = error_log.tell()
        error_log.seek(max(0, error_size - 8192))
        stderr = error_log.read().decode("utf-8", errors="replace")
    return samples, return_code, stderr


def extract_frame(clip: Clip, at: float, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}.partial{output.suffix}")
    temporary.unlink(missing_ok=True)
    frame_interval = 1.0 / max(1.0, float(clip.fps or 0.0))
    safe_at = min(
        max(0.0, at),
        max(0.0, clip.duration - max(0.1, frame_interval * 2.0)),
    )
    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{safe_at:.3f}",
            "-i",
            clip.path,
            "-frames:v",
            "1",
            "-vf",
            "scale=640:-2:force_original_aspect_ratio=decrease",
            "-pix_fmt",
            "yuvj420p",
            "-q:v",
            "4",
            "-y",
            str(temporary),
        ]
    )
    if not temporary.exists() or temporary.stat().st_size == 0:
        # Some iPhone MOV edit lists let a fast input seek return success while
        # producing no packet. Retry with an accurate output seek before
        # treating the representative frame as unavailable.
        temporary.unlink(missing_ok=True)
        run_command(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                clip.path,
                "-ss",
                f"{safe_at:.3f}",
                "-frames:v",
                "1",
                "-vf",
                "scale=640:-2:force_original_aspect_ratio=decrease",
                "-pix_fmt",
                "yuvj420p",
                "-q:v",
                "4",
                "-y",
                str(temporary),
            ]
        )
    if not temporary.exists() or temporary.stat().st_size == 0:
        temporary.unlink(missing_ok=True)
        raise VideoSummaryError(
            f"대표 프레임을 추출하지 못했습니다 ({Path(clip.path).name} @ {safe_at:.3f}s)"
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


def _metadata_tag(probe: dict[str, Any], *keys: str) -> str | None:
    containers: list[dict[str, Any]] = []
    format_data = probe.get("format")
    if isinstance(format_data, dict):
        containers.append(format_data)
    containers.extend(stream for stream in probe.get("streams", []) if isinstance(stream, dict))
    lowered_containers: list[dict[str, Any]] = []
    for container in containers:
        tags = container.get("tags", {})
        if not isinstance(tags, dict):
            continue
        lowered_containers.append(
            {str(key).casefold(): value for key, value in tags.items()}
        )
    # Honor tag priority across all containers. A generic format creation_time
    # must never beat an original QuickTime creation date stored on a stream.
    for key in keys:
        for lowered in lowered_containers:
            if lowered.get(key):
                return str(lowered[key])
    return None


def _creation_time(probe: dict[str, Any]) -> str | None:
    # Photos exports can stamp the export time into generic creation_time while
    # preserving the original capture time in the QuickTime tag.
    return _metadata_tag(
        probe,
        "com.apple.quicktime.creationdate",
        "creation_time",
        "date",
    )


def _source_kind(
    relative_path: Path,
    camera_make: str | None,
    camera_model: str | None,
) -> str:
    name = relative_path.name.casefold()
    make = (camera_make or "").casefold()
    model = (camera_model or "").casefold()
    if name.startswith("_talkv_"):
        return "shared"
    if "iphone" in model or make == "apple":
        return "phone"
    if "dji" in make or "dji" in model or name.startswith("dji_"):
        return "action_camera"
    return "unknown"


def _source_stream_id(
    relative_path: Path,
    *,
    source_kind: str,
    camera_make: str | None,
    camera_model: str | None,
) -> str:
    if camera_model:
        identity = {
            "kind": source_kind,
            "make": (camera_make or "").strip().casefold(),
            "model": camera_model.strip().casefold(),
        }
    elif source_kind == "shared":
        identity = {"kind": "shared", "channel": "talkv"}
    else:
        parents = [part.casefold() for part in relative_path.parts[:-1]]
        identity = {
            "kind": source_kind,
            "folder": parents[-1] if parents else "root",
        }
    return "stream_" + stable_hash(identity, length=12)


def _capture_time_confidence(
    capture_source: str,
    override: dict[str, Any] | None,
    *,
    source_kind: str = "unknown",
    sequence_source: str | None = None,
) -> str:
    configured = str((override or {}).get("confidence", "")).strip().casefold()
    if configured in {"high", "medium", "low"}:
        return configured
    if source_kind == "shared":
        return "low"
    if sequence_source in {"shared_unplaced", "relative_clock_unplaced"}:
        return "low"
    if sequence_source and (
        sequence_source.startswith("manual_clock_offset")
        or sequence_source in {
            "dji_mimo_filename",
            "date_override:dji_mimo_filename",
        }
    ):
        return "medium"
    if source_kind == "action_camera" and capture_source == "date_override:metadata":
        return "medium"
    if capture_source in {"metadata", "date_override:metadata", "override"}:
        return "high"
    if capture_source in {"filename", "date_override:filename"}:
        return "medium"
    return "low"


def _capture_time_basis(
    capture_source: str,
    override: dict[str, Any] | None,
    *,
    source_kind: str,
    sequence_source: str,
) -> str:
    """Classify whether wall-clock time can order clips across cameras."""
    if sequence_source in {"shared_unplaced", "relative_clock_unplaced"}:
        return "unplaced"
    if source_kind == "shared" and capture_source != "override":
        return "unplaced"
    if sequence_source.startswith("manual_clock_offset") or sequence_source in {
        "dji_mimo_filename",
        "date_override:dji_mimo_filename",
    }:
        return "estimated"
    if source_kind == "action_camera" and capture_source.startswith("date_override:"):
        return "unplaced"
    if capture_source in {"mtime", "date_override:mtime"}:
        return "unplaced"
    if capture_source in {"filename", "date_override:filename"}:
        return "estimated"
    return "absolute"


def _parse_datetime(value: str, timezone: ZoneInfo) -> datetime:
    parsed = _parse_datetime_raw(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)


def _parse_datetime_raw(value: str) -> datetime:
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    # Apple QuickTime commonly writes local offsets as ``-1000`` instead of
    # the colonized ISO 8601 form ``-10:00``.  Python 3.11+ accepts both, but
    # normalize explicitly so capture ordering stays stable on every supported
    # runtime and in tools that reuse this parser.
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)
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


def _dji_mimo_capture_datetime(name: str, timezone: ZoneInfo) -> datetime | None:
    """Read the recording timestamp from DJI Mimo's second datetime token."""
    if not name.casefold().startswith("dji_mimo_"):
        return None
    match = _DJI_MIMO_TIMESTAMP_PATTERN.search(name)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone)
    except ValueError:
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


def _optional_probe_text(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _dolby_vision_profile(video: dict[str, Any]) -> int | None:
    for side_data in video.get("side_data_list", []):
        if not isinstance(side_data, dict):
            continue
        if str(side_data.get("side_data_type", "")).casefold() != "dovi configuration record":
            continue
        value = side_data.get("dv_profile")
        if isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


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
