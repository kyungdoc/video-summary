from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List
from zoneinfo import ZoneInfo

from .brief import (
    PROMPT_FILENAME,
    brief_default_timezone,
    brief_final_feedback,
    brief_final_render_mode,
    brief_preview_preset_ids,
    brief_selected_preset,
    derive_prompt_date_span,
    derive_video_title,
    ensure_prompt_backed_brief,
    ensure_project_metadata,
    load_project_prompt,
    load_project_metadata,
)
from .captions import (
    DEFAULT_CAPTION_MODEL,
    DEFAULT_TRANSCRIPTION_BASE_URL,
    DEFAULT_TRANSCRIPTION_PROVIDER,
    transcribe_project_clips,
)
from .candidates import build_segment_candidates, validate_segment_selection
from .look_presets import collect_look_presets
from .media import dominant_fps, scan_media_directory
from .models import ClipInfo, TimelinePlan
from .planner import build_timeline_from_selection
from .renderer import (
    grade_delivery_variant,
    grade_master_variant,
    grade_preview_variant,
    render_delivery_base,
    render_delivery_chunked,
    render_draft,
    render_master,
    render_youtube,
    write_chapters,
)
from .taxonomy import ensure_project_taxonomy


WORK_ROOT = Path("work")
INTERNAL_WORK_ROOT_NAME = ".video-summary"
EXPORT_ROOT_NAME = "exports"
DEFAULT_DAY_START_HOUR = 4
DEFAULT_SPEECH_LOCALE = "ko-KR"


def slugify(value: str) -> str:
    return "".join(character.lower() if character.isalnum() else "-" for character in value).strip("-")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _delete_intermediate_files(paths: List[Path]) -> List[str]:
    deleted: List[str] = []
    for path in paths:
        try:
            if path.exists() and path.is_file():
                path.unlink()
                deleted.append(str(path.resolve()))
        except OSError:
            continue
    return deleted


def _legacy_project_paths(album_name: str) -> Dict[str, Path]:
    slug = slugify(album_name)
    return {
        "root": WORK_ROOT,
        "raw": WORK_ROOT / "raw" / slug,
        "build": WORK_ROOT / "build" / slug,
        "assets": WORK_ROOT / "assets" / slug,
        "output": WORK_ROOT / "output" / slug,
    }


def _default_project_dir() -> Path:
    configured = Path.cwd()
    env_value = __import__("os").environ.get("VIDEO_SUMMARY_PROJECT_DIR")
    if env_value:
        configured = Path(env_value).expanduser()
    return configured.resolve()


def _project_paths(
    album_name: str,
    source_dir: str | Path | None = None,
    project_dir: str | Path | None = None,
    settings: Dict[str, object] | None = None,
) -> Dict[str, Path]:
    slug = slugify(album_name)
    resolved = settings or {}
    explicit_paths = {
        "project": resolved.get("project_dir"),
        "root": resolved.get("root_dir"),
        "raw": resolved.get("source_dir"),
        "build": resolved.get("build_dir"),
        "assets": resolved.get("assets_dir"),
        "output": resolved.get("output_dir"),
    }
    if all(explicit_paths.values()):
        return {key: Path(str(value)).expanduser().resolve() for key, value in explicit_paths.items()}

    if source_dir is not None:
        resolved_source = Path(source_dir).expanduser().resolve()
        resolved_project = Path(project_dir).expanduser().resolve() if project_dir is not None else _default_project_dir()
        work_root = resolved_project / INTERNAL_WORK_ROOT_NAME
        return {
            "project": resolved_project,
            "root": work_root,
            "raw": resolved_source,
            "build": work_root / "build" / slug,
            "assets": work_root / "assets" / slug,
            "output": resolved_project / EXPORT_ROOT_NAME / slug,
        }

    if resolved.get("source_dir"):
        return _project_paths(
            album_name,
            source_dir=str(resolved["source_dir"]),
            project_dir=str(resolved["project_dir"]) if resolved.get("project_dir") else None,
        )

    legacy = {key: path.resolve() for key, path in _legacy_project_paths(album_name).items()}
    legacy["project"] = legacy["raw"]
    return legacy


def _project_manifest_path(build_dir: Path) -> Path:
    return build_dir / "project_manifest.json"


def _load_project_manifest(build_dir: Path) -> Dict[str, object]:
    manifest_path = _project_manifest_path(build_dir)
    if not manifest_path.exists():
        return {}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _default_timezone_name() -> str:
    tzinfo = datetime.now().astimezone().tzinfo
    return getattr(tzinfo, "key", None) or "UTC"


def configure_project(
    project_name: str,
    source_dir: str | Path | None = None,
    project_dir: str | Path | None = None,
    timezone_name: str | None = None,
    day_start_hour: int | None = None,
    speech_locale: str | None = None,
    transcription_provider: str | None = None,
    transcription_model: str | None = None,
    transcription_base_url: str | None = None,
    metadata_path: str | Path | None = None,
    prompt_text: str | None = None,
    prompt_path: str | Path | None = None,
) -> Dict[str, object]:
    legacy_paths = _legacy_project_paths(project_name)
    manifest = _load_project_manifest(legacy_paths["build"])

    resolved_source: Path | None = None
    if source_dir is not None:
        resolved_source = Path(source_dir).expanduser().resolve()
        if not resolved_source.exists():
            raise FileNotFoundError(f"Source directory does not exist: {resolved_source}")
        manifest["source_dir"] = str(resolved_source)
    elif "source_dir" in manifest:
        resolved_source = Path(str(manifest["source_dir"])).expanduser().resolve()
    else:
        resolved_source = legacy_paths["raw"].resolve()
        manifest["source_dir"] = str(resolved_source)

    resolved_project_dir: Path | None = None
    if project_dir is not None:
        resolved_project_dir = Path(project_dir).expanduser().resolve()
        if not resolved_project_dir.exists():
            raise FileNotFoundError(f"Project directory does not exist: {resolved_project_dir}")
        if not resolved_project_dir.is_dir():
            raise NotADirectoryError(f"Project directory is not a directory: {resolved_project_dir}")
        manifest["project_dir"] = str(resolved_project_dir)
    elif "project_dir" in manifest:
        resolved_project_dir = Path(str(manifest["project_dir"])).expanduser().resolve()
    else:
        resolved_project_dir = _default_project_dir()
        manifest["project_dir"] = str(resolved_project_dir)

    paths = _project_paths(project_name, source_dir=resolved_source, project_dir=resolved_project_dir, settings=manifest)
    manifest["project_dir"] = str(paths["project"].resolve())
    manifest["root_dir"] = str(paths["root"].resolve())
    manifest["build_dir"] = str(paths["build"].resolve())
    manifest["assets_dir"] = str(paths["assets"].resolve())
    manifest["output_dir"] = str(paths["output"].resolve())

    resolved_prompt_text = ""
    if prompt_path is not None:
        resolved_prompt_file = Path(prompt_path).expanduser().resolve()
        if not resolved_prompt_file.exists():
            raise FileNotFoundError(f"Project prompt file does not exist: {resolved_prompt_file}")
        resolved_prompt_text = resolved_prompt_file.read_text(encoding="utf-8").strip()
        manifest["prompt_path"] = str(resolved_prompt_file)
    elif prompt_text is not None:
        resolved_prompt_text = str(prompt_text).strip()
        manifest["prompt_path"] = str((paths["build"] / PROMPT_FILENAME).resolve())
    else:
        existing_prompt = manifest.get("prompt_path")
        if existing_prompt:
            prompt_file = Path(str(existing_prompt)).expanduser().resolve()
            if prompt_file.exists():
                resolved_prompt_text = prompt_file.read_text(encoding="utf-8").strip()
                manifest["prompt_path"] = str(prompt_file)

    resolved_brief_path: Path | None = None
    if resolved_prompt_text:
        resolved_brief_path = ensure_prompt_backed_brief(
            paths["build"],
            project_name,
            resolved_prompt_text,
            timezone_name=timezone_name or _default_timezone_name(),
            base_metadata_path=metadata_path,
        ).resolve()
        manifest["metadata_path"] = str(resolved_brief_path)
    elif metadata_path is not None:
        resolved_brief_path = Path(metadata_path).expanduser().resolve()
        if not resolved_brief_path.exists():
            raise FileNotFoundError(f"Project metadata does not exist: {resolved_brief_path}")
        manifest["metadata_path"] = str(resolved_brief_path)
    else:
        existing_brief = manifest.get("metadata_path") or manifest.get("brief_path")
        if existing_brief:
            resolved_brief_path = Path(str(existing_brief)).expanduser().resolve()
            manifest["metadata_path"] = str(resolved_brief_path)
            manifest.pop("brief_path", None)
        else:
            resolved_brief_path = ensure_project_metadata(
                paths["build"],
                project_name,
                timezone_name=timezone_name or _default_timezone_name(),
            ).resolve()
            manifest["metadata_path"] = str(resolved_brief_path)

    brief_default_tz = None
    if resolved_brief_path is not None and resolved_brief_path.exists():
        try:
            brief_default_tz = brief_default_timezone(
                load_project_metadata(paths["build"], project_name, metadata_path=resolved_brief_path)
            )
        except Exception:
            brief_default_tz = None

    if timezone_name is not None:
        ZoneInfo(timezone_name)
        manifest["timezone"] = timezone_name
    elif "timezone" not in manifest:
        manifest["timezone"] = brief_default_tz or _default_timezone_name()

    if day_start_hour is not None:
        manifest["day_start_hour"] = int(day_start_hour)
    elif "day_start_hour" not in manifest:
        manifest["day_start_hour"] = DEFAULT_DAY_START_HOUR

    if speech_locale is not None:
        manifest["speech_locale"] = speech_locale
    elif "speech_locale" not in manifest:
        manifest["speech_locale"] = DEFAULT_SPEECH_LOCALE

    if transcription_provider is not None:
        manifest["transcription_provider"] = transcription_provider
    elif "transcription_provider" not in manifest:
        manifest["transcription_provider"] = os.environ.get(
            "VIDEO_SUMMARY_TRANSCRIPTION_PROVIDER",
            DEFAULT_TRANSCRIPTION_PROVIDER,
        )

    if transcription_model is not None:
        manifest["transcription_model"] = transcription_model
    elif "transcription_model" not in manifest:
        manifest["transcription_model"] = os.environ.get(
            "VIDEO_SUMMARY_TRANSCRIPTION_MODEL",
            DEFAULT_CAPTION_MODEL,
        )

    if transcription_base_url is not None:
        manifest["transcription_base_url"] = transcription_base_url
    elif "transcription_base_url" not in manifest:
        manifest["transcription_base_url"] = os.environ.get(
            "VIDEO_SUMMARY_TRANSCRIPTION_BASE_URL",
            DEFAULT_TRANSCRIPTION_BASE_URL,
        )

    manifest["project"] = project_name
    manifest["slug"] = slugify(project_name)
    manifest["taxonomy_path"] = str(ensure_project_taxonomy(paths["build"], project_name))
    _write_json(_project_manifest_path(paths["build"]), manifest)
    if paths["build"] != legacy_paths["build"].resolve():
        _write_json(_project_manifest_path(legacy_paths["build"]), manifest)
    return manifest


def _project_source_dir(project_name: str, settings: Dict[str, object] | None = None) -> Path:
    resolved = settings or _load_project_manifest(_legacy_project_paths(project_name)["build"])
    paths = _project_paths(project_name, settings=resolved)
    return Path(str(resolved.get("source_dir") or paths["raw"])).expanduser().resolve()


def _project_metadata_path(project_name: str, settings: Dict[str, object] | None = None) -> Path:
    resolved = settings or _load_project_manifest(_legacy_project_paths(project_name)["build"])
    brief_value = resolved.get("metadata_path") or resolved.get("brief_path")
    if brief_value:
        return Path(str(brief_value)).expanduser().resolve()
    paths = _project_paths(project_name, settings=resolved)
    return ensure_project_metadata(
        paths["build"],
        project_name,
        timezone_name=str(resolved.get("timezone") or _default_timezone_name()),
    ).resolve()


def _require_project_prompt(project_name: str) -> str:
    settings = _load_project_manifest(_legacy_project_paths(project_name)["build"])
    build_dir = _project_paths(project_name, settings=settings)["build"]
    prompt_text = load_project_prompt(build_dir)
    if not prompt_text.strip():
        raise ValueError("A prompt is required. Run `plan` or `run` with `--prompt` or `--prompt-file` first.")
    return prompt_text


def _write_preset_docs(build_dir: Path, brief: Dict[str, object]) -> Dict[str, str]:
    preset_ids = brief_preview_preset_ids(brief)
    presets = collect_look_presets(preset_ids)
    selected = brief_selected_preset(brief)
    feedback = brief_final_feedback(brief)

    payload = {
        "preview_presets": presets,
        "selected_preset": selected,
        "final_feedback": feedback,
    }
    json_path = build_dir / "look_presets.json"
    md_path = build_dir / "look_presets.md"
    _write_json(json_path, payload)

    lines = ["# Look Presets", ""]
    if selected:
        lines.append(f"Selected preset: {selected}")
        lines.append("")
    if feedback:
        lines.append("Final feedback:")
        for item in feedback:
            lines.append(f"- {item}")
        lines.append("")
    for preset in presets:
        lines.append(f"## {preset['id']} · {preset['label']}")
        lines.append("")
        lines.append("Review points:")
        for point in preset.get("review_points", []):
            lines.append(f"- {point}")
        lines.append("")
        lines.append("Preview prompt:")
        lines.append("")
        lines.append("```text")
        lines.append(str(preset["preview_prompt"]).rstrip())
        lines.append("```")
        lines.append("")
        lines.append("Final prompt:")
        lines.append("")
        lines.append("```text")
        lines.append(str(preset["final_prompt"]).rstrip())
        lines.append("```")
        lines.append("")
    md_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return {
        "look_presets_json": str(json_path.resolve()),
        "look_presets_markdown": str(md_path.resolve()),
    }


def scan_project(
    project_name: str,
    source_dir: str | Path | None = None,
    project_dir: str | Path | None = None,
    metadata_path: str | Path | None = None,
    prompt_text: str | None = None,
    prompt_path: str | Path | None = None,
    timezone_name: str | None = None,
    day_start_hour: int | None = None,
    speech_locale: str | None = None,
    transcription_provider: str | None = None,
    transcription_model: str | None = None,
    transcription_base_url: str | None = None,
) -> Dict[str, object]:
    settings = configure_project(
        project_name,
        source_dir=source_dir,
        project_dir=project_dir,
        metadata_path=metadata_path,
        prompt_text=prompt_text,
        prompt_path=prompt_path,
        timezone_name=timezone_name,
        day_start_hour=day_start_hour,
        speech_locale=speech_locale,
        transcription_provider=transcription_provider,
        transcription_model=transcription_model,
        transcription_base_url=transcription_base_url,
    )
    paths = _project_paths(project_name, settings=settings)
    raw_dir = _project_source_dir(project_name, settings)
    brief = load_project_metadata(paths["build"], project_name, metadata_path=_project_metadata_path(project_name, settings))
    clips = scan_media_directory(
        raw_dir,
        timezone_name=str(settings["timezone"]),
        day_start_hour=int(settings["day_start_hour"]),
        brief=brief,
    )
    manifest = {
        "project": project_name,
        "source_dir": str(raw_dir),
        "timezone": settings["timezone"],
        "day_start_hour": settings["day_start_hour"],
        "speech_locale": settings["speech_locale"],
        "transcription_provider": settings["transcription_provider"],
        "transcription_model": settings["transcription_model"],
        "transcription_base_url": settings["transcription_base_url"],
        "clip_count": len(clips),
        "clips": [clip.to_dict() for clip in clips],
    }
    _write_json(paths["build"] / "clip_manifest.json", manifest)
    return manifest


def _load_project_clips(project_name: str, settings: Dict[str, object] | None = None) -> List[ClipInfo]:
    resolved = settings or _load_project_manifest(_legacy_project_paths(project_name)["build"]) or configure_project(project_name)
    paths = _project_paths(project_name, settings=resolved)
    manifest_path = paths["build"] / "clip_manifest.json"
    if not manifest_path.exists():
        scan_project(project_name, source_dir=str(resolved.get("source_dir")) if resolved.get("source_dir") else None)
    brief = load_project_metadata(paths["build"], project_name, metadata_path=_project_metadata_path(project_name, resolved))
    return scan_media_directory(
        _project_source_dir(project_name, resolved),
        timezone_name=str(resolved["timezone"]),
        day_start_hour=int(resolved["day_start_hour"]),
        brief=brief,
    )


def plan_project(
    project_name: str,
    source_dir: str | Path | None = None,
    project_dir: str | Path | None = None,
    metadata_path: str | Path | None = None,
    prompt_text: str | None = None,
    prompt_path: str | Path | None = None,
    timezone_name: str | None = None,
    day_start_hour: int | None = None,
    speech_locale: str | None = None,
    transcription_provider: str | None = None,
    transcription_model: str | None = None,
    transcription_base_url: str | None = None,
) -> Dict[str, object]:
    settings = configure_project(
        project_name,
        source_dir=source_dir,
        project_dir=project_dir,
        metadata_path=metadata_path,
        prompt_text=prompt_text,
        prompt_path=prompt_path,
        timezone_name=timezone_name,
        day_start_hour=day_start_hour,
        speech_locale=speech_locale,
        transcription_provider=transcription_provider,
        transcription_model=transcription_model,
        transcription_base_url=transcription_base_url,
    )
    paths = _project_paths(project_name, settings=settings)
    prompt_text_value = _require_project_prompt(project_name)
    brief = load_project_metadata(paths["build"], project_name, metadata_path=_project_metadata_path(project_name, settings))
    clips = _load_project_clips(project_name, settings)
    fps = dominant_fps(clips)
    trip_title = derive_video_title(brief, prompt_text=prompt_text_value)
    intro_date_span = derive_prompt_date_span(prompt_text_value, [clip.start_time.isoformat() for clip in clips])
    caption_summary = transcribe_project_clips(
        paths["build"],
        project_name,
        [clip.path for clip in clips],
        metadata_path=_project_metadata_path(project_name, settings),
        speech_locale=str(settings["speech_locale"]),
        provider=str(settings["transcription_provider"]),
        model_size=str(settings["transcription_model"]),
        base_url=str(settings.get("transcription_base_url") or ""),
        api_key=os.environ.get("VIDEO_SUMMARY_TRANSCRIPTION_API_KEY"),
    )
    candidates = build_segment_candidates(paths["build"], trip_title, clips, brief)
    selections = validate_segment_selection(paths["build"])
    clip_lookup = {clip.path: clip for clip in clips}
    final_cut = build_timeline_from_selection(selections, clip_lookup, trip_title, fps, intro_date_span=intro_date_span)

    _write_json(paths["build"] / "timeline_final.json", final_cut.to_dict())
    write_chapters(final_cut, paths["build"] / "trip_final_chapters.txt")
    look_docs = _write_preset_docs(paths["build"], brief)
    return {
        "project": settings,
        "captions": caption_summary,
        "candidates": candidates,
        "look_presets": look_docs,
        "final_cut": final_cut.to_dict(),
    }


def _render_project_outputs(project_name: str, draft: bool = False) -> Dict[str, str]:
    settings = _load_project_manifest(_legacy_project_paths(project_name)["build"]) or configure_project(project_name)
    paths = _project_paths(project_name, settings=settings)
    build_dir = paths["build"]
    output_dir = paths["output"]
    output_dir.mkdir(parents=True, exist_ok=True)
    brief = load_project_metadata(build_dir, project_name, metadata_path=_project_metadata_path(project_name, settings))
    look_docs = _write_preset_docs(build_dir, brief)
    validate_segment_selection(build_dir)

    final_path = build_dir / "timeline_final.json"
    if not final_path.exists():
        raise ValueError("Render requires a planned final timeline. Run `plan` or `run` first.")
    final_data = json.loads(final_path.read_text(encoding="utf-8"))

    final_cut = TimelinePlan(**{
        "title": final_data["title"],
        "fps": final_data["fps"],
        "target_duration": final_data["target_duration"],
        "items": final_data["items"],
        "days": [],
        "chapters": final_data["chapters"],
        "notes": final_data["notes"],
    })

    if draft:
        preview_base_dir = build_dir / "preview_base"
        preview_base_dir.mkdir(parents=True, exist_ok=True)
        preview_plan = final_cut
        preview_base = preview_base_dir / "trip_preview_reel_base_720p.mp4"
        render_draft(preview_plan, paths["assets"] / "draft" / "preview_reel", build_dir, preview_base)

        preset_outputs = {}
        for preset in collect_look_presets(brief_preview_preset_ids(brief)):
            output_path = output_dir / f"trip_preview_{preset['id']}_720p.mp4"
            grade_preview_variant(preview_base, output_path, str(preset["filter"]))
            preset_outputs[preset["id"]] = str(output_path.resolve())
        outputs = {
            "profile": "draft",
            "selected_preset": brief_selected_preset(brief),
            "preview_base": str(preview_base.resolve()),
            "preview_variants": preset_outputs,
            **look_docs,
        }
    else:
        selected_preset_id = brief_selected_preset(brief)
        selected_preset = collect_look_presets([selected_preset_id])[0] if selected_preset_id else None
        final_mode = brief_final_render_mode(brief)
        if final_mode == "direct_mp4":
            if selected_preset is None:
                raise ValueError("A selected_preset is required for direct_mp4 final rendering.")
            final_base = output_dir / "trip_final_base_4k.mp4"
            final_output = output_dir / f"trip_final_{selected_preset_id}_4k.mp4"
            render_delivery_base(final_cut, paths["assets"] / "final", build_dir, final_base)
            grade_delivery_variant(final_base, final_output, str(selected_preset["filter"]))
            deleted_intermediates = _delete_intermediate_files([final_base])
            outputs = {
                "profile": "final_delivery",
                "final_render_mode": final_mode,
                "selected_preset": selected_preset_id,
                "final_output": str(final_output.resolve()),
                "deleted_intermediates": deleted_intermediates,
                **look_docs,
            }
        elif final_mode == "chunked_direct_mp4":
            if selected_preset is None:
                raise ValueError("A selected_preset is required for chunked_direct_mp4 final rendering.")
            final_base = output_dir / "trip_final_base_4k.mp4"
            final_output = output_dir / f"trip_final_{selected_preset_id}_4k.mp4"
            final_chunks = render_delivery_chunked(
                final_cut,
                paths["assets"] / "final",
                build_dir / "final_chunks",
                output_dir / "chunks" / "final",
                final_base,
                final_output,
                str(selected_preset["filter"]),
            )
            deleted_intermediates = _delete_intermediate_files(
                [
                    final_base,
                    *[Path(path) for path in final_chunks["base_chunks"]],
                ]
            )
            outputs = {
                "profile": "final_delivery_chunked",
                "final_render_mode": final_mode,
                "selected_preset": selected_preset_id,
                "final_output": str(final_output.resolve()),
                "final_chunks": final_chunks["final_chunks"],
                "deleted_intermediates": deleted_intermediates,
                **look_docs,
            }
        else:
            final_master_base = output_dir / "trip_final_master_4k_base.mov"
            final_master = output_dir / "trip_final_master_4k.mov"
            final_youtube = output_dir / "trip_final_youtube_4k.mp4"

            render_master(final_cut, paths["assets"] / "final", build_dir, final_master_base)
            if selected_preset:
                grade_master_variant(final_master_base, final_master, str(selected_preset["filter"]))
            else:
                final_master_base.replace(final_master)
            render_youtube(final_master, final_youtube)

            outputs = {
                "profile": "master",
                "final_render_mode": final_mode,
                "selected_preset": selected_preset_id,
                "final_master": str(final_master.resolve()),
                "final_youtube": str(final_youtube.resolve()),
                **look_docs,
            }
    _write_json(build_dir / "render_outputs.json", outputs)
    return outputs


def render_project(
    project_name: str,
    draft: bool = False,
    source_dir: str | Path | None = None,
    project_dir: str | Path | None = None,
    metadata_path: str | Path | None = None,
    prompt_text: str | None = None,
    prompt_path: str | Path | None = None,
    timezone_name: str | None = None,
    day_start_hour: int | None = None,
    speech_locale: str | None = None,
    transcription_provider: str | None = None,
    transcription_model: str | None = None,
    transcription_base_url: str | None = None,
) -> Dict[str, str]:
    configure_project(
        project_name,
        source_dir=source_dir,
        project_dir=project_dir,
        metadata_path=metadata_path,
        prompt_text=prompt_text,
        prompt_path=prompt_path,
        timezone_name=timezone_name,
        day_start_hour=day_start_hour,
        speech_locale=speech_locale,
        transcription_provider=transcription_provider,
        transcription_model=transcription_model,
        transcription_base_url=transcription_base_url,
    )
    return _render_project_outputs(project_name, draft=draft)


def run_project_pipeline(
    project_name: str,
    source_dir: str | Path | None = None,
    project_dir: str | Path | None = None,
    metadata_path: str | Path | None = None,
    prompt_text: str | None = None,
    prompt_path: str | Path | None = None,
    timezone_name: str | None = None,
    day_start_hour: int | None = None,
    speech_locale: str | None = None,
    transcription_provider: str | None = None,
    transcription_model: str | None = None,
    transcription_base_url: str | None = None,
    draft: bool = False,
) -> Dict[str, object]:
    settings = configure_project(
        project_name,
        source_dir=source_dir,
        project_dir=project_dir,
        metadata_path=metadata_path,
        prompt_text=prompt_text,
        prompt_path=prompt_path,
        timezone_name=timezone_name,
        day_start_hour=day_start_hour,
        speech_locale=speech_locale,
        transcription_provider=transcription_provider,
        transcription_model=transcription_model,
        transcription_base_url=transcription_base_url,
    )
    _require_project_prompt(project_name)
    clip_manifest = scan_project(
        project_name,
        source_dir=source_dir,
        project_dir=project_dir,
        metadata_path=metadata_path,
        prompt_text=prompt_text,
        prompt_path=prompt_path,
        timezone_name=timezone_name,
        day_start_hour=day_start_hour,
        speech_locale=speech_locale,
        transcription_provider=transcription_provider,
        transcription_model=transcription_model,
        transcription_base_url=transcription_base_url,
    )
    plans = plan_project(
        project_name,
        source_dir=source_dir,
        project_dir=project_dir,
        metadata_path=metadata_path,
        prompt_text=prompt_text,
        prompt_path=prompt_path,
        timezone_name=timezone_name,
        day_start_hour=day_start_hour,
        speech_locale=speech_locale,
        transcription_provider=transcription_provider,
        transcription_model=transcription_model,
        transcription_base_url=transcription_base_url,
    )
    outputs = render_project(
        project_name,
        draft=draft,
        source_dir=source_dir,
        project_dir=project_dir,
        metadata_path=metadata_path,
        prompt_text=prompt_text,
        prompt_path=prompt_path,
        timezone_name=timezone_name,
        day_start_hour=day_start_hour,
        speech_locale=speech_locale,
        transcription_provider=transcription_provider,
        transcription_model=transcription_model,
        transcription_base_url=transcription_base_url,
    )
    return {
        "project": settings,
        "clips": clip_manifest,
        "plans": plans,
        "outputs": outputs,
    }
