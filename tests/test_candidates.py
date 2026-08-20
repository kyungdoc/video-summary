from __future__ import annotations

import unittest

from video_summary.candidates import _candidate_location, _candidate_windows, _merge_overlapping_windows
from video_summary.models import Clip, TranscriptCue


class CandidateCoverageTests(unittest.TestCase):
    def test_candidate_location_rechecks_ordered_rules_before_scan_fallback(self) -> None:
        clip = Clip(
            clip_id="clip", path="/tmp/day-02/DJI_0002.MP4",
            relative_path="day-02/DJI_0002.MP4", fingerprint="fp", size_bytes=1,
            duration=10.0, captured_at="2026-08-20T10:00:00+09:00",
            capture_source="filename", day_key="2026-08-20", travel_day=2,
            width=1920, height=1080, fps=30.0, codec="h264", rotation=0,
            has_audio=True, location="오키나와 · 나하",
        )
        rules = [
            {
                "label": "인천국제공항",
                "day_key": "2026-08-20",
                "match": ["airport/*"],
                "keywords": ["공항", "탑승"],
            },
            {"label": "오키나와 · 나하", "day_key": "2026-08-20", "match": ["*"]},
        ]

        self.assertEqual(_candidate_location(clip, "공항에 도착했다", rules), "인천국제공항")
        self.assertEqual(_candidate_location(clip, "바다에 도착했다", rules), "오키나와 · 나하")

    def test_overlapping_windows_are_merged_into_continuous_unique_ranges(self) -> None:
        windows = _merge_overlapping_windows(
            [
                (0.0, 6.0, "opener"),
                (2.5, 9.5, "visual"),
                (5.5, 12.5, "speech"),
                (20.0, 26.0, "closer"),
            ]
        )
        self.assertEqual(
            windows,
            [
                (0.0, 12.5, "opener"),
                (20.0, 26.0, "closer"),
            ],
        )

    def test_transitive_merge_preserves_coverage_without_long_candidates(self) -> None:
        windows = _merge_overlapping_windows(
            [
                (0.0, 18.0, "opener"),
                (10.0, 28.0, "speech"),
                (20.0, 38.0, "visual"),
                (30.0, 48.0, "closer"),
            ]
        )

        self.assertEqual(windows[0][0], 0.0)
        self.assertEqual(windows[-1][1], 48.0)
        self.assertEqual(windows[0][2], "opener")
        self.assertEqual(windows[-1][2], "closer")
        self.assertTrue(all(end - start <= 18.0 for start, end, _ in windows))
        self.assertAlmostEqual(sum(end - start for start, end, _ in windows), 48.0)
        self.assertTrue(
            all(left[1] == right[0] for left, right in zip(windows, windows[1:]))
        )

    def test_candidate_limit_keeps_start_and_end_coverage(self) -> None:
        clip = Clip(
            clip_id="clip", path="/tmp/clip.mp4", relative_path="clip.mp4", fingerprint="fp",
            size_bytes=1, duration=90.0, captured_at="2026-08-19T10:00:00+09:00",
            capture_source="filename", day_key="2026-08-19", travel_day=1,
            width=1920, height=1080, fps=30.0, codec="h264", rotation=0, has_audio=True,
        )
        cues = [
            TranscriptCue(float(index * 10), float(index * 10 + 2), f"대화 {index}")
            for index in range(9)
        ]
        signals = [
            {"time": float(index * 10), "brightness": 0.5, "contrast": 0.5, "motion": index / 10}
            for index in range(9)
        ]
        windows = _candidate_windows(clip, cues, signals, max_per_clip=3)
        self.assertEqual(len(windows), 3)
        self.assertEqual(windows[0][0], 0.0)
        self.assertEqual(windows[-1][1], 90.0)


if __name__ == "__main__":
    unittest.main()
