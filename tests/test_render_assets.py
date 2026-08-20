from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageChops

from video_summary.project import DEFAULT_CONFIG
from video_summary.render_assets import MOSAIC_CAPACITY, MOSAIC_COLUMNS, MOSAIC_ROWS, create_mosaic_card
from video_summary.utils import VideoSummaryError


class MosaicCardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = copy.deepcopy(DEFAULT_CONFIG)

    @staticmethod
    def make_solid(path: Path, color: tuple[int, int, int], size: tuple[int, int] = (120, 120)) -> None:
        with Image.new("RGB", size, color) as image:
            image.save(path)

    @staticmethod
    def cell_centers(total: int, count: int, gap: int) -> list[int]:
        available = total - gap * (count - 1)
        base, remainder = divmod(available, count)
        centers: list[int] = []
        start = 0
        for index in range(count):
            size = base + (1 if index < remainder else 0)
            centers.append(start + size // 2)
            start += size + gap
        return centers

    def test_output_resolution_and_unused_cells_remain_blank(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            red = root / "red.png"
            green = root / "green.png"
            output = root / "mosaic.png"
            self.make_solid(red, (240, 20, 20))
            self.make_solid(green, (20, 240, 20))

            create_mosaic_card(output, [red, green], "", "", 808, 808, self.config)

            with Image.open(output) as image:
                centers = self.cell_centers(808, MOSAIC_COLUMNS, 4)
                self.assertEqual(image.size, (808, 808))
                first = image.getpixel((centers[0], centers[0]))
                second = image.getpixel((centers[1], centers[0]))
                unused = image.getpixel((centers[2], centers[0]))
            self.assertGreater(first[0], first[1] * 3)
            self.assertGreater(second[1], second[0] * 3)
            self.assertLess(max(unused), 30)

    def test_tiles_are_center_cropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "wide.png"
            output = root / "mosaic.png"
            with Image.new("RGB", (300, 100), (0, 220, 0)) as image:
                for x in range(100):
                    for y in range(100):
                        image.putpixel((x, y), (240, 0, 0))
                        image.putpixel((200 + x, y), (0, 0, 240))
                image.save(source)

            create_mosaic_card(output, [source], "", "", 400, 400, self.config)

            with Image.open(output) as image:
                center = image.getpixel((24, 24))
            self.assertGreater(center[1], center[0] * 4)
            self.assertGreater(center[1], center[2] * 4)

    def test_title_and_date_range_are_both_drawn(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            frame = root / "black.png"
            blank = root / "blank.png"
            title_only = root / "title.png"
            period_only = root / "period.png"
            self.make_solid(frame, (0, 0, 0))
            frames = [frame] * MOSAIC_CAPACITY

            create_mosaic_card(blank, frames, "", "", 640, 360, self.config)
            create_mosaic_card(title_only, frames, "SUMMER TRIP", "", 640, 360, self.config)
            create_mosaic_card(period_only, frames, "", "2026-08-01 — 2026-08-09", 640, 360, self.config)

            with Image.open(blank) as base, Image.open(title_only) as title, Image.open(period_only) as period:
                self.assertIsNotNone(ImageChops.difference(base, title).getbbox())
                self.assertIsNotNone(ImageChops.difference(base, period).getbbox())

    def test_more_than_capacity_frames_are_sampled_deterministically_with_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            frames: list[Path] = []
            for index in range(70):
                frame = root / f"frame-{index:02d}.png"
                if index == 0:
                    color = (240, 10, 30)
                elif index == 69:
                    color = (10, 220, 240)
                else:
                    color = (40 + index, 30, 30)
                self.make_solid(frame, color)
                frames.append(frame)
            first = root / "first.png"
            second = root / "second.png"

            create_mosaic_card(first, frames, "", "", 808, 808, self.config)
            create_mosaic_card(second, frames, "", "", 808, 808, self.config)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            with Image.open(first) as image:
                centers = self.cell_centers(808, MOSAIC_COLUMNS, 4)
                first_cell = image.getpixel((centers[0], centers[0]))
                last_cell = image.getpixel((centers[-1], centers[-1]))
            self.assertGreater(first_cell[0], first_cell[1] * 4)
            self.assertGreater(last_cell[1], last_cell[0] * 4)
            self.assertGreater(last_cell[2], last_cell[0] * 4)

    def test_all_forty_nine_cells_are_filled_in_row_major_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            frames: list[Path] = []
            for index in range(MOSAIC_CAPACITY):
                frame = root / f"frame-{index:02d}.png"
                self.make_solid(frame, (3 + index * 4,) * 3)
                frames.append(frame)
            output = root / "mosaic.png"

            create_mosaic_card(output, frames, "", "", 808, 808, self.config)

            centers = self.cell_centers(808, MOSAIC_COLUMNS, 4)
            with Image.open(output) as image:
                values = [image.getpixel((x, y))[0] for y in centers for x in centers]
            self.assertEqual((MOSAIC_COLUMNS, MOSAIC_ROWS, MOSAIC_CAPACITY), (7, 7, 49))
            self.assertEqual(len(set(values)), MOSAIC_CAPACITY)
            self.assertEqual(values, sorted(values))

    def test_title_panel_darkens_only_the_center_background(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            frame = root / "bright.png"
            without_panel = root / "without-panel.png"
            with_panel = root / "with-panel.png"
            self.make_solid(frame, (180, 180, 180))
            frames = [frame] * MOSAIC_CAPACITY

            create_mosaic_card(without_panel, frames, "", "", 640, 360, self.config)
            create_mosaic_card(with_panel, frames, "A", "", 640, 360, self.config)

            with Image.open(without_panel) as plain, Image.open(with_panel) as titled:
                plain_center_background = plain.getpixel((200, 157))
                panel_center_background = titled.getpixel((200, 157))
                plain_corner = plain.getpixel((20, 20))
                titled_corner = titled.getpixel((20, 20))
            self.assertLess(sum(panel_center_background), sum(plain_center_background) - 60)
            self.assertEqual(titled_corner, plain_corner)

    def test_empty_frame_list_has_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "mosaic.png"
            with self.assertRaisesRegex(VideoSummaryError, "대표 프레임"):
                create_mosaic_card(output, [], "Trip", "2026-08", 640, 360, self.config)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
