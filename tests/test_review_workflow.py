"""Synthetic media regressions for the review → analyze → plan boundary.

No personal media, remote model, or subjective recognition score is used. The
observations below are fixture labels testing persistence/coverage, not claims
that a color-bar video contains a meal or a person.
"""
from __future__ import annotations

import copy
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from video_summary.candidates import load_candidates
from video_summary.media import load_clips, scan_project
from video_summary.pipeline import analyze_project
from video_summary.planner import plan_project
from video_summary.project import DEFAULT_CONFIG, load_config, project_paths, save_config
from video_summary.review import apply_review_action, build_review_catalog
from video_summary.utils import file_fingerprint, read_json, write_json


def make_review_fixture(workspace: Path):
    source = workspace / "source"
    source.mkdir()
    path = source / "IMG_[1].mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10:duration=15",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=15",
        "-c:v", "libx264", "-threads", "1", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-movflags", "+faststart", "-shortest", str(path),
    ], check=True)
    path.with_suffix(".srt").write_text(
        "1\n00:00:00,100 --> 00:00:01,000\n식당에 들어가요.\n\n"
        "2\n00:00:13,000 --> 00:00:14,000\n이제 나가요.\n", encoding="utf-8",
    )
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["project"]["name"] = "Event Review Demo"
    config["editing"].update(preserve_family_interviews=False, preserve_meal_events=False)
    config["date_overrides"] = [{"match": "*.mp4", "captured_at": "2026-08-20T12:00:00+09:00"}]
    paths = project_paths(workspace, config["project"]["name"])
    paths.ensure()
    save_config(paths, config)
    scan_project(paths, source, config)
    analyze_project(paths, config)
    plan_project(paths, config, planner_name="local")
    return paths, path


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class ReviewWorkflowTests(unittest.TestCase):
    def test_legacy_analysis_cache_backfills_source_provenance_without_regeneration(self):
        with tempfile.TemporaryDirectory() as directory:
            paths, _ = make_review_fixture(Path(directory))
            config = load_config(paths)
            payload = read_json(paths.candidates)
            provenance = payload.pop("source_fingerprints")
            write_json(paths.candidates, payload)
            frame_fingerprints = {path.name: file_fingerprint(path) for path in paths.frames.glob("*.jpg")}
            analyze_project(paths, config)
            current = read_json(paths.candidates)
            self.assertEqual(current["source_fingerprints"], provenance)
            self.assertEqual(current["candidate_set_hash"], payload["candidate_set_hash"])
            self.assertEqual(current["candidates"], payload["candidates"])
            self.assertEqual({path.name: file_fingerprint(path) for path in paths.frames.glob("*.jpg")}, frame_fingerprints)

    def test_reviewed_core_survives_replanning_without_forcing_full_context(self):
        with tempfile.TemporaryDirectory() as directory:
            paths, source = make_review_fixture(Path(directory))
            original_fingerprint = file_fingerprint(source)
            config = load_config(paths)
            clip = load_clips(paths, config)[0]
            initial = build_review_catalog(paths)
            self.assertFalse(initial["pending_reanalysis"])
            self.assertFalse(initial["plan_stale"])
            observation = {
                "clip_id": clip.clip_id, "source_fingerprint": clip.fingerprint,
                "kind": "meal_body", "basis": "visual", "description": "회귀 fixture: 식사 본체",
                "core_range": {"start": 6.0, "end": 8.0},
                "context_range": {"start": 2.0, "end": 12.0}, "source_cue_ids": [],
            }
            saved = apply_review_action(paths, {
                "expected_revision": initial["revision"], "action": "include",
                "clip_id": clip.clip_id, "start": 6.0, "end": 8.0,
                "reason": "식사 본체 검수 fixture", "observation": observation,
            })
            self.assertTrue(saved["pending_reanalysis"])
            self.assertTrue(saved["plan_stale"])
            self.assertNotEqual(saved["revision"], initial["revision"])

            config = load_config(paths)
            analyze_project(paths, config)
            plan = plan_project(paths, config, planner_name="local")
            candidates = load_candidates(paths, config)
            mandatory = [candidate for candidate in candidates if candidate.reviewed_inclusion_reason]
            self.assertEqual([(candidate.start, candidate.end) for candidate in mandatory], [(6.0, 8.0)])
            segments = {segment["candidate_id"]: segment for episode in plan["episodes"] for segment in episode["segments"]}
            for candidate in mandatory:
                self.assertEqual(segments[candidate.candidate_id]["speed"], 1.0)
            catalog = build_review_catalog(paths)
            self.assertFalse(catalog["pending_reanalysis"])
            self.assertFalse(catalog["plan_stale"])
            reviewed = [item for day in catalog["days"] for event in day["events"] for item in event["candidates"]
                        if "meal_body" in item["evidence"]["confirmed_visual_kinds"]]
            self.assertTrue(reviewed)
            self.assertTrue(all(item["selected"] for item in reviewed))
            self.assertEqual(file_fingerprint(source), original_fingerprint)

            # A precise exclusion is not allowed to discard its whole old
            # candidate. All source seconds remain represented in the catalog.
            apply_review_action(paths, {
                "expected_revision": catalog["revision"], "action": "exclude",
                "clip_id": clip.clip_id, "start": 10.0, "end": 11.0, "reason": "사적 장면 fixture",
            })
            config = load_config(paths)
            analyze_project(paths, config)
            replanned = plan_project(paths, config, planner_name="local")
            candidates = load_candidates(paths, config)
            excluded = [candidate for candidate in candidates if candidate.exclusion_reason]
            self.assertEqual([(candidate.start, candidate.end) for candidate in excluded], [(10.0, 11.0)])
            kept_ids = {segment["candidate_id"] for episode in replanned["episodes"] for segment in episode["segments"]}
            self.assertTrue(all(candidate.candidate_id not in kept_ids for candidate in excluded))
            self.assertAlmostEqual(sum(candidate.duration for candidate in candidates), clip.duration, places=3)
            self.assertEqual(len(build_review_catalog(paths)["history"]), 2)
            self.assertEqual(file_fingerprint(source), original_fingerprint)


if __name__ == "__main__":
    unittest.main()
