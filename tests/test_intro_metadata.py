from __future__ import annotations

import copy
import unittest

from video_summary.intro_metadata import (
    infer_destination,
    infer_travel_period,
    resolve_intro_metadata,
)
from video_summary.project import DEFAULT_CONFIG
from video_summary.utils import VideoSummaryError


class IntroMetadataTests(unittest.TestCase):
    def test_destination_is_inferred_from_known_source_folder_conventions(self) -> None:
        cases = (
            ("/Users/example/Documents/2512_vietnam-phuquoc", "Phu Quoc"),
            ("/Users/example/Documents/2605_japan-okinawa", "Okinawa"),
            ("/Users/example/Documents/2605_usa-SF", "San Francisco"),
            ("/Users/example/Documents/2026-05_japan-okinawa", "Okinawa"),
            ("/Users/example/Documents/2025-12-27_vietnam-phuquoc", "Phu Quoc"),
            ("/Users/example/Documents/Đà-Nẵng", "Đà Nẵng"),
            ("/Users/example/Documents/São-Paulo", "São Paulo"),
            ("/Users/example/Documents/south-korea-seoul", "Seoul"),
        )
        for source_dir, expected in cases:
            with self.subTest(source_dir=source_dir):
                destination, source = infer_destination("", source_dir, "fallback-trip")
                self.assertEqual(destination, expected)
                self.assertEqual(source, "source_dir")

    def test_explicit_destination_wins_and_generic_source_uses_project(self) -> None:
        explicit, explicit_source = infer_destination(
            "  푸꾸옥   가족여행  ",
            "/Users/example/Documents/2605_japan-okinawa",
            "fallback-trip",
        )
        fallback, fallback_source = infer_destination(
            "",
            "/Users/example/Documents/원본 영상",
            "sf-trip",
        )
        self.assertEqual((explicit, explicit_source), ("푸꾸옥 가족여행", "config"))
        self.assertEqual((fallback, fallback_source), ("San Francisco", "project"))

    def test_country_only_destination_is_kept_and_camera_folders_use_project(self) -> None:
        for source_dir, project_name, expected, expected_source in (
            ("/media/2605_japan", "japan-trip", "Japan", "source_dir"),
            ("/media/2605_vietnam", "vietnam-trip", "Vietnam", "source_dir"),
            ("/media/DCIM", "okinawa-trip", "Okinawa", "project"),
            ("/media/100MEDIA", "okinawa-trip", "Okinawa", "project"),
        ):
            with self.subTest(source_dir=source_dir):
                self.assertEqual(
                    infer_destination("", source_dir, project_name),
                    (expected, expected_source),
                )

    def test_period_uses_sorted_unique_corrected_day_keys(self) -> None:
        self.assertEqual(
            infer_travel_period(["2026-01-01", "2025-12-27", "2025-12-27"]),
            ("2025-12-27", "2026-01-01", "2025-12-27 — 2026-01-01"),
        )
        self.assertEqual(
            infer_travel_period(["2026-05-17"]),
            ("2026-05-17", "2026-05-17", "2026-05-17"),
        )
        with self.assertRaises(VideoSummaryError):
            infer_travel_period(["not-a-date"])

    def test_resolver_records_destination_and_period_provenance(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["project"]["name"] = "2025-phuquoc"
        manifest = {
            "source_dir": "/Users/example/Documents/2512_vietnam-phuquoc",
            "days": [
                {"day_key": "2026-01-01", "travel_day": 6},
                {"day_key": "2025-12-27", "travel_day": 1},
            ],
        }
        metadata = resolve_intro_metadata(config, manifest)
        self.assertEqual(metadata.destination, "Phu Quoc")
        self.assertEqual(metadata.destination_source, "source_dir")
        self.assertEqual(metadata.start_date, "2025-12-27")
        self.assertEqual(metadata.end_date, "2026-01-01")
        self.assertEqual(metadata.period, "2025-12-27 — 2026-01-01")
        self.assertEqual(metadata.period_source, "scan_day_key")

    def test_resolver_reports_edit_plan_period_fallback_provenance(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        metadata = resolve_intro_metadata(
            config,
            {"source_dir": "/Users/example/Documents/원본 영상", "days": []},
            ["2026-05-18", "2026-05-17"],
        )
        self.assertEqual(metadata.start_date, "2026-05-17")
        self.assertEqual(metadata.end_date, "2026-05-18")
        self.assertEqual(metadata.period_source, "edit_plan_day_key")


if __name__ == "__main__":
    unittest.main()
