from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageChops

from video_summary.animated_mosaic import (
    AnimatedMosaicComposer,
    animation_frame_count,
    evenly_sample_indices,
    render_animated_mosaic,
    serpentine_positions,
    tile_reveal_progress,
    title_panel_opacity,
)
from video_summary.utils import VideoSummaryError


class AnimatedMosaicTests(unittest.TestCase):
    @staticmethod
    def make_frame(path: Path, color: tuple[int, int, int], size: tuple[int, int] = (160, 90)) -> None:
        with Image.new("RGB", size, color) as image:
            image.save(path)

    def test_serpentine_order_flows_across_alternating_rows(self) -> None:
        order = serpentine_positions(6)
        self.assertEqual(order[:6], tuple((0, column) for column in range(6)))
        self.assertEqual(order[6:12], tuple((1, column) for column in range(5, -1, -1)))
        self.assertEqual(order[-1], (5, 0))
        self.assertEqual(len(set(order)), 36)
        self.assertEqual(len(serpentine_positions(8)), 64)

    def test_sampling_is_deterministic_and_preserves_endpoints(self) -> None:
        sampled = evenly_sample_indices(70, 49)
        self.assertEqual(sampled, evenly_sample_indices(70, 49))
        self.assertEqual((sampled[0], sampled[-1]), (0, 69))
        self.assertEqual(len(sampled), 49)
        self.assertEqual(len(set(sampled)), 49)

    def test_timing_rounds_once_to_an_exact_integer_frame_duration(self) -> None:
        frame_count, duration = animation_frame_count(1.016, 30)
        self.assertEqual(frame_count, 30)
        self.assertEqual(duration, 1.0)
        with self.assertRaises(VideoSummaryError):
            animation_frame_count(float("nan"), 30)

    def test_flip_reveal_is_ordered_and_monotonic_and_panel_appears_late(self) -> None:
        first = [tile_reveal_progress(frame, 0, 49, 120, 30) for frame in range(120)]
        last = [tile_reveal_progress(frame, 48, 49, 120, 30) for frame in range(120)]
        self.assertTrue(all(left <= right for left, right in zip(first, first[1:])))
        self.assertTrue(all(left <= right for left, right in zip(last, last[1:])))
        self.assertGreater(first[30], last[30])
        self.assertEqual(title_panel_opacity(50, 120), 0.0)
        self.assertGreater(title_panel_opacity(75, 120), 0.5)
        self.assertEqual(title_panel_opacity(84, 120), 1.0)

    def test_composer_uses_requested_grid_and_emits_frames_one_at_a_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            frames: list[Path] = []
            for index in range(40):
                path = root / f"frame-{index:02d}.png"
                self.make_frame(path, ((index * 37) % 255, (index * 71) % 255, (index * 19) % 255))
                frames.append(path)
            with AnimatedMosaicComposer(
                frames,
                "CALIFORNIA",
                "2026-05-17 — 2026-05-25",
                duration=1.0,
                width=420,
                height=240,
                fps=20,
                grid_size=6,
            ) as composer:
                self.assertEqual(composer.tile_count, 36)
                self.assertEqual(composer.selected_input_indices[0], 0)
                self.assertEqual(composer.selected_input_indices[-1], 39)
                early = composer.compose_frame(2)
                late = composer.compose_frame(15)
                try:
                    self.assertEqual(early.size, (420, 240))
                    self.assertIsNotNone(ImageChops.difference(early, late).getbbox())
                finally:
                    early.close()
                    late.close()

    def test_rejects_unsupported_grid_and_style(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            frame = Path(tmpdir) / "frame.png"
            self.make_frame(frame, (20, 40, 60))
            for grid, style in ((5, "flow"), (7, "random")):
                with self.subTest(grid=grid, style=style), self.assertRaises(VideoSummaryError):
                    AnimatedMosaicComposer(
                        [frame],
                        "Trip",
                        "Dates",
                        duration=1.0,
                        width=320,
                        height=180,
                        fps=10,
                        grid_size=grid,
                        animation_style=style,
                    )

    def test_encode_failure_removes_partial_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            frame = root / "frame.png"
            output = root / "intro.mp4"
            self.make_frame(frame, (20, 40, 60))

            def fail_encode(_composer, temporary: Path, *_args) -> None:
                temporary.write_bytes(b"incomplete")
                raise VideoSummaryError("synthetic encode failure")

            with (
                patch("video_summary.animated_mosaic._encoded_mosaic_is_usable", return_value=False),
                patch("video_summary.animated_mosaic._encode_animation", side_effect=fail_encode),
                self.assertRaisesRegex(VideoSummaryError, "synthetic"),
            ):
                render_animated_mosaic(
                    output,
                    [frame],
                    "Trip",
                    "Dates",
                    1.0,
                    320,
                    180,
                    10,
                    "libx264",
                    "1M",
                    "96k",
                )

            self.assertFalse(output.exists())
            self.assertFalse(any(root.glob("*.partial.mp4")))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
    def test_real_encode_has_exact_frame_count_fps_resolution_and_silent_audio(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            frames: list[Path] = []
            for index, color in enumerate(((220, 40, 40), (40, 220, 40), (40, 40, 220))):
                frame = root / f"frame-{index}.png"
                self.make_frame(frame, color)
                frames.append(frame)
            output = root / "intro.mp4"
            result = render_animated_mosaic(
                output,
                frames,
                "TRIP",
                "2026",
                0.7,
                320,
                180,
                10,
                "libx264",
                "1M",
                "96k",
                grid_size=6,
            )
            self.assertTrue(output.is_file())
            self.assertEqual(result.frame_count, 7)
            self.assertEqual(result.duration, 0.7)
            self.assertEqual(result.grid, (6, 6))
            self.assertEqual(result.tile_count, 3)


if __name__ == "__main__":
    unittest.main()
