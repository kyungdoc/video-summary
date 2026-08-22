from __future__ import annotations

import copy
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from video_summary.media import load_clips, scan_project
from video_summary.pipeline import analyze_project
from video_summary.planner import plan_project
from video_summary.project import DEFAULT_CONFIG, project_paths, save_config
from video_summary.renderer import render_project
from video_summary.utils import VideoSummaryError


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class PipelineIntegrationTests(unittest.TestCase):
    def test_two_day_sidecar_pipeline_renders_daily_videos(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            source = workspace / "원본 영상"
            source.mkdir()
            first = source / "여행 O'Brien DJI_20260819_053045_001_D.MP4"
            second = source / "DJI_20260820_093000_001_D.MP4"
            self._make_video(first, portrait=False, audio=True, color="0x315d8a")
            self._make_video(second, portrait=True, audio=False, color="0xc06c4e")
            first.with_suffix(".srt").write_text(
                "1\n00:00:00,200 --> 00:00:01,600\n이번 여행 어땠나요? 정말 좋았어요!\n\n",
                encoding="utf-8",
            )
            second.with_suffix(".srt").write_text(
                "1\n00:00:00,100 --> 00:00:01,500\n드디어 바다에 도착했다.\n\n", encoding="utf-8"
            )

            config = copy.deepcopy(DEFAULT_CONFIG)
            config["project"]["name"] = "Integration Trip"
            config["project"]["destination"] = "테스트 여행지"
            config["editing"]["target_minutes_per_day"] = 0.2
            config["render"].update(
                {
                    "encoder": "libx264",
                    "intro_seconds": 0.6,
                    "date_card_seconds": 0.5,
                    "outro_seconds": 0.6,
                }
            )
            config["locations"] = [
                {"label": "시장", "match": ["*0819*"]},
                {"label": "해변", "match": ["*0820*"]},
            ]
            paths = project_paths(workspace, "Integration Trip")
            paths.ensure()
            save_config(paths, config)

            manifest = scan_project(paths, source, config)
            self.assertEqual(len(manifest["days"]), 2)
            analysis = analyze_project(paths, config)
            self.assertGreaterEqual(analysis["candidates"]["count"], 2)
            plan = plan_project(paths, config, planner_name="local")
            self.assertEqual(len(plan["episodes"]), 2)
            report = render_project(paths, config, draft=True)
            self.assertEqual(report["intro_metadata"]["destination"], "테스트 여행지")
            self.assertEqual(report["intro_metadata"]["period"], "2026-08-19 — 2026-08-20")
            interview_coverage = report["moment_coverage"]["family_interviews"]
            self.assertEqual(interview_coverage["status"], "satisfied")
            self.assertEqual(interview_coverage["detected_event_count"], 1)
            self.assertEqual(
                interview_coverage["required_candidate_count"],
                interview_coverage["selected_candidate_count"],
            )
            self.assertEqual(len(report["outputs"]), 2)
            for output in report["outputs"]:
                movie = Path(output["path"])
                self.assertTrue(movie.exists())
                self.assertGreater(movie.stat().st_size, 1000)
                self.assertTrue(Path(output["subtitles"]).exists())
                self.assertTrue(Path(output["chapters"]).exists())

            cached_manifest = scan_project(paths, source, config)
            cached_analysis = analyze_project(paths, config)
            cached_plan = plan_project(paths, config, planner_name="local")
            cached_report = render_project(paths, config, draft=True)
            self.assertEqual(cached_manifest["manifest_hash"], manifest["manifest_hash"])
            self.assertTrue(cached_analysis["transcription"]["cached"])
            self.assertEqual(cached_plan, plan)
            self.assertEqual(cached_report, report)

            trip_config = copy.deepcopy(config)
            trip_config["editing"]["episode_mode"] = "trip"
            trip_report = render_project(paths, trip_config, draft=True)
            self.assertEqual(trip_report["mode"], "trip")
            self.assertEqual(trip_report["intro_metadata"]["destination"], "테스트 여행지")
            self.assertEqual(len(trip_report["outputs"]), 1)
            self.assertEqual(Path(trip_report["outputs"][0]["path"]).name, "trip-summary-draft.mp4")

            changed_analysis = copy.deepcopy(config)
            changed_analysis["analysis"]["asr_model"] = "medium"
            with self.assertRaisesRegex(VideoSummaryError, "analyze"):
                plan_project(paths, changed_analysis, planner_name="local")

            with first.open("ab") as handle:
                handle.write(b"changed-after-scan")
            with self.assertRaisesRegex(VideoSummaryError, "scan"):
                load_clips(paths, config)

    @staticmethod
    def _make_video(path: Path, *, portrait: bool, audio: bool, color: str) -> None:
        size = "180x320" if portrait else "320x180"
        args = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
            "-i", f"color=c={color}:s={size}:r=30:d=2",
        ]
        if audio:
            args.extend(["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2", "-shortest"])
        args.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p"])
        if audio:
            args.extend(["-c:a", "aac"])
        args.extend(["-y", str(path)])
        subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


if __name__ == "__main__":
    unittest.main()
