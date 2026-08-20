from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Iterator, Sequence

from PIL import Image, ImageDraw, ImageOps

from .render_assets import load_font, wrap_text
from .utils import VideoSummaryError


DEFAULT_GRID_SIZE = 7
SUPPORTED_GRID_SIZES = frozenset({6, 7, 8})
DEFAULT_ANIMATION_STYLE = "flow"
SUPPORTED_ANIMATION_STYLES = frozenset({DEFAULT_ANIMATION_STYLE})
ANIMATED_MOSAIC_POLICY_VERSION = 1


@dataclass(frozen=True, slots=True)
class AnimatedMosaicResult:
    path: Path
    duration: float
    frame_count: int
    width: int
    height: int
    fps: int
    grid: tuple[int, int]
    tile_count: int
    selected_input_indices: tuple[int, ...]
    reveal_order: tuple[tuple[int, int], ...]
    animation_style: str


@dataclass(frozen=True, slots=True)
class _TitlePanel:
    image: Image.Image
    left: int
    top: int


def animation_frame_count(duration: float, fps: int) -> tuple[int, float]:
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(float(duration)):
        raise VideoSummaryError("동적 모자이크 길이는 유한한 숫자여야 합니다.")
    if isinstance(fps, bool) or not isinstance(fps, int) or fps <= 0:
        raise VideoSummaryError("동적 모자이크 fps는 양의 정수여야 합니다.")
    if duration <= 0:
        raise VideoSummaryError("동적 모자이크 길이는 0보다 커야 합니다.")
    frame_count = max(1, int(math.floor(float(duration) * fps + 0.5)))
    return frame_count, frame_count / fps


def evenly_sample_indices(count: int, take: int) -> tuple[int, ...]:
    if count <= 0 or take <= 0:
        return ()
    if take >= count:
        return tuple(range(count))
    if take == 1:
        return (count // 2,)
    return tuple(index * (count - 1) // (take - 1) for index in range(take))


def sample_frame_paths(frame_paths: Sequence[Path], capacity: int) -> tuple[tuple[int, Path], ...]:
    paths = tuple(Path(path) for path in frame_paths)
    return tuple((index, paths[index]) for index in evenly_sample_indices(len(paths), capacity))


def serpentine_positions(grid_size: int = DEFAULT_GRID_SIZE) -> tuple[tuple[int, int], ...]:
    _validate_grid_size(grid_size)
    positions: list[tuple[int, int]] = []
    for row in range(grid_size):
        columns = range(grid_size) if row % 2 == 0 else range(grid_size - 1, -1, -1)
        positions.extend((row, column) for column in columns)
    return tuple(positions)


def tile_reveal_progress(
    frame_index: int,
    tile_index: int,
    tile_count: int,
    frame_count: int,
    fps: int,
) -> float:
    """Return an eased 0..1 horizontal flip/reveal value for one tile."""
    if tile_count <= 0 or tile_index < 0 or tile_index >= tile_count or frame_count <= 0 or fps <= 0:
        return 0.0
    first_start = int(round((frame_count - 1) * 0.035))
    last_start = int(round((frame_count - 1) * 0.56))
    if tile_count == 1:
        start = first_start
    else:
        start = int(round(first_start + (last_start - first_start) * tile_index / (tile_count - 1)))
    effective_duration = frame_count / fps
    flip_seconds = min(0.38, max(2.0 / fps, effective_duration * 0.10))
    flip_frames = max(1, min(int(round(flip_seconds * fps)), max(1, int(round(frame_count * 0.18)))))
    raw = min(1.0, max(0.0, (frame_index - start + 1) / flip_frames))
    return math.sin(raw * math.pi / 2.0)


def title_panel_opacity(frame_index: int, frame_count: int) -> float:
    if frame_count <= 0:
        return 0.0
    start = int(round((frame_count - 1) * 0.58))
    fade_frames = max(1, int(round(frame_count * 0.10)))
    raw = min(1.0, max(0.0, (frame_index - start + 1) / fade_frames))
    return raw * raw * (3.0 - 2.0 * raw)


class AnimatedMosaicComposer:
    """Compose one RGB frame at a time while retaining only small prepared tiles."""

    def __init__(
        self,
        frame_paths: Sequence[Path],
        title: str,
        subtitle: str,
        *,
        duration: float,
        width: int,
        height: int,
        fps: int,
        grid_size: int = DEFAULT_GRID_SIZE,
        animation_style: str = DEFAULT_ANIMATION_STYLE,
        font_file: str | Path | None = None,
    ) -> None:
        _validate_render_dimensions(width, height)
        _validate_grid_size(grid_size)
        _validate_animation_style(animation_style)
        self.frame_count, self.duration = animation_frame_count(duration, fps)
        self.width = width
        self.height = height
        self.fps = fps
        self.grid_size = grid_size
        self.animation_style = animation_style
        self.reveal_order = serpentine_positions(grid_size)
        sampled = sample_frame_paths(frame_paths, grid_size * grid_size)
        if not sampled:
            raise VideoSummaryError("동적 모자이크에 사용할 대표 프레임이 없습니다.")
        self.selected_input_indices = tuple(index for index, _path in sampled)
        self.frame_paths = tuple(path for _index, path in sampled)
        self.gap = max(2, min(width, height) // 210)
        self.x_spans = _axis_spans(width, grid_size, self.gap)
        self.y_spans = _axis_spans(height, grid_size, self.gap)
        self.tiles = self._prepare_tiles()
        self.panel = _create_title_panel(
            title,
            subtitle,
            width,
            height,
            Path(font_file).expanduser() if font_file else Path(""),
        )
        self._closed = False

    @property
    def tile_count(self) -> int:
        return len(self.tiles)

    def __enter__(self) -> AnimatedMosaicComposer:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        for tile in self.tiles:
            tile.close()
        self.panel.image.close()
        self._closed = True

    def frames(self) -> Iterator[Image.Image]:
        for frame_index in range(self.frame_count):
            yield self.compose_frame(frame_index)

    def compose_frame(self, frame_index: int) -> Image.Image:
        if self._closed:
            raise VideoSummaryError("이미 닫힌 동적 모자이크 composer입니다.")
        if not 0 <= frame_index < self.frame_count:
            raise VideoSummaryError("동적 모자이크 프레임 번호가 범위를 벗어났습니다.")
        canvas = Image.new("RGB", (self.width, self.height), "#10151d")
        try:
            for tile_index, tile in enumerate(self.tiles):
                progress = tile_reveal_progress(
                    frame_index,
                    tile_index,
                    self.tile_count,
                    self.frame_count,
                    self.fps,
                )
                if progress <= 0.0:
                    continue
                row, column = self.reveal_order[tile_index]
                left, right = self.x_spans[column]
                top, bottom = self.y_spans[row]
                display_width = max(1, min(tile.width, int(round(tile.width * progress))))
                offset_y = int(round((1.0 - progress) * tile.height * (0.07 if tile_index % 2 else -0.07)))
                if display_width == tile.width:
                    display = tile
                    owns_display = False
                else:
                    display = tile.resize((display_width, tile.height), Image.Resampling.BICUBIC)
                    owns_display = True
                try:
                    paste_x = left + (right - left - display_width) // 2
                    canvas.paste(display, (paste_x, top + offset_y))
                finally:
                    if owns_display:
                        display.close()

            # A light cinematic shade keeps the grid coherent without hiding it.
            with Image.new("L", (self.width, self.height), 48) as shade:
                canvas.paste("#05080c", (0, 0, self.width, self.height), shade)

            panel_opacity = title_panel_opacity(frame_index, self.frame_count)
            if panel_opacity > 0.0 and self.panel.image.getbbox() is not None:
                with self.panel.image.copy() as panel:
                    alpha = panel.getchannel("A").point(lambda value: int(round(value * panel_opacity)))
                    panel.putalpha(alpha)
                    canvas.paste(panel, (self.panel.left, self.panel.top), panel)

            fade_out_frames = max(1, int(round(min(0.45, self.duration * 0.12) * self.fps)))
            fade_out_start = self.frame_count - fade_out_frames
            if frame_index >= fade_out_start:
                amount = min(255, int(round(255 * (frame_index - fade_out_start + 1) / fade_out_frames)))
                with Image.new("L", (self.width, self.height), amount) as fade_mask:
                    canvas.paste("#000000", (0, 0, self.width, self.height), fade_mask)
            return canvas
        except BaseException:
            canvas.close()
            raise

    def _prepare_tiles(self) -> list[Image.Image]:
        tiles: list[Image.Image] = []
        try:
            for tile_index, frame_path in enumerate(self.frame_paths):
                row, column = self.reveal_order[tile_index]
                left, right = self.x_spans[column]
                top, bottom = self.y_spans[row]
                tile_size = (right - left, bottom - top)
                try:
                    with Image.open(frame_path) as source:
                        source.draft("RGB", (tile_size[0] * 2, tile_size[1] * 2))
                        oriented = ImageOps.exif_transpose(source)
                        try:
                            with oriented.convert("RGB") as rgb:
                                tile = ImageOps.fit(
                                    rgb,
                                    tile_size,
                                    method=Image.Resampling.LANCZOS,
                                    centering=(0.5, 0.5),
                                )
                        finally:
                            if oriented is not source:
                                oriented.close()
                except OSError as exc:
                    raise VideoSummaryError(f"동적 모자이크 대표 프레임을 읽을 수 없습니다: {frame_path}") from exc
                tiles.append(tile)
            return tiles
        except BaseException:
            for tile in tiles:
                tile.close()
            raise


def render_animated_mosaic(
    output: Path,
    frame_paths: Sequence[Path],
    title: str,
    subtitle: str,
    duration: float,
    width: int,
    height: int,
    fps: int,
    encoder: str,
    bitrate: str,
    audio_bitrate: str,
    *,
    grid_size: int = DEFAULT_GRID_SIZE,
    animation_style: str = DEFAULT_ANIMATION_STYLE,
    font_file: str | Path | None = None,
    force: bool = False,
    ffmpeg_binary: str = "ffmpeg",
    ffprobe_binary: str = "ffprobe",
) -> AnimatedMosaicResult:
    """Render a deterministic, exact-frame animated travel mosaic with silent AAC."""
    output = Path(output)
    with AnimatedMosaicComposer(
        frame_paths,
        title,
        subtitle,
        duration=duration,
        width=width,
        height=height,
        fps=fps,
        grid_size=grid_size,
        animation_style=animation_style,
        font_file=font_file,
    ) as composer:
        result = AnimatedMosaicResult(
            path=output,
            duration=composer.duration,
            frame_count=composer.frame_count,
            width=width,
            height=height,
            fps=fps,
            grid=(grid_size, grid_size),
            tile_count=composer.tile_count,
            selected_input_indices=composer.selected_input_indices,
            reveal_order=composer.reveal_order[: composer.tile_count],
            animation_style=animation_style,
        )
        if not force and _encoded_mosaic_is_usable(result, ffprobe_binary):
            return result
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.stem}.animated.partial.mp4")
        temporary.unlink(missing_ok=True)
        try:
            _encode_animation(
                composer,
                temporary,
                encoder,
                bitrate,
                audio_bitrate,
                ffmpeg_binary,
            )
            temporary_result = AnimatedMosaicResult(
                path=temporary,
                duration=result.duration,
                frame_count=result.frame_count,
                width=result.width,
                height=result.height,
                fps=result.fps,
                grid=result.grid,
                tile_count=result.tile_count,
                selected_input_indices=result.selected_input_indices,
                reveal_order=result.reveal_order,
                animation_style=result.animation_style,
            )
            if not _encoded_mosaic_is_usable(temporary_result, ffprobe_binary):
                raise VideoSummaryError("동적 모자이크 출력의 프레임 수 또는 형식이 올바르지 않습니다.")
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
        return result


def _encode_animation(
    composer: AnimatedMosaicComposer,
    output: Path,
    encoder: str,
    bitrate: str,
    audio_bitrate: str,
    ffmpeg_binary: str,
) -> None:
    command = [
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-threads",
        "2",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-video_size",
        f"{composer.width}x{composer.height}",
        "-framerate",
        str(composer.fps),
        "-i",
        "pipe:0",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=48000:cl=stereo",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-frames:v",
        str(composer.frame_count),
        "-vf",
        f"setpts=N/({composer.fps}*TB),format=yuv420p",
        "-af",
        f"atrim=duration={composer.duration:.6f},asetpts=PTS-STARTPTS",
        "-t",
        f"{composer.duration:.6f}",
        *_video_encode_args(encoder, bitrate),
        "-c:a",
        "aac",
        "-b:a",
        str(audio_bitrate),
        "-ar",
        "48000",
        "-ac",
        "2",
        "-video_track_timescale",
        "90000",
        "-movflags",
        "+faststart",
        "-shortest",
        "-y",
        str(output),
    ]
    try:
        with tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
            )
            try:
                if process.stdin is None:
                    raise VideoSummaryError("FFmpeg 입력 스트림을 열지 못했습니다.")
                for frame in composer.frames():
                    try:
                        process.stdin.write(frame.tobytes())
                    finally:
                        frame.close()
                process.stdin.close()
                return_code = process.wait()
            except BaseException:
                if process.stdin is not None and not process.stdin.closed:
                    process.stdin.close()
                process.kill()
                process.wait()
                raise
            if return_code != 0:
                stderr.seek(0)
                detail = stderr.read().decode("utf-8", errors="replace").strip()
                raise VideoSummaryError(f"동적 모자이크 FFmpeg 렌더 실패: {detail[-3000:] or f'exit {return_code}'}")
    except FileNotFoundError as exc:
        raise VideoSummaryError(f"필요한 명령을 찾지 못했습니다: {ffmpeg_binary}") from exc
    except BrokenPipeError as exc:
        raise VideoSummaryError("동적 모자이크를 FFmpeg로 전송하는 중 연결이 끊겼습니다.") from exc


def _encoded_mosaic_is_usable(result: AnimatedMosaicResult, ffprobe_binary: str) -> bool:
    if not result.path.is_file() or result.path.stat().st_size <= 0:
        return False
    command = [
        ffprobe_binary,
        "-v",
        "error",
        "-count_frames",
        "-show_entries",
        "stream=codec_type,codec_name,width,height,r_frame_rate,nb_frames,nb_read_frames,duration,sample_rate,channels:format=duration",
        "-of",
        "json",
        str(result.path),
    ]
    try:
        completed = subprocess.run(command, text=True, capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError, OSError):
        return False
    try:
        payload = json.loads(completed.stdout)
        streams = payload["streams"]
        video = next(stream for stream in streams if stream.get("codec_type") == "video")
        audio = next(stream for stream in streams if stream.get("codec_type") == "audio")
        encoded_frames = int(video.get("nb_read_frames") or video["nb_frames"])
        encoded_fps = float(Fraction(video["r_frame_rate"]))
        video_duration = float(video["duration"])
        audio_duration = float(audio["duration"])
        format_duration = float(payload["format"]["duration"])
        mux_tolerance = (1024 / 48000) + (1 / result.fps)
        return (
            video["codec_name"] == "h264"
            and audio["codec_name"] == "aac"
            and int(video["width"]) == result.width
            and int(video["height"]) == result.height
            and encoded_frames == result.frame_count
            and abs(encoded_fps - result.fps) < 1e-6
            and abs(video_duration - result.duration) <= max(0.002, 0.25 / result.fps)
            and int(audio["sample_rate"]) == 48000
            and int(audio["channels"]) == 2
            and abs(audio_duration - result.duration) <= mux_tolerance
            and abs(format_duration - result.duration) <= mux_tolerance
        )
    except (KeyError, StopIteration, TypeError, ValueError, ZeroDivisionError, json.JSONDecodeError):
        return False


def _video_encode_args(encoder: str, bitrate: str) -> list[str]:
    if encoder == "h264_videotoolbox":
        return [
            "-c:v",
            encoder,
            "-allow_sw",
            "1",
            "-b:v",
            bitrate,
            "-pix_fmt",
            "yuv420p",
            "-tag:v",
            "avc1",
        ]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-tag:v", "avc1"]


def _validate_grid_size(grid_size: int) -> None:
    if isinstance(grid_size, bool) or not isinstance(grid_size, int) or grid_size not in SUPPORTED_GRID_SIZES:
        raise VideoSummaryError("동적 모자이크 grid_size는 6, 7 또는 8이어야 합니다.")


def _validate_animation_style(animation_style: str) -> None:
    if animation_style not in SUPPORTED_ANIMATION_STYLES:
        raise VideoSummaryError("동적 모자이크 animation_style은 flow여야 합니다.")


def _validate_render_dimensions(width: int, height: int) -> None:
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in (width, height)):
        raise VideoSummaryError("동적 모자이크 해상도는 양의 정수여야 합니다.")
    if width % 2 or height % 2:
        raise VideoSummaryError("동적 모자이크 해상도는 H.264 출력을 위해 짝수여야 합니다.")


def _axis_spans(total: int, count: int, gap: int) -> tuple[tuple[int, int], ...]:
    available = total - gap * (count - 1)
    if available < count:
        raise VideoSummaryError("동적 모자이크 해상도가 그리드에 비해 너무 작습니다.")
    base, remainder = divmod(available, count)
    spans: list[tuple[int, int]] = []
    start = 0
    for index in range(count):
        size = base + (1 if index < remainder else 0)
        spans.append((start, start + size))
        start += size + gap
    return tuple(spans)


def _create_title_panel(
    title: str,
    subtitle: str,
    width: int,
    height: int,
    font_file: Path,
) -> _TitlePanel:
    measurement = Image.new("RGB", (1, 1))
    try:
        draw = ImageDraw.Draw(measurement)
        title_font = load_font(font_file, max(32, width // 18))
        subtitle_font = load_font(font_file, max(18, width // 48))
        wrapped_title = wrap_text(draw, title.strip(), title_font, int(width * 0.72))
        wrapped_subtitle = wrap_text(draw, subtitle.strip(), subtitle_font, int(width * 0.64))
        if not wrapped_title and not wrapped_subtitle:
            return _TitlePanel(Image.new("RGBA", (1, 1), (0, 0, 0, 0)), 0, 0)
        spacing = max(4, height // 90)
        title_box = draw.multiline_textbbox((0, 0), wrapped_title, font=title_font, spacing=spacing, align="center")
        subtitle_box = draw.multiline_textbbox(
            (0, 0), wrapped_subtitle, font=subtitle_font, spacing=spacing, align="center"
        )
    finally:
        measurement.close()

    title_height = title_box[3] - title_box[1] if wrapped_title else 0
    subtitle_height = subtitle_box[3] - subtitle_box[1] if wrapped_subtitle else 0
    title_width = title_box[2] - title_box[0] if wrapped_title else 0
    subtitle_width = subtitle_box[2] - subtitle_box[0] if wrapped_subtitle else 0
    separation = max(10, height // 30) if wrapped_title and wrapped_subtitle else 0
    total_height = title_height + separation + subtitle_height
    horizontal_padding = max(24, width // 28)
    vertical_padding = max(18, height // 36)
    panel_width = min(int(width * 0.84), max(int(width * 0.58), max(title_width, subtitle_width) + horizontal_padding * 2))
    panel_height = min(int(height * 0.56), max(int(height * 0.22), total_height + vertical_padding * 2))
    panel = Image.new("RGBA", (panel_width, panel_height), (0, 0, 0, 0))
    panel_draw = ImageDraw.Draw(panel)
    radius = max(12, min(panel_width, panel_height) // 10)
    panel_draw.rounded_rectangle(
        (0, 0, panel_width - 1, panel_height - 1),
        radius=radius,
        fill=(8, 14, 22, 218),
        outline=(255, 177, 90, 165),
        width=max(1, width // 960),
    )
    top = max(0.0, (panel_height - total_height) / 2.0)
    if wrapped_title:
        panel_draw.multiline_text(
            (panel_width / 2, top),
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
        panel_draw.multiline_text(
            (panel_width / 2, top + title_height + separation),
            wrapped_subtitle,
            font=subtitle_font,
            fill="#f0c38a",
            anchor="ma",
            align="center",
            spacing=spacing,
        )
    return _TitlePanel(panel, (width - panel_width) // 2, (height - panel_height) // 2)
