from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from video_summary.candidates import _auto_select_day, build_segment_candidates
from video_summary.models import ClipInfo


class CandidatesTests(unittest.TestCase):
    def test_auto_select_day_does_not_drop_all_low_priority_followups(self) -> None:
        entries = [
            {
                "candidate_id": "seg-001",
                "clip_path": "/tmp/a.mp4",
                "start": 0.0,
                "end": 4.0,
                "duration": 4.0,
                "source_time": "2026-03-20T10:00:00",
                "analysis": {"event_type": "moment", "summary": "first"},
            },
            {
                "candidate_id": "seg-002",
                "clip_path": "/tmp/a.mp4",
                "start": 5.0,
                "end": 9.0,
                "duration": 4.0,
                "source_time": "2026-03-20T10:00:05",
                "analysis": {"event_type": "meal", "summary": "second"},
            },
            {
                "candidate_id": "seg-003",
                "clip_path": "/tmp/a.mp4",
                "start": 10.0,
                "end": 14.0,
                "duration": 4.0,
                "source_time": "2026-03-20T10:00:10",
                "analysis": {"event_type": "reaction", "summary": "third"},
            },
        ]

        with patch(
            "video_summary.candidates._candidate_priority",
            side_effect=[0.53, 0.52, 0.51],
        ):
            selected = _auto_select_day(entries, brief={}, budget_seconds=20.0, day_index=1, total_days=1)

        self.assertEqual(len(selected), 3)

    def test_build_segment_candidates_writes_cue_analysis_and_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            build_dir = root / "build"
            build_dir.mkdir(parents=True)
            clip_path = root / "day1.mp4"
            clip_path.write_bytes(b"")
            brief = {
                "people": [{"name": "민수", "aliases": ["민수야"]}],
                "editorial_brief": {
                    "summary": "가족 반응과 식사 장면을 잘 살린 여행 요약",
                    "must_include": ["우와", "맛있다"],
                    "target_runtime": "10분",
                },
            }
            clips = [
                ClipInfo(
                    filename="day1.mp4",
                    path=str(clip_path.resolve()),
                    start_time=datetime.fromisoformat("2026-03-20T10:00:00"),
                    duration=30.0,
                    width=1920,
                    height=1080,
                    fps=30.0,
                    has_audio=True,
                    size_bytes=100,
                    bitrate_mbps=10.0,
                    date_key="2026-03-20",
                    travel_day=1,
                )
            ]
            results = [
                {
                    "clip_path": str(clip_path.resolve()),
                    "transcript": [
                        {"start": 1.0, "end": 3.0, "text": "민수야 우와 진짜 맛있다"},
                        {"start": 6.0, "end": 8.0, "text": "이제 공항으로 이동하자"},
                    ],
                }
            ]

            with (
                patch("video_summary.candidates.load_all_clip_results", return_value=results),
                patch("video_summary.candidates._extract_frame", return_value=None),
            ):
                output = build_segment_candidates(build_dir, "Sample Trip", clips, brief)

            analysis_path = Path(output["cue_analysis_path"])
            self.assertTrue(analysis_path.exists())
            analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
            self.assertEqual(len(analysis["days"]), 1)
            cues = analysis["days"][0]["cues"]
            self.assertEqual(len(cues), 2)
            self.assertTrue(any("민수" in cue["analysis"]["people"] for cue in cues))
            self.assertTrue(any(cue["analysis"]["features"]["food"] > 0.1 for cue in cues))
            self.assertTrue(any(cue["analysis"]["features"]["fun"] > 0.1 for cue in cues))

            selection_path = Path(output["segment_selection_path"])
            selection = json.loads(selection_path.read_text(encoding="utf-8"))
            selections = selection["days"][0]["selections"]
            self.assertGreaterEqual(len(selections), 1)
            self.assertIn("analysis_summary", selections[0])


if __name__ == "__main__":
    unittest.main()
