from __future__ import annotations

import importlib.util
import os
import platform
import re
import shutil
from pathlib import Path
from typing import Any

from .candidates import build_candidates
from .media import load_clips, scan_project
from .planner import plan_project
from .project import ProjectPaths
from .renderer import render_project
from .state import StateStore
from .transcribe import resolve_backend, transcribe_project
from .utils import VideoSummaryError, command_exists, read_json, run_command


def analyze_project(
    paths: ProjectPaths,
    config: dict[str, Any],
    *,
    force: bool = False,
    skip_transcribe: bool = False,
) -> dict[str, Any]:
    clips = load_clips(paths, config)
    transcript = transcribe_project(paths, clips, config, force=force, skip=skip_transcribe)
    candidates = build_candidates(paths, clips, config, force=force)
    return {"transcription": transcript, "candidates": candidates}


def run_pipeline(
    paths: ProjectPaths,
    source_dir: str | Path,
    config: dict[str, Any],
    *,
    planner_name: str,
    prompt: str | None,
    planner_images: bool,
    plan_file: str | Path | None,
    strict_planner: bool,
    skip_transcribe: bool,
    draft: bool,
    force: bool,
) -> dict[str, Any]:
    scan = scan_project(paths, source_dir, config, force=force)
    analysis = analyze_project(paths, config, force=force, skip_transcribe=skip_transcribe)
    plan = plan_project(
        paths,
        config,
        planner_name=planner_name,
        prompt=prompt,
        planner_images=planner_images,
        plan_file=plan_file,
        strict_planner=strict_planner,
        force=force,
    )
    render = render_project(paths, config, draft=draft, force=force)
    return {"scan": scan, "analysis": analysis, "plan": plan, "render": render}


def project_status(paths: ProjectPaths) -> dict[str, Any]:
    if not paths.root.exists():
        raise VideoSummaryError(f"프로젝트가 없습니다: {paths.root}")
    artifacts = {
        "config": paths.config.exists(),
        "manifest": paths.manifest.exists(),
        "candidates": paths.candidates.exists(),
        "edit_plan": paths.plan.exists(),
        "render_report": (paths.root / "render-report.json").exists(),
    }
    outputs = []
    report_path = paths.root / "render-report.json"
    if report_path.exists():
        outputs = read_json(report_path).get("outputs", [])
    return {
        "project_root": str(paths.root),
        "exports_root": str(paths.exports),
        "artifacts": artifacts,
        "stages": StateStore(paths.state).rows(),
        "outputs": outputs,
    }


def doctor(
    *,
    whisper_cpp_model: str | None = None,
    whisper_cpp_vad_model: str | None = None,
) -> dict[str, Any]:
    commands = {name: shutil.which(name) for name in ("ffmpeg", "ffprobe", "uv", "whisper-cli", "codex", "claude")}
    ffmpeg_version = None
    encoders: list[str] = []
    filters: list[str] = []
    if commands["ffmpeg"]:
        ffmpeg_version = run_command(["ffmpeg", "-hide_banner", "-version"]).stdout.splitlines()[0]
        encoder_output = run_command(["ffmpeg", "-hide_banner", "-encoders"]).stdout
        encoders = [name for name in ("h264_videotoolbox", "libx264") if name in encoder_output]
        filter_output = run_command(["ffmpeg", "-hide_banner", "-filters"]).stdout
        filters = [name for name in ("scale", "pad", "overlay", "loudnorm", "sidechaincompress") if name in filter_output]
    whisper_model = (whisper_cpp_model or os.environ.get("WHISPER_CPP_MODEL", "")).strip()
    vad_model = (whisper_cpp_vad_model or os.environ.get("WHISPER_CPP_VAD_MODEL", "")).strip()
    whisper_help = ""
    whisper_help_error = None
    if commands["whisper-cli"]:
        try:
            completed = run_command(["whisper-cli", "--help"])
            whisper_help = f"{completed.stdout}\n{completed.stderr}"
        except VideoSummaryError as exc:
            whisper_help_error = str(exc)
    whisper_flags = (
        "--model",
        "--file",
        "--language",
        "--output-json",
        "--output-file",
        "--processors",
        "--threads",
        "--no-prints",
        "--vad",
        "--vad-model",
    )
    whisper_capabilities = {flag: _help_has_flag(whisper_help, flag) for flag in whisper_flags}
    model_path = Path(whisper_model).expanduser() if whisper_model else None
    vad_path = Path(vad_model).expanduser() if vad_model else None
    model_is_file = bool(model_path and model_path.is_file())
    vad_is_file = bool(vad_path and vad_path.is_file()) if vad_path else None
    whisper_ready = bool(
        commands["whisper-cli"]
        and model_is_file
        and all(
            whisper_capabilities[flag]
            for flag in (
                "--model",
                "--file",
                "--language",
                "--output-json",
                "--output-file",
                "--processors",
                "--threads",
                "--no-prints",
            )
        )
        and (
            vad_path is None
            or (vad_is_file and whisper_capabilities["--vad"] and whisper_capabilities["--vad-model"])
        )
    )
    faster_whisper = importlib.util.find_spec("faster_whisper") is not None
    font_candidates = [
        Path("/System/Library/Fonts/AppleSDGothicNeo.ttc"),
        Path("/Library/Fonts/Arial Unicode.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
    ]
    memory_bytes = None
    if platform.system() == "Darwin" and command_exists("sysctl"):
        try:
            memory_bytes = int(run_command(["sysctl", "-n", "hw.memsize"]).stdout.strip())
        except (ValueError, VideoSummaryError):
            pass
    asr_ready = bool(whisper_ready or faster_whisper)
    required_filters = {"scale", "pad", "overlay", "loudnorm"}
    required_ready = bool(
        commands["ffmpeg"]
        and commands["ffprobe"]
        and encoders
        and required_filters.issubset(filters)
        and asr_ready
    )
    return {
        "ready": required_ready,
        "platform": {"system": platform.system(), "machine": platform.machine(), "memory_bytes": memory_bytes},
        "commands": commands,
        "ffmpeg": {
            "version": ffmpeg_version,
            "encoders": encoders,
            "filters": filters,
            "required_filters_present": required_filters.issubset(filters),
        },
        "asr": {
            "ready": asr_ready,
            "recommended": "whisper.cpp + large-v3-turbo-q5_0 + Silero VAD",
            "whisper_cpp_model": whisper_model or None,
            "whisper_cpp_model_is_file": model_is_file,
            "whisper_cpp_vad_model": vad_model or None,
            "whisper_cpp_vad_model_is_file": vad_is_file,
            "whisper_cpp_capabilities": whisper_capabilities,
            "whisper_cpp_help_error": whisper_help_error,
            "whisper_cpp_ready": whisper_ready,
            "faster_whisper_installed": faster_whisper,
            "selected_default": "whisper.cpp" if whisper_ready else "faster-whisper" if faster_whisper else None,
        },
        "font": next((str(path) for path in font_candidates if path.exists()), None),
        "notes": [
            "기본 렌더는 메모리 절약을 위해 1080p/30fps, 세그먼트 순차 처리입니다.",
            "whisper.cpp 모델을 지정하지 않으면 faster-whisper small/int8 CPU를 사용합니다.",
            "Codex/Claude 플래너는 선택 사항이며 local 플래너만으로도 완주할 수 있습니다.",
        ],
    }


def _help_has_flag(help_text: str, flag: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", help_text) is not None
