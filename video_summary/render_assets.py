from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .utils import VideoSummaryError


FONT_CANDIDATES = (
    Path("/System/Library/Fonts/AppleSDGothicNeo.ttc"),
    Path("/Library/Fonts/Arial Unicode.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJKkr-Regular.otf"),
)
MOSAIC_COLUMNS = 7
MOSAIC_ROWS = 7
MOSAIC_CAPACITY = MOSAIC_COLUMNS * MOSAIC_ROWS


def create_card(path: Path, title: str, subtitle: str, width: int, height: int, config: dict[str, Any]) -> None:
    canvas = Image.new("RGB", (width, height), "#11151c")
    draw = ImageDraw.Draw(canvas)
    accent = max(8, width // 180)
    draw.rectangle((0, 0, accent, height), fill="#ffb15a")
    draw.ellipse((int(width * 0.72), int(-height * 0.30), int(width * 1.10), int(height * 0.38)), fill="#1d2735")
    draw.ellipse((int(-width * 0.12), int(height * 0.72), int(width * 0.30), int(height * 1.35)), fill="#182331")
    font_path = Path(str(config["render"].get("font_file", ""))).expanduser()
    title_font = load_font(font_path, max(34, width // 22))
    subtitle_font = load_font(font_path, max(20, width // 48))
    wrapped_title = wrap_text(draw, title, title_font, int(width * 0.76))
    wrapped_subtitle = wrap_text(draw, subtitle, subtitle_font, int(width * 0.72))
    title_box = draw.multiline_textbbox((0, 0), wrapped_title, font=title_font, spacing=10, align="center")
    subtitle_box = draw.multiline_textbbox((0, 0), wrapped_subtitle, font=subtitle_font, spacing=6, align="center")
    total_height = (title_box[3] - title_box[1]) + (subtitle_box[3] - subtitle_box[1]) + height * 0.05
    title_y = (height - total_height) / 2
    draw.multiline_text(
        (width / 2, title_y), wrapped_title, font=title_font, fill="#ffffff", anchor="ma", align="center", spacing=10
    )
    subtitle_y = title_y + (title_box[3] - title_box[1]) + height * 0.07
    draw.multiline_text(
        (width / 2, subtitle_y), wrapped_subtitle, font=subtitle_font, fill="#c8d0db", anchor="ma", align="center", spacing=6
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(path, "PNG", optimize=True)


def create_mosaic_card(
    path: Path,
    frame_paths: list[Path],
    title: str,
    subtitle: str,
    width: int,
    height: int,
    config: dict[str, Any],
    *,
    columns: int = MOSAIC_COLUMNS,
    rows: int = MOSAIC_ROWS,
) -> None:
    if width <= 0 or height <= 0:
        raise VideoSummaryError("모자이크 카드 해상도는 양수여야 합니다.")
    if columns <= 0 or rows <= 0:
        raise VideoSummaryError("모자이크 카드 그리드는 양수여야 합니다.")
    if not frame_paths:
        raise VideoSummaryError("모자이크 카드에 사용할 대표 프레임이 없습니다.")

    selected = _sample_mosaic_frames(frame_paths, columns * rows)
    gap = max(2, min(width, height) // 180)
    x_spans = _mosaic_axis_spans(width, columns, gap)
    y_spans = _mosaic_axis_spans(height, rows, gap)
    canvas = Image.new("RGB", (width, height), "#10151d")
    try:
        for index, frame_path in enumerate(selected):
            row, column = divmod(index, columns)
            left, right = x_spans[column]
            top, bottom = y_spans[row]
            tile_size = (right - left, bottom - top)
            with Image.open(Path(frame_path)) as source:
                source.draft("RGB", (tile_size[0] * 2, tile_size[1] * 2))
                oriented = ImageOps.exif_transpose(source)
                try:
                    with oriented.convert("RGB") as rgb:
                        with ImageOps.fit(
                            rgb,
                            tile_size,
                            method=Image.Resampling.LANCZOS,
                            centering=(0.5, 0.5),
                        ) as tile:
                            canvas.paste(tile, (left, top))
                finally:
                    if oriented is not source:
                        oriented.close()

        with Image.new("L", (width, height), 96) as shade:
            canvas.paste("#05080c", (0, 0, width, height), shade)
        _draw_mosaic_card_text(canvas, title, subtitle, config)
        path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(path, "PNG", optimize=True)
    finally:
        canvas.close()


def _sample_mosaic_frames(frame_paths: list[Path], capacity: int) -> list[Path]:
    paths = [Path(value) for value in frame_paths]
    if len(paths) <= capacity:
        return paths
    if capacity == 1:
        return [paths[len(paths) // 2]]
    return [paths[index * (len(paths) - 1) // (capacity - 1)] for index in range(capacity)]


def _mosaic_axis_spans(total: int, count: int, gap: int) -> list[tuple[int, int]]:
    available = total - gap * (count - 1)
    if available < count:
        raise VideoSummaryError("모자이크 카드 해상도가 그리드에 비해 너무 작습니다.")
    base, remainder = divmod(available, count)
    spans: list[tuple[int, int]] = []
    start = 0
    for index in range(count):
        size = base + (1 if index < remainder else 0)
        spans.append((start, start + size))
        start += size + gap
    return spans


def _draw_mosaic_card_text(
    canvas: Image.Image,
    title: str,
    subtitle: str,
    config: dict[str, Any],
) -> None:
    width, height = canvas.size
    draw = ImageDraw.Draw(canvas)
    font_path = Path(str(config["render"].get("font_file", ""))).expanduser()
    title_font = load_font(font_path, max(32, width // 18))
    subtitle_font = load_font(font_path, max(18, width // 48))
    wrapped_title = wrap_text(draw, title.strip(), title_font, int(width * 0.72))
    wrapped_subtitle = wrap_text(draw, subtitle.strip(), subtitle_font, int(width * 0.64))
    if not wrapped_title and not wrapped_subtitle:
        return
    spacing = max(4, height // 90)
    title_box = draw.multiline_textbbox((0, 0), wrapped_title, font=title_font, spacing=spacing, align="center")
    subtitle_box = draw.multiline_textbbox(
        (0, 0), wrapped_subtitle, font=subtitle_font, spacing=spacing, align="center"
    )
    title_height = title_box[3] - title_box[1] if wrapped_title else 0
    subtitle_height = subtitle_box[3] - subtitle_box[1] if wrapped_subtitle else 0
    title_width = title_box[2] - title_box[0] if wrapped_title else 0
    subtitle_width = subtitle_box[2] - subtitle_box[0] if wrapped_subtitle else 0
    separation = max(10, height // 30) if wrapped_title and wrapped_subtitle else 0
    total_height = title_height + separation + subtitle_height

    horizontal_padding = max(24, width // 28)
    vertical_padding = max(18, height // 36)
    panel_width = min(
        int(width * 0.84),
        max(int(width * 0.58), max(title_width, subtitle_width) + horizontal_padding * 2),
    )
    panel_height = min(
        int(height * 0.56),
        max(int(height * 0.22), total_height + vertical_padding * 2),
    )
    panel_left = (width - panel_width) // 2
    panel_top = (height - panel_height) // 2
    panel_radius = max(12, min(panel_width, panel_height) // 10)
    panel_border = max(1, width // 960)
    with Image.new("RGBA", (panel_width, panel_height), (0, 0, 0, 0)) as panel:
        panel_draw = ImageDraw.Draw(panel)
        panel_draw.rounded_rectangle(
            (0, 0, panel_width - 1, panel_height - 1),
            radius=panel_radius,
            fill=(8, 14, 22, 190),
            outline=(255, 177, 90, 145),
            width=panel_border,
        )
        canvas.paste(panel, (panel_left, panel_top), panel)

    top = max(0.0, (height - total_height) / 2.0)
    shadow_offset = max(1, width // 640)
    if wrapped_title:
        draw.multiline_text(
            (width / 2 + shadow_offset, top + shadow_offset),
            wrapped_title,
            font=title_font,
            fill="#000000",
            anchor="ma",
            align="center",
            spacing=spacing,
            stroke_width=max(1, width // 960),
            stroke_fill="#000000",
        )
        draw.multiline_text(
            (width / 2, top),
            wrapped_title,
            font=title_font,
            fill="#ffffff",
            anchor="ma",
            align="center",
            spacing=spacing,
            stroke_width=max(1, width // 960),
            stroke_fill="#15191f",
        )
    if wrapped_subtitle:
        subtitle_y = top + title_height + separation
        draw.multiline_text(
            (width / 2 + shadow_offset, subtitle_y + shadow_offset),
            wrapped_subtitle,
            font=subtitle_font,
            fill="#000000",
            anchor="ma",
            align="center",
            spacing=spacing,
        )
        draw.multiline_text(
            (width / 2, subtitle_y),
            wrapped_subtitle,
            font=subtitle_font,
            fill="#f0c38a",
            anchor="ma",
            align="center",
            spacing=spacing,
        )


def create_lower_third(path: Path, text: str, width: int, height: int, config: dict[str, Any]) -> None:
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font_path = Path(str(config["render"].get("font_file", ""))).expanduser()
    font = load_font(font_path, max(22, width // 52))
    margin = max(30, width // 32)
    box_height = max(76, height // 10)
    text_box = draw.textbbox((0, 0), text, font=font)
    box_width = min(width - 2 * margin, text_box[2] - text_box[0] + margin * 2)
    top = height - margin - box_height
    draw.rounded_rectangle(
        (margin, top, margin + box_width, top + box_height), radius=box_height // 4, fill=(12, 17, 24, 210)
    )
    draw.rectangle((margin, top, margin + max(7, width // 240), top + box_height), fill=(255, 177, 90, 255))
    draw.text((margin + margin * 0.7, top + box_height / 2), text, font=font, fill="white", anchor="lm")
    path.parent.mkdir(parents=True, exist_ok=True)
    overlay.save(path, "PNG", optimize=True)


def load_font(path: Path, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (path, *FONT_CANDIDATES)
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            return ImageFont.truetype(str(candidate), size)
        except OSError:
            continue
    return ImageFont.load_default()


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
    words = text.split()
    if len(words) <= 1:
        lines: list[str] = []
        current = ""
        for character in text:
            candidate = current + character
            if current and draw.textlength(candidate, font=font) > max_width:
                lines.append(current)
                current = character
            else:
                current = candidate
        if current:
            lines.append(current)
        return "\n".join(lines)
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and draw.textlength(candidate, font=font) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return "\n".join(lines)


def vtt_time(seconds: float) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def chapter_time(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def chapter_seconds(line: str) -> float:
    value = line.split(" ", 1)[0]
    parts = [int(part) for part in value.split(":")]
    return float(parts[0] * 60 + parts[1]) if len(parts) == 2 else float(parts[0] * 3600 + parts[1] * 60 + parts[2])
