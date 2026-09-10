from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any

from .media import scan_project
from .pipeline import analyze_project, doctor, project_status, run_pipeline
from .planner import plan_project
from .project import ProjectPaths, ensure_config, project_paths, save_config
from .renderer import render_project
from .state import project_lock
from .utils import VideoSummaryError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="video-summary",
        description="날짜별 여행 영상 요약을 만드는 로컬 우선 CLI",
    )
    parser.add_argument("--debug", action="store_true", help="오류 발생 시 traceback 표시")
    parser.add_argument("--full-json", action="store_true", help="내부 단계 payload까지 stdout에 모두 출력")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor_parser = subparsers.add_parser("doctor", help="FFmpeg, ASR, 렌더 환경 점검")
    doctor_parser.add_argument("--json", action="store_true", help="JSON 출력(기본 출력 형식과 동일한 호환 옵션)")
    doctor_parser.add_argument("--whisper-cpp-model", help="점검할 whisper.cpp 모델 경로")
    doctor_parser.add_argument("--whisper-cpp-vad-model", help="점검할 Silero VAD 모델 경로")

    review_parser = subparsers.add_parser("review", help="이벤트·원본·편집 근거를 로컬 브라우저에서 검토")
    review_parser.add_argument("--project", required=True, help="프로젝트 이름")
    review_parser.add_argument("--workspace", "--project-dir", dest="workspace", help="기존 프로젝트 workspace")
    review_parser.add_argument("--port", type=_review_port, default=8765, help="127.0.0.1 포트 (0: 자동 할당)")
    review_parser.add_argument("--json", action="store_true", help="서버 없이 검토 카탈로그 JSON만 출력")

    for command in ("init", "scan", "analyze", "plan", "render", "run", "status"):
        item = subparsers.add_parser(command, help=_command_help(command))
        item.add_argument("--project", required=True, help="프로젝트 이름")
        item.add_argument(
            "--workspace",
            "--project-dir",
            dest="workspace",
            help="빌드와 exports가 저장될 루트; 기본값은 실행한 폴더",
        )
        if command in {"scan", "run"}:
            item.add_argument("--source-dir", required=True, help="원본 영상 폴더")
        elif command in {"analyze", "plan"}:
            item.add_argument("--source-dir", help="지정하면 scan부터 다시 확인할 원본 영상 폴더")
        if command in {"init", "scan", "analyze", "plan", "run"}:
            _add_project_overrides(item)
        if command == "render":
            item.add_argument("--destination", help="인트로에 표시할 여행지 이름; 생략하면 자동 추론")
            item.add_argument("--episode-mode", choices=["daily", "trip"])
            item.add_argument("--resolution", choices=["720p", "1080p", "2160p"])
            item.add_argument("--day-key", help="전체 plan 검증 후 이 날짜(YYYY-MM-DD)만 daily로 렌더")
            item.add_argument("--plan-file", help="기본 edit-plan 대신 검증해 사용할 variant JSON plan")
            item.add_argument("--output-tag", help="비교 출력 이름에 붙일 영문/숫자 태그")
        if command in {"analyze", "plan", "run"}:
            item.add_argument("--skip-transcribe", action="store_true", help="음성 전사를 건너뛰고 영상 신호만 사용")
        if command in {"plan", "run"}:
            _add_planner_options(item)
        if command in {"render", "run"}:
            item.add_argument("--draft", action="store_true", help="720p 빠른 미리보기 렌더")
        if command not in {"init", "status"}:
            item.add_argument("--force", action="store_true", help="해당 단계 캐시를 무시하고 다시 실행")
    return parser


def _review_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("포트는 0~65535 사이 정수여야 합니다.") from exc
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("포트는 0~65535 사이 정수여야 합니다.")
    return port


def _command_help(command: str) -> str:
    return {
        "init": "프로젝트 설정 생성",
        "scan": "원본 클립과 촬영일 스캔",
        "analyze": "전사, 프레임 신호, 후보 큐 생성",
        "plan": "날짜별 편집계획 생성",
        "render": "검증된 편집계획 렌더",
        "run": "scan부터 render까지 한 번에 실행",
        "status": "캐시와 산출물 상태 확인",
    }[command]


def _add_project_overrides(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--destination", help="인트로에 표시할 여행지 이름; 생략하면 자동 추론")
    parser.add_argument("--prompt", help="자연어 편집 프롬프트")
    parser.add_argument("--prompt-file", help="프롬프트가 담긴 UTF-8 파일")
    parser.add_argument("--timezone", help="예: Asia/Seoul")
    parser.add_argument("--day-start-hour", type=int, help="이 시각 전 클립은 전날 여행일로 분류")
    parser.add_argument("--language", help="전사 언어 예: ko, en, auto")
    parser.add_argument("--target-minutes", type=float, help="날짜별 목표 길이(분)")
    parser.add_argument(
        "--pacing-profile",
        choices=["gentle", "balanced"],
        help="사건은 유지하면서 사건 내부 source 길이를 조절하는 편집 호흡",
    )
    parser.add_argument(
        "--tone-profile",
        choices=["calm", "playful"],
        help="같은 사건 안에서 차분한 맥락 또는 행동·결과·리액션을 우선하는 편집 톤",
    )
    parser.add_argument("--asr-backend", choices=["auto", "whisper.cpp", "faster-whisper"])
    parser.add_argument("--asr-model", help="faster-whisper 모델명 또는 로컬 경로")
    parser.add_argument("--whisper-cpp-model", help="whisper.cpp GGML/GGUF 모델 경로")
    parser.add_argument("--whisper-cpp-vad-model", help="whisper.cpp Silero VAD 모델 경로")
    parser.add_argument("--episode-mode", choices=["daily", "trip"])
    parser.add_argument("--resolution", choices=["720p", "1080p", "2160p"])


def _add_planner_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--planner", choices=["local", "codex", "claude", "file"], default="local")
    image_group = parser.add_mutually_exclusive_group()
    image_group.add_argument(
        "--planner-images",
        dest="planner_images",
        action="store_true",
        default=False,
        help="외부 플래너에 축소 contact sheet 제공",
    )
    image_group.add_argument(
        "--no-planner-images",
        dest="planner_images",
        action="store_false",
        help="외부 플래너에 이미지를 제공하지 않음(기본값)",
    )
    parser.add_argument("--plan-file", help="--planner file에서 가져올 JSON 편집계획")
    parser.add_argument("--strict-planner", action="store_true", help="외부 플래너 실패 시 로컬 fallback 금지")


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = execute(args)
        output = result if args.full_json else summarize_cli_result(args.command, result)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        if args.command == "doctor" and not result.get("ready", False):
            raise SystemExit(1)
    except (VideoSummaryError, FileNotFoundError, ValueError) as exc:
        print(f"video-summary: {exc}", file=sys.stderr)
        if args.debug:
            traceback.print_exc()
        raise SystemExit(2) from exc


def execute(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "doctor":
        return doctor(
            whisper_cpp_model=args.whisper_cpp_model,
            whisper_cpp_vad_model=args.whisper_cpp_vad_model,
        )
    workspace = Path(
        args.workspace
        or os.environ.get("VIDEO_SUMMARY_PROJECT_DIR")
        or Path.cwd()
    ).expanduser().resolve()
    paths = project_paths(workspace, args.project)
    if args.command == "status":
        return project_status(paths)
    if args.command == "review":
        # Review reads existing artifacts, including stale plans. It must not
        # create/overwrite config or monopolize the pipeline lock while idle.
        from .review import build_review_catalog, serve_review

        if args.json:
            return build_review_catalog(paths)
        return serve_review(paths, port=args.port)

    with project_lock(paths.root / ".pipeline.lock"):
        return execute_project_command(args, paths)


def execute_project_command(args: argparse.Namespace, paths: ProjectPaths) -> dict[str, Any]:
    config = ensure_config(paths, args.project)
    config = apply_overrides(config, args)
    save_config(paths, config)
    if args.command == "init":
        return {"project_root": str(paths.root), "config": str(paths.config), "exports": str(paths.exports)}

    source_dir = getattr(args, "source_dir", None)
    if args.command == "scan":
        return scan_project(paths, require_source(source_dir, args.command), config, force=args.force)
    if args.command == "analyze":
        if source_dir:
            scan_project(paths, source_dir, config, force=args.force)
        return analyze_project(paths, config, force=args.force, skip_transcribe=args.skip_transcribe)
    if args.command == "plan":
        if args.skip_transcribe and not source_dir:
            raise VideoSummaryError("plan의 --skip-transcribe는 --source-dir과 함께 사용해야 합니다.")
        if source_dir:
            scan_project(paths, source_dir, config, force=args.force)
            analyze_project(paths, config, force=args.force, skip_transcribe=args.skip_transcribe)
        return plan_project(
            paths,
            config,
            planner_name=args.planner,
            prompt=config["editing"]["prompt"],
            planner_images=args.planner_images,
            plan_file=args.plan_file,
            strict_planner=args.strict_planner,
            force=args.force,
        )
    if args.command == "render":
        return render_project(
            paths,
            config,
            draft=args.draft,
            force=args.force,
            day_key=args.day_key,
            plan_file=args.plan_file,
            output_tag=args.output_tag,
        )
    if args.command == "run":
        return run_pipeline(
            paths,
            require_source(source_dir, args.command),
            config,
            planner_name=args.planner,
            prompt=config["editing"]["prompt"],
            planner_images=args.planner_images,
            plan_file=args.plan_file,
            strict_planner=args.strict_planner,
            skip_transcribe=args.skip_transcribe,
            draft=args.draft,
            force=args.force,
        )
    raise VideoSummaryError(f"지원하지 않는 명령입니다: {args.command}")


def require_source(value: str | None, command: str) -> str:
    if not value:
        raise VideoSummaryError(f"{command}에는 --source-dir이 필요합니다.")
    return str(Path(value).expanduser().resolve())


def apply_overrides(config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    prompt = getattr(args, "prompt", None)
    prompt_file = getattr(args, "prompt_file", None)
    if prompt and prompt_file:
        raise VideoSummaryError("--prompt와 --prompt-file은 함께 사용할 수 없습니다.")
    if prompt_file:
        prompt = Path(prompt_file).expanduser().read_text(encoding="utf-8").strip()
    if prompt is not None:
        config["editing"]["prompt"] = prompt.strip()
        if getattr(args, "target_minutes", None) is None:
            inferred_target = parse_target_minutes(prompt)
            if inferred_target is not None:
                config["editing"]["target_minutes_per_day"] = inferred_target
    mapping = {
        "destination": ("project", "destination"),
        "timezone": ("project", "timezone"),
        "day_start_hour": ("project", "day_start_hour"),
        "language": ("project", "language"),
        "target_minutes": ("editing", "target_minutes_per_day"),
        "pacing_profile": ("editing", "pacing_profile"),
        "tone_profile": ("editing", "tone_profile"),
        "episode_mode": ("editing", "episode_mode"),
        "asr_backend": ("analysis", "asr_backend"),
        "asr_model": ("analysis", "asr_model"),
        "whisper_cpp_model": ("analysis", "whisper_cpp_model"),
        "whisper_cpp_vad_model": ("analysis", "whisper_cpp_vad_model"),
        "resolution": ("render", "resolution"),
    }
    for name, (section, key) in mapping.items():
        value = getattr(args, name, None)
        if value is not None:
            config[section][key] = value
    return config


def parse_target_minutes(prompt: str) -> float | None:
    range_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:~|–|-)\s*(\d+(?:\.\d+)?)\s*(?:분|minutes?|mins?)", prompt, re.IGNORECASE)
    if range_match:
        return (float(range_match.group(1)) + float(range_match.group(2))) / 2.0
    hour_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:시간|hours?|hrs?)", prompt, re.IGNORECASE)
    if hour_match:
        return float(hour_match.group(1)) * 60.0
    minute_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:분|minutes?|mins?)", prompt, re.IGNORECASE)
    if minute_match:
        return float(minute_match.group(1))
    return None


def summarize_cli_result(command: str, result: dict[str, Any]) -> dict[str, Any]:
    if command == "scan":
        return {
            "project": result.get("project"),
            "source_dir": result.get("source_dir"),
            "clip_count": len(result.get("clips", [])),
            "day_count": len(result.get("days", [])),
            "days": result.get("days", []),
            "manifest_hash": result.get("manifest_hash"),
        }
    if command == "analyze":
        candidates = result.get("candidates", {})
        return {
            "transcription": result.get("transcription", {}),
            "candidates": {
                "count": candidates.get("count", 0),
                "days": candidates.get("days", []),
                "candidate_set_hash": candidates.get("candidate_set_hash"),
            },
        }
    if command == "plan":
        return {
            "project": result.get("project"),
            "planner": result.get("planner"),
            "candidate_set_hash": result.get("candidate_set_hash"),
            "episodes": [
                {
                    "day_key": episode.get("day_key"),
                    "travel_day": episode.get("travel_day"),
                    "title": episode.get("title"),
                    "segment_count": len(episode.get("segments", [])),
                    "target_duration": episode.get("target_duration"),
                }
                for episode in result.get("episodes", [])
            ],
        }
    if command == "run":
        return {
            "scan": summarize_cli_result("scan", result.get("scan", {})),
            "analysis": summarize_cli_result("analyze", result.get("analysis", {})),
            "plan": summarize_cli_result("plan", result.get("plan", {})),
            "render": result.get("render", {}),
        }
    return result
