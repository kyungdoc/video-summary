from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from PIL import Image, ImageDraw, ImageFont

from .models import TimelinePlan


FONT_PATH = Path("/System/Library/Fonts/Supplemental/AppleGothic.ttf")
MASTER_FRAME = (3840, 2160)
DRAFT_FRAME = (1280, 720)


def _shell_join(command: List[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def _run(command: List[str]) -> None:
    subprocess.run(command, check=True)


def _log_step(message: str) -> None:
    print(f"[render] {message}", flush=True)


def _slugify(value: str) -> str:
    return "".join(character.lower() if character.isalnum() else "-" for character in value).strip("-")


def _item_duration(item: Dict[str, object]) -> float:
    if "duration" in item:
        return float(item["duration"])
    start = float(item.get("start", 0.0))
    end = float(item.get("end", start))
    return max(0.0, end - start)


def _chunk_plan(plan: TimelinePlan, label: str, items: List[Dict[str, object]]) -> TimelinePlan:
    return TimelinePlan(
        title=f"{plan.title} · {label}",
        fps=plan.fps,
        target_duration=sum(_item_duration(item) for item in items),
        items=items,
        days=[],
        chapters=[{"timecode": "00:00", "label": label}],
        notes=[f"Chunk render for {label}"],
    )


def split_plan_into_chunks(plan: TimelinePlan) -> List[Dict[str, object]]:
    if not plan.items:
        return []

    items = list(plan.items)
    title_starts = [index for index, item in enumerate(items) if item.get("kind") == "title"]
    chunks: List[Dict[str, object]] = []

    if not title_starts:
        label = str(plan.title).strip() or "Timeline"
        return [{"label": label, "slug": _slugify(label) or "timeline", "plan": _chunk_plan(plan, label, items)}]

    first_title_index = title_starts[0]
    if first_title_index > 0:
        prelude_items = items[:first_title_index]
        first_label = str(prelude_items[0].get("label", "")).strip() if prelude_items else ""
        label = "Cold Open" if first_label.startswith("Cold Open") else "Prelude"
        chunks.append(
            {
                "label": label,
                "slug": _slugify(label) or "prelude",
                "plan": _chunk_plan(plan, label, prelude_items),
            }
        )

    for chunk_index, start_index in enumerate(title_starts):
        end_index = title_starts[chunk_index + 1] if chunk_index + 1 < len(title_starts) else len(items)
        chunk_items = items[start_index:end_index]
        label = str(items[start_index].get("label", "")).strip() or f"Chapter {chunk_index + 1}"
        chunks.append(
            {
                "label": label,
                "slug": _slugify(label) or f"chapter-{chunk_index + 1}",
                "plan": _chunk_plan(plan, label, chunk_items),
            }
        )
    return chunks


def concat_copy_videos(input_paths: List[Path], output_path: Path, build_dir: Path) -> None:
    if not input_paths:
        raise ValueError("At least one input path is required for concat.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    build_dir.mkdir(parents=True, exist_ok=True)

    if len(input_paths) == 1:
        shutil.copy2(input_paths[0], output_path)
        return

    concat_list = build_dir / f"{output_path.stem}_concat.txt"
    concat_list.write_text(
        "".join(f"file '{path.resolve().as_posix()}'\n" for path in input_paths),
        encoding="utf-8",
    )
    command = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_list),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    _run(command)


def _title_png(label: str, output_path: Path, frame_width: int = MASTER_FRAME[0], frame_height: int = MASTER_FRAME[1]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (frame_width, frame_height), color=(15, 13, 11))
    draw = ImageDraw.Draw(image)
    scale = frame_width / MASTER_FRAME[0]
    major_font = ImageFont.truetype(str(FONT_PATH), max(40, int(120 * scale)))
    minor_font = ImageFont.truetype(str(FONT_PATH), max(24, int(60 * scale)))

    if "·" in label:
        title, subtitle = [part.strip() for part in label.split("·", 1)]
    else:
        title, subtitle = label.strip(), ""

    title_box = draw.textbbox((0, 0), title, font=major_font)
    title_width = title_box[2] - title_box[0]
    title_height = title_box[3] - title_box[1]
    title_x = (frame_width - title_width) / 2
    title_y = frame_height * 0.42
    draw.text((title_x, title_y), title, fill=(246, 242, 236), font=major_font)

    if subtitle:
        subtitle_box = draw.textbbox((0, 0), subtitle, font=minor_font)
        subtitle_width = subtitle_box[2] - subtitle_box[0]
        subtitle_x = (frame_width - subtitle_width) / 2
        subtitle_y = title_y + title_height + max(20, int(48 * scale))
        draw.text((subtitle_x, subtitle_y), subtitle, fill=(214, 208, 198), font=minor_font)

    image.save(output_path)


def create_title_card(
    label: str,
    duration: float,
    fps: float,
    output_path: Path,
    frame_width: int = MASTER_FRAME[0],
    frame_height: int = MASTER_FRAME[1],
    preset: str = "medium",
    crf: int = 12,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    png_path = output_path.with_suffix(".png")
    if not png_path.exists():
        _title_png(label, png_path, frame_width=frame_width, frame_height=frame_height)
    command = [
        "ffmpeg",
        "-y",
        "-loop",
        "1",
        "-i",
        str(png_path),
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=48000:cl=stereo",
        "-t",
        str(duration),
        "-shortest",
        "-vf",
        "fps={fps},scale={frame_width}:{frame_height}:flags=lanczos,format=yuv420p".format(
            fps=fps,
            frame_width=frame_width,
            frame_height=frame_height,
        ),
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        str(output_path),
    ]
    _run(command)


def ensure_title_assets(
    plan: TimelinePlan,
    asset_dir: Path,
    frame_width: int = MASTER_FRAME[0],
    frame_height: int = MASTER_FRAME[1],
    preset: str = "medium",
    crf: int = 12,
) -> Dict[str, Path]:
    assets: Dict[str, Path] = {}
    variant = f"_{frame_width}x{frame_height}"
    for index, item in enumerate(plan.items):
        if item["kind"] != "title":
            continue
        asset_path = asset_dir / f"title_{index:03d}{variant}.mp4"
        if not asset_path.exists():
            create_title_card(
                str(item["label"]),
                float(item["duration"]),
                plan.fps,
                asset_path,
                frame_width=frame_width,
                frame_height=frame_height,
                preset=preset,
                crf=crf,
            )
        assets[str(index)] = asset_path
    return assets


def _input_map(plan: TimelinePlan, title_assets: Dict[str, Path]) -> Tuple[List[str], Dict[str, int]]:
    inputs: List[str] = []
    mapping: Dict[str, int] = {}
    for index, item in enumerate(plan.items):
        if item["kind"] == "title":
            path = str(title_assets[str(index)].resolve())
        else:
            path = str(Path(str(item["clip_path"])).resolve())
        if path not in mapping:
            mapping[path] = len(inputs)
            inputs.append(path)
    return inputs, mapping


def _video_fade(duration: float) -> float:
    return min(0.24, max(0.10, duration * 0.075))


def _video_chain(
    label_index: int,
    input_index: int,
    item: Dict[str, object],
    fps: float,
    frame_width: int = MASTER_FRAME[0],
    frame_height: int = MASTER_FRAME[1],
    pixel_format: str = "yuv422p10le",
    draft: bool = False,
) -> List[str]:
    duration = float(item["duration"])
    fade = _video_fade(duration)
    fade_out_start = max(0.0, duration - fade)
    if item["kind"] == "title":
        base_ref = f"v{label_index}base"
        filters = [
            f"[{input_index}:v]trim=start=0:end={duration:.3f},setpts=PTS-STARTPTS,scale={frame_width}:{frame_height}:flags=lanczos,"
            f"fade=t=in:st=0:d={fade:.3f},fade=t=out:st={fade_out_start:.3f}:d={fade:.3f},setsar=1,fps={fps},format={pixel_format}[{base_ref}]"
        ]
        filters.append(f"[{base_ref}]null[v{label_index}]")
        return filters

    start = float(item["start"])
    end = float(item["end"])
    if draft:
        base_ref = f"v{label_index}base"
        filters = [
            f"[{input_index}:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS,split=2[v{label_index}bg][v{label_index}fg]",
            f"[v{label_index}bg]scale={frame_width}:{frame_height}:force_original_aspect_ratio=increase:flags=bicubic,crop={frame_width}:{frame_height},boxblur=12:1,eq=brightness=-0.03:contrast=1.02:saturation=0.88[v{label_index}bgb]",
            f"[v{label_index}fg]scale={frame_width}:{frame_height}:force_original_aspect_ratio=decrease:flags=bicubic,eq=contrast=1.05:brightness=0.01:saturation=1.06:gamma=0.98,unsharp=3:3:0.35:3:3:0.0[v{label_index}fgb]",
            f"[v{label_index}bgb][v{label_index}fgb]overlay=(W-w)/2:(H-h)/2,fade=t=in:st=0:d={fade:.3f},fade=t=out:st={fade_out_start:.3f}:d={fade:.3f},setsar=1,fps={fps},format={pixel_format}[{base_ref}]",
        ]
        filters.append(f"[{base_ref}]null[v{label_index}]")
        return filters
    base_ref = f"v{label_index}base"
    filters = [
        f"[{input_index}:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS,split=2[v{label_index}bg][v{label_index}fg]",
        f"[v{label_index}bg]scale={frame_width}:{frame_height}:force_original_aspect_ratio=increase:flags=lanczos,crop={frame_width}:{frame_height},boxblur=18:2,eq=brightness=-0.02:saturation=0.90[v{label_index}bgb]",
        f"[v{label_index}fg]scale={frame_width}:{frame_height}:force_original_aspect_ratio=decrease:flags=lanczos,eq=contrast=1.06:brightness=0.01:saturation=1.08:gamma=0.98,unsharp=5:5:0.4:5:5:0.0[v{label_index}fgb]",
        f"[v{label_index}bgb][v{label_index}fgb]overlay=(W-w)/2:(H-h)/2,fade=t=in:st=0:d={fade:.3f},fade=t=out:st={fade_out_start:.3f}:d={fade:.3f},setsar=1,fps={fps},format={pixel_format}[{base_ref}]",
    ]
    filters.append(f"[{base_ref}]null[v{label_index}]")
    return filters


def _audio_chain(label_index: int, input_index: int, item: Dict[str, object]) -> List[str]:
    duration = float(item["duration"])
    fade = min(0.20, max(0.10, duration * 0.09))
    fade_out_start = max(0.0, duration - fade)

    if item["kind"] == "title":
        return [
            f"[{input_index}:a]atrim=start=0:end={duration:.3f},asetpts=PTS-STARTPTS,aresample=48000[a{label_index}]"
        ]

    if bool(item.get("has_audio", False)):
        start = float(item["start"])
        end = float(item["end"])
        return [
            f"[{input_index}:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS,aresample=48000,highpass=f=55,lowpass=f=14000,afade=t=in:st=0:d={fade:.3f},afade=t=out:st={fade_out_start:.3f}:d={fade:.3f}[a{label_index}]"
        ]

    return [
        f"anullsrc=r=48000:cl=stereo,atrim=duration={duration:.3f}[a{label_index}]"
    ]


def write_filter_script(
    plan: TimelinePlan,
    title_assets: Dict[str, Path],
    script_path: Path,
    frame_width: int = MASTER_FRAME[0],
    frame_height: int = MASTER_FRAME[1],
    pixel_format: str = "yuv422p10le",
    draft: bool = False,
) -> Tuple[List[str], str, str]:
    inputs, mapping = _input_map(plan, title_assets)
    script_lines: List[str] = []
    concat_refs: List[str] = []
    has_live_audio = any(
        item.get("kind") != "title" and bool(item.get("has_audio", False))
        for item in plan.items
    )

    for index, item in enumerate(plan.items):
        if item["kind"] == "title":
            key = str(title_assets[str(index)].resolve())
        else:
            key = str(Path(str(item["clip_path"])).resolve())
        input_index = mapping[key]
        script_lines.extend(
            _video_chain(
                index,
                input_index,
                item,
                plan.fps,
                frame_width=frame_width,
                frame_height=frame_height,
                pixel_format=pixel_format,
                draft=draft,
            )
        )
        script_lines.extend(_audio_chain(index, input_index, item))
        concat_refs.append(f"[v{index}][a{index}]")

    script_lines.append(
        "".join(concat_refs) + f"concat=n={len(plan.items)}:v=1:a=1[vcat][acat]"
    )
    if has_live_audio:
        script_lines.append("[acat]loudnorm=I=-14:TP=-1:LRA=11,aresample=48000[aout]")
    else:
        script_lines.append("[acat]aresample=48000[aout]")
    script_path.write_text(";\n".join(script_lines) + ";\n", encoding="utf-8")
    return inputs, "[vcat]", "[aout]"


def render_master(plan: TimelinePlan, asset_dir: Path, build_dir: Path, output_path: Path) -> None:
    asset_dir.mkdir(parents=True, exist_ok=True)
    build_dir.mkdir(parents=True, exist_ok=True)
    title_assets = ensure_title_assets(plan, asset_dir)
    filter_script = build_dir / f"{output_path.stem}_filtergraph.ffscript"
    inputs, video_label, audio_label = write_filter_script(plan, title_assets, filter_script)

    command: List[str] = ["ffmpeg", "-y"]
    for input_path in inputs:
        command.extend(["-i", input_path])
    command.extend(
        [
            "-filter_complex_script",
            str(filter_script),
            "-map",
            video_label,
            "-map",
            audio_label,
            "-c:v",
            "prores_ks",
            "-profile:v",
            "3",
            "-pix_fmt",
            "yuv422p10le",
            "-c:a",
            "pcm_s24le",
            str(output_path),
        ]
    )
    _run(command)


def render_draft(plan: TimelinePlan, asset_dir: Path, build_dir: Path, output_path: Path) -> None:
    asset_dir.mkdir(parents=True, exist_ok=True)
    build_dir.mkdir(parents=True, exist_ok=True)
    title_assets = ensure_title_assets(
        plan,
        asset_dir,
        frame_width=DRAFT_FRAME[0],
        frame_height=DRAFT_FRAME[1],
        preset="veryfast",
        crf=18,
    )
    filter_script = build_dir / f"{output_path.stem}_filtergraph.ffscript"
    inputs, video_label, audio_label = write_filter_script(
        plan,
        title_assets,
        filter_script,
        frame_width=DRAFT_FRAME[0],
        frame_height=DRAFT_FRAME[1],
        pixel_format="yuv420p",
        draft=True,
    )

    command: List[str] = ["ffmpeg", "-y"]
    for input_path in inputs:
        command.extend(["-i", input_path])
    command.extend(
        [
            "-filter_complex_script",
            str(filter_script),
            "-map",
            video_label,
            "-map",
            audio_label,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(output_path),
        ]
    )
    _run(command)


def render_delivery_base(plan: TimelinePlan, asset_dir: Path, build_dir: Path, output_path: Path) -> None:
    asset_dir.mkdir(parents=True, exist_ok=True)
    build_dir.mkdir(parents=True, exist_ok=True)
    title_assets = ensure_title_assets(plan, asset_dir)
    filter_script = build_dir / f"{output_path.stem}_filtergraph.ffscript"
    inputs, video_label, audio_label = write_filter_script(plan, title_assets, filter_script)

    command: List[str] = ["ffmpeg", "-y"]
    for input_path in inputs:
        command.extend(["-i", input_path])
    command.extend(
        [
            "-filter_complex_script",
            str(filter_script),
            "-map",
            video_label,
            "-map",
            audio_label,
            "-c:v",
            "h264_videotoolbox",
            "-b:v",
            "40M",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-c:a",
            "aac",
            "-b:a",
            "320k",
            str(output_path),
        ]
    )
    _run(command)


def grade_delivery_variant(source_path: Path, output_path: Path, video_filter: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(source_path),
        "-vf",
        video_filter,
        "-c:v",
        "h264_videotoolbox",
        "-b:v",
        "35M",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-c:a",
        "copy",
        str(output_path),
    ]
    _run(command)


def render_delivery_chunked(
    plan: TimelinePlan,
    asset_dir: Path,
    build_dir: Path,
    chunk_dir: Path,
    base_output_path: Path,
    final_output_path: Path,
    video_filter: str,
) -> Dict[str, List[str]]:
    chunk_dir.mkdir(parents=True, exist_ok=True)
    build_dir.mkdir(parents=True, exist_ok=True)
    chunks = split_plan_into_chunks(plan)
    if not chunks:
        raise ValueError("Timeline plan is empty; nothing to render.")

    base_chunks: List[Path] = []
    final_chunks: List[Path] = []
    total_chunks = len(chunks)

    for index, chunk in enumerate(chunks, start=1):
        label = str(chunk["label"])
        slug = str(chunk["slug"])
        chunk_plan = chunk["plan"]
        chunk_prefix = f"{index:03d}_{slug}"
        chunk_asset_dir = asset_dir / "chunks" / chunk_prefix
        chunk_build_dir = build_dir / "chunks" / chunk_prefix
        base_chunk_path = chunk_dir / f"{chunk_prefix}_base_4k.mp4"
        final_chunk_path = chunk_dir / f"{chunk_prefix}_final_4k.mp4"

        _log_step(f"[{index}/{total_chunks}] render base chunk: {label}")
        render_delivery_base(chunk_plan, chunk_asset_dir, chunk_build_dir, base_chunk_path)
        base_chunks.append(base_chunk_path)

        _log_step(f"[{index}/{total_chunks}] grade final chunk: {label}")
        grade_delivery_variant(base_chunk_path, final_chunk_path, video_filter)
        final_chunks.append(final_chunk_path)

    _log_step(f"concat base chunks -> {base_output_path.name}")
    concat_copy_videos(base_chunks, base_output_path, build_dir / "concat")
    _log_step(f"concat final chunks -> {final_output_path.name}")
    concat_copy_videos(final_chunks, final_output_path, build_dir / "concat")

    return {
        "base_chunks": [str(path.resolve()) for path in base_chunks],
        "final_chunks": [str(path.resolve()) for path in final_chunks],
    }


def grade_preview_variant(source_path: Path, output_path: Path, video_filter: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(source_path),
        "-vf",
        video_filter,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "16",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-c:a",
        "copy",
        str(output_path),
    ]
    _run(command)


def grade_master_variant(source_path: Path, output_path: Path, video_filter: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(source_path),
        "-vf",
        video_filter,
        "-c:v",
        "prores_ks",
        "-profile:v",
        "3",
        "-pix_fmt",
        "yuv422p10le",
        "-c:a",
        "copy",
        str(output_path),
    ]
    _run(command)


def render_youtube(master_path: Path, output_path: Path) -> None:
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(master_path),
        "-c:v",
        "libx264",
        "-preset",
        "slow",
        "-crf",
        "16",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-c:a",
        "aac",
        "-b:a",
        "320k",
        str(output_path),
    ]
    _run(command)


def write_chapters(plan: TimelinePlan, output_path: Path) -> None:
    lines = [f"{entry['timecode']} {entry['label']}" for entry in plan.chapters]
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
