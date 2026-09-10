from __future__ import annotations

import copy
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from video_summary.candidates import (
    _candidate_exclusion_reason,
    _candidate_reviewed_inclusion_reason,
    _range_rule_matches_clip,
    _split_exact_exclusion_windows,
    _validate_reviewed_inclusion_rules,
)
from video_summary.cli import build_parser, execute
from video_summary.models import Clip
from video_summary.project import DEFAULT_CONFIG, _validate_config, project_paths, save_config
from video_summary.review import create_review_server
from video_summary.utils import VideoSummaryError, write_json


def source_clip(relative_path: str = "IMG_[1].MOV") -> Clip:
    return Clip(
        clip_id="clip", path="/unused/source.mov", relative_path=relative_path,
        fingerprint="reviewed-source", size_bytes=1, duration=20.0,
        captured_at="2026-08-20T10:00:00+09:00", capture_source="metadata",
        day_key="2026-08-20", travel_day=1, width=1080, height=1920,
        fps=30, codec="h264", rotation=0, has_audio=True,
    )


class ReviewSourceRulesTests(unittest.TestCase):
    def rule(self, **overrides):
        return {
            "match": "IMG_[1].MOV", "match_type": "exact",
            "source_fingerprint": "reviewed-source", "start": 5.0, "end": 10.0,
            "reason": "음식이 나오고 먹는 행동을 확인", **overrides,
        }

    def test_exact_rule_is_literal_and_never_matches_nested_basename(self):
        rule = self.rule()
        self.assertTrue(_range_rule_matches_clip(source_clip(), rule))
        self.assertFalse(_range_rule_matches_clip(source_clip("iphone/IMG_[1].MOV"), rule))
        self.assertFalse(_range_rule_matches_clip(source_clip("IMG_1.MOV"), rule))
        self.assertTrue(_range_rule_matches_clip(source_clip("iphone/IMG_1.MOV"), {"match": "IMG_*.MOV"}))

    def test_source_bound_include_and_exclude_keep_overlap_semantics(self):
        clip, rule = source_clip(), self.rule()
        _validate_reviewed_inclusion_rules([clip], [rule], [])
        self.assertEqual(_candidate_reviewed_inclusion_reason(clip, 5, 10, [rule]), rule["reason"])
        self.assertEqual(_candidate_exclusion_reason(clip, 8, 12, [rule]), rule["reason"])
        self.assertIsNone(_candidate_exclusion_reason(clip, 10, 12, [rule]))

    def test_changed_or_missing_source_stops_both_keep_and_exclude(self):
        for clips in ([], [replace(source_clip(), fingerprint="replacement")]):
            for include, exclude in (([self.rule()], []), ([], [self.rule()])):
                with self.subTest(clips=clips, include=bool(include)):
                    with self.assertRaisesRegex(VideoSummaryError, "검수한 원본"):
                        _validate_reviewed_inclusion_rules(clips, include, exclude)

    def test_exact_exclusion_preserves_neighboring_source_intervals(self):
        clip, rule = source_clip(), self.rule()
        windows = [(0.0, 20.0, "speech")]
        split = _split_exact_exclusion_windows(windows, clip, [rule])
        self.assertEqual(split, [(0, 5, "speech"), (5, 10, "speech"), (10, 20, "speech")])
        excluded = [(start, end) for start, end, _ in split if _candidate_exclusion_reason(clip, start, end, [rule])]
        self.assertEqual(excluded, [(5, 10)])
        self.assertEqual(_split_exact_exclusion_windows(windows, clip, [{"match": "*.MOV", "start": 5, "end": 10}]), windows)

    def test_legacy_glob_rules_still_work_without_fingerprint(self):
        rule = {"match": "**/*.MOV", "start": 5, "end": 10, "reason": "기존 검수"}
        _validate_reviewed_inclusion_rules([source_clip()], [rule], [])
        self.assertTrue(_range_rule_matches_clip(source_clip(), rule))

    def test_config_validates_exact_binding_and_evidence_container(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        for field in ("reviewed_include_ranges", "exclude_ranges"):
            config["editing"][field] = [self.rule()]
        _validate_config(config)
        for override in (
            {"match_type": []}, {"match_type": "prefix"}, {"match": "../other.MOV"},
            {"match": "/private/other.MOV"}, {"source_fingerprint": False},
            {"source_fingerprint": ""}, {"match_type": "glob"},
        ):
            for field in ("reviewed_include_ranges", "exclude_ranges"):
                invalid = copy.deepcopy(DEFAULT_CONFIG)
                invalid["editing"][field] = [self.rule(**override)]
                with self.subTest(override=override, field=field):
                    with self.assertRaises(VideoSummaryError):
                        _validate_config(invalid)
        for evidence in ({}, [None], ["claim"]):
            config = copy.deepcopy(DEFAULT_CONFIG)
            config["editing"]["reviewed_evidence"] = evidence
            with self.assertRaisesRegex(VideoSummaryError, "reviewed_evidence"):
                _validate_config(config)


class ReviewCliTests(unittest.TestCase):
    def test_review_accepts_existing_workspace_and_ephemeral_port(self):
        args = build_parser().parse_args(["review", "--project", "trip", "--project-dir", "/tmp/trip", "--port", "0"])
        self.assertEqual(args.port, 0)
        self.assertEqual(args.workspace, "/tmp/trip")
        self.assertFalse(args.json)
        self.assertFalse(hasattr(args, "force"))

    def test_review_rejects_invalid_port(self):
        for value in ("-1", "65536", "2.5", "abc"):
            with self.subTest(value=value), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                build_parser().parse_args(["review", "--project", "trip", "--port", value])

    def test_review_json_does_not_initialize_or_save_project(self):
        with tempfile.TemporaryDirectory() as directory:
            args = build_parser().parse_args(["review", "--project", "trip", "--workspace", directory, "--json"])
            with patch("video_summary.review.build_review_catalog", return_value={"events": []}) as catalog:
                self.assertEqual(execute(args), {"events": []})
            self.assertEqual(catalog.call_count, 1)
            self.assertFalse((Path(directory) / ".video-summary").exists())

    def test_unavailable_port_has_actionable_cli_error(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = project_paths(directory, "trip")
            paths.ensure()
            save_config(paths, copy.deepcopy(DEFAULT_CONFIG))
            write_json(paths.manifest, {"clips": []})
            with patch("video_summary.review.ReviewServer", side_effect=OSError("port in use")):
                with self.assertRaisesRegex(VideoSummaryError, "--port 0"):
                    create_review_server(paths, port=8765)


if __name__ == "__main__":
    unittest.main()
