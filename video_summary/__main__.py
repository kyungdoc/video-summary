from __future__ import annotations

import argparse
import json

from .pipeline import plan_project, render_project, run_project_pipeline, scan_project


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prompt-first travel video summary pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("scan", "plan", "render", "run"):
        command = subparsers.add_parser(name, help=f"{name.title()} the trip project")
        command.add_argument("--project", required=True, help="Project name")
        if name in {"scan", "plan", "render", "run"}:
            command.add_argument("--source-dir", help="Folder containing original clips")
        if name in {"scan", "plan", "run"}:
            command.add_argument("--prompt", help="Free-form editing prompt for the project")
            command.add_argument("--prompt-file", help="Path to a text or markdown file containing the editing prompt")
            command.add_argument(
                "--timezone",
                help="IANA timezone for travel-day grouping, for example Asia/Ho_Chi_Minh",
            )
            command.add_argument(
                "--day-start-hour",
                type=int,
                help="Treat clips before this local hour as part of the previous travel day",
            )
            command.add_argument(
                "--speech-locale",
                help="Speech locale for local ASR transcription, for example ko-KR",
            )
        if name in {"render", "run"}:
            command.add_argument(
                "--draft",
                action="store_true",
                help="Render quick preview MP4s instead of full 4K masters",
            )

    return parser


def main() -> None:
    args = build_parser().parse_args()

    project = args.project
    if args.command == "scan":
        print(
            json.dumps(
                scan_project(
                    project,
                    source_dir=args.source_dir,
                    prompt_text=args.prompt,
                    prompt_path=args.prompt_file,
                    timezone_name=args.timezone,
                    day_start_hour=args.day_start_hour,
                    speech_locale=args.speech_locale,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "plan":
        print(
            json.dumps(
                plan_project(
                    project,
                    source_dir=args.source_dir,
                    prompt_text=args.prompt,
                    prompt_path=args.prompt_file,
                    timezone_name=args.timezone,
                    day_start_hour=args.day_start_hour,
                    speech_locale=args.speech_locale,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "render":
        print(
            json.dumps(
                render_project(project, draft=args.draft, source_dir=args.source_dir),
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "run":
        print(
            json.dumps(
                run_project_pipeline(
                    project,
                    source_dir=args.source_dir,
                    prompt_text=args.prompt,
                    prompt_path=args.prompt_file,
                    timezone_name=args.timezone,
                    day_start_hour=args.day_start_hour,
                    speech_locale=args.speech_locale,
                    draft=args.draft,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
