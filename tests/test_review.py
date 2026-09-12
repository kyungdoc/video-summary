from __future__ import annotations

import copy
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from video_summary.candidates import _candidate_cache_key
from video_summary.models import Candidate, Clip
from video_summary.media import _scan_config_signature
from video_summary.project import DEFAULT_CONFIG, load_config, project_paths, save_config
from video_summary.review import (
    MAX_BODY, ReviewConflict, _byte_range, _recover_pending,
    apply_review_action, build_review_catalog, create_review_server,
)
from video_summary.state import project_lock
from video_summary.utils import VideoSummaryError, file_fingerprint, stable_hash, write_json


class ReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.paths = project_paths(self.root, "review-test")
        self.paths.ensure()
        save_config(self.paths, copy.deepcopy(DEFAULT_CONFIG))
        self.source = self.root / "originals"
        self.source.mkdir()
        self.file = self.source / "meal[1].mp4"
        self.file.write_bytes(b"0123456789")
        self.clip = self.make_clip("clip-1", self.file)
        second = self.source / "phone" / "meal[1].mp4"
        second.parent.mkdir()
        second.write_bytes(b"abcdefghij")
        self.other_clip = self.make_clip("clip-2", second)
        write_json(self.paths.manifest, {"source_dir": str(self.source),
                                       "scan_config_hash": _scan_config_signature(load_config(self.paths)), "clips": [
            self.clip.to_dict(), self.other_clip.to_dict(),
        ]})
        self.frame = self.paths.frames / "candidate-1.jpg"
        self.frame.write_bytes(b"jpeg")
        self.candidate = Candidate(
            candidate_id="candidate-1", clip_id=self.clip.clip_id, day_key=self.clip.day_key,
            travel_day=1, start=2.0, end=8.0, captured_at=self.clip.captured_at,
            transcript="food", roles=["meal"], score=1, speech_ratio=0, motion_score=0,
            visual_quality=1, location="식당", frame_path=str(self.frame.relative_to(self.paths.root)),
            story_event_id="meal-event", story_stage="body", exclusion_reason="privacy",
        )
        self.omitted = copy.deepcopy(self.candidate)
        self.omitted.candidate_id = "candidate-2"
        self.omitted.start, self.omitted.end = 8.0, 10.0
        self.omitted.exclusion_reason = None
        write_json(self.paths.candidates, {
            "candidate_set_hash": "test-hash", "cache_key": _candidate_cache_key(
                self.paths, [self.clip, self.other_clip], load_config(self.paths)),
            "source_fingerprints": {self.clip.clip_id: self.clip.fingerprint, self.other_clip.clip_id: self.other_clip.fingerprint},
            "candidates": [self.candidate.to_dict(), self.omitted.to_dict()],
        })
        write_json(self.paths.plan, {"candidate_set_hash": "test-hash", "episodes": [{"segments": [
            {"candidate_id": "candidate-1", "speed": 1.0, "reason": "먹는 장면"},
        ]}]})

    def make_clip(self, clip_id: str, path: Path) -> Clip:
        return Clip(
            clip_id=clip_id, path=str(path), relative_path=str(path.relative_to(self.source)),
            fingerprint=file_fingerprint(path), size_bytes=path.stat().st_size, duration=10.0,
            captured_at="2026-09-01T12:00:00+09:00", capture_source="metadata",
            day_key="2026-09-01", travel_day=1, width=1920, height=1080, fps=30,
            codec="h264", rotation=0, has_audio=False, source_kind="phone",
            capture_time_confidence="high", sequence_at="2026-09-01T08:30:00+09:00",
            sequence_source="trusted_anchor",
        )

    def action(self, kind: str = "include", **changes: object) -> dict:
        result = {"expected_revision": build_review_catalog(self.paths)["revision"],
                  "action": kind, "clip_id": self.clip.clip_id,
                  "start": 2.0, "end": 8.0, "reason": "식사 본체 보존"}
        result.update(changes)
        return result

    def test_catalog_retains_omitted_excluded_and_unanalysed_source(self) -> None:
        catalog = build_review_catalog(self.paths)
        self.assertFalse(catalog["pending_reanalysis"])
        self.assertFalse(catalog["plan_stale"])
        events = catalog["days"][0]["events"]
        candidates = {item["candidate_id"]: item for event in events for item in event["candidates"]}
        self.assertEqual(len(candidates), 3)
        self.assertTrue(candidates["candidate-1"]["selected"])
        self.assertEqual(candidates["candidate-1"]["exclusion_reason"], "privacy")
        self.assertFalse(candidates["candidate-2"]["selected"])
        self.assertEqual(candidates["source-clip-2"]["origin"], "unanalysed")
        self.assertNotEqual(candidates["candidate-1"]["sequence_at"], candidates["candidate-1"]["captured_at"])
        self.assertNotIn(str(self.source), json.dumps(catalog))

    def test_include_is_exact_fingerprinted_durable_and_source_immutable(self) -> None:
        before = self.file.read_bytes()
        request = self.action()
        catalog = apply_review_action(self.paths, request)
        rule = load_config(self.paths)["editing"]["reviewed_include_ranges"][0]
        self.assertEqual(rule["match"], "meal[1].mp4")
        self.assertEqual(rule["match_type"], "exact")
        self.assertEqual(rule["source_fingerprint"], self.clip.fingerprint)
        self.assertEqual(self.file.read_bytes(), before)
        self.assertNotEqual(catalog["revision"], request["expected_revision"])
        self.assertTrue(catalog["pending_reanalysis"])
        self.assertTrue(catalog["plan_stale"])
        self.assertEqual(catalog["history"][0]["action"], "include")
        self.assertEqual(catalog["history"][0]["applied"], 1)
        with sqlite3.connect(self.paths.root / "review.sqlite3") as connection:
            snapshot = connection.execute("SELECT snapshot_yaml FROM revisions").fetchone()[0]
        self.assertIn("source_fingerprint", snapshot)
        raw_phone = next(event for event in catalog["days"][0]["events"] if event["event_id"] == "source-clip-2")
        self.assertEqual(raw_phone["candidates"][0]["reviewed_ranges"]["include"], [])

    def test_stale_revision_and_opposing_ranges_are_rejected(self) -> None:
        first = self.action()
        apply_review_action(self.paths, first)
        after = self.paths.config.read_bytes()
        with self.assertRaises(ReviewConflict):
            apply_review_action(self.paths, first)
        with self.assertRaises(ReviewConflict):
            apply_review_action(self.paths, self.action("exclude", start=7.0, end=9.0))
        self.assertEqual(self.paths.config.read_bytes(), after)
        self.assertEqual(len(build_review_catalog(self.paths)["history"]), 1)

    def test_unplaced_include_is_rejected_but_exclusion_is_allowed(self) -> None:
        manifest = json.loads(self.paths.manifest.read_text())
        manifest["clips"][0]["capture_time_basis"] = "unplaced"
        write_json(self.paths.manifest, manifest)
        with self.assertRaises(ReviewConflict):
            apply_review_action(self.paths, self.action())
        apply_review_action(self.paths, self.action("exclude"))

    def test_invalid_requests_and_replaced_sources_never_write_config(self) -> None:
        before = self.paths.config.read_bytes()
        for changes in ({"start": True}, {"end": float("nan")}, {"start": -1},
                        {"end": 11}, {"end": 2}, {"action": []}, {"clip_id": "../secret"},
                        {"reason": ""}, {"unknown": "field"}):
            with self.subTest(changes=changes), self.assertRaises(VideoSummaryError):
                apply_review_action(self.paths, self.action(**changes))
        self.file.write_bytes(b"changed source")
        with self.assertRaises(ReviewConflict):
            apply_review_action(self.paths, self.action())
        self.assertEqual(self.paths.config.read_bytes(), before)

    def test_exact_review_conflicts_with_existing_full_source_glob_exclusion(self) -> None:
        config = load_config(self.paths)
        config["editing"]["exclude_ranges"] = [{"match": "*.mp4", "end": None, "reason": "private"}]
        save_config(self.paths, config)
        with self.assertRaises(ReviewConflict):
            apply_review_action(self.paths, self.action())

    def test_human_evidence_is_normalized_and_atomic_with_include(self) -> None:
        observation = {"clip_id": self.clip.clip_id, "source_fingerprint": self.clip.fingerprint,
                       "kind": "meal_body", "basis": "visual", "description": "직접 먹는 모습을 확인",
                       "core_range": {"start": 2.0, "end": 8.0},
                       "context_range": {"start": 0.0, "end": 10.0}}
        catalog = apply_review_action(self.paths, self.action(observation=observation))
        editing = load_config(self.paths)["editing"]
        self.assertEqual(len(editing["reviewed_evidence"]), 1)
        self.assertEqual(len(editing["reviewed_include_ranges"]), 1)
        evidence = next(item["evidence"] for day in catalog["days"] for event in day["events"]
                        for item in event["candidates"] if item["candidate_id"] == "candidate-1")
        self.assertIn("meal_body", evidence["confirmed_visual_kinds"])
        before = self.paths.config.read_bytes()
        observation["core_range"] = {"start": 3.0, "end": 8.0}
        with self.assertRaises(VideoSummaryError):
            apply_review_action(self.paths, self.action("evidence", observation=observation))
        self.assertEqual(self.paths.config.read_bytes(), before)

    def test_failed_yaml_write_recovers_pending_snapshot_on_restart(self) -> None:
        request = self.action()
        with patch("video_summary.review.atomic_write_text", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                apply_review_action(self.paths, request)
        self.assertEqual(load_config(self.paths)["editing"]["reviewed_include_ranges"], [])
        server = create_review_server(self.paths)
        server.server_close()
        self.assertEqual(len(load_config(self.paths)["editing"]["reviewed_include_ranges"]), 1)
        self.assertEqual(build_review_catalog(self.paths)["history"][0]["applied"], 1)

    def test_pending_recovery_never_overwrites_intervening_manual_edit(self) -> None:
        with patch("video_summary.review.atomic_write_text", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                apply_review_action(self.paths, self.action())
        config = load_config(self.paths)
        config["project"]["name"] = "manually changed"
        save_config(self.paths, config)
        before = self.paths.config.read_bytes()
        with project_lock(self.paths.root / ".pipeline.lock"):
            with self.assertRaises(ReviewConflict):
                _recover_pending(self.paths)
        self.assertEqual(self.paths.config.read_bytes(), before)

    def test_intervening_manual_edit_during_validation_is_rejected(self) -> None:
        from video_summary.project import _validate_config as validate
        def external_edit(config: dict) -> None:
            validate(config)
            self.paths.config.write_text(self.paths.config.read_text() + "\n# external change\n")
        with patch("video_summary.review._validate_config", side_effect=external_edit):
            with self.assertRaises(ReviewConflict):
                apply_review_action(self.paths, self.action())
        self.assertTrue(self.paths.config.read_text().endswith("# external change\n"))
        self.assertEqual(load_config(self.paths)["editing"]["reviewed_include_ranges"], [])

    def test_pipeline_lock_prevents_review_mutation(self) -> None:
        with project_lock(self.paths.root / ".pipeline.lock"):
            with self.assertRaises(VideoSummaryError):
                apply_review_action(self.paths, self.action())

    def start_server(self):
        server = create_review_server(self.paths)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def stop() -> None:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
        self.addCleanup(stop)
        return server

    def request(self, server, method: str, path: str, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def authenticate(self, server) -> dict[str, str]:
        status, headers, _ = self.request(server, "GET", f"/?token={server.token}")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        return {"Cookie": headers["Set-Cookie"].split(";", 1)[0]}

    def test_http_auth_host_csrf_and_body_limits(self) -> None:
        server = self.start_server()
        self.assertEqual(self.request(server, "GET", "/api/review")[0], 403)
        auth = self.authenticate(server)
        self.assertEqual(self.request(server, "GET", "/api/review", headers={**auth, "Host": "evil.test"})[0], 403)
        status, headers, body = self.request(server, "GET", "/api/review", headers=auth)
        self.assertEqual(status, 200)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        csrf = json.loads(body)["csrf_token"]
        post_headers = {**auth, "Content-Type": "application/json", "X-Review-Token": csrf}
        data = json.dumps(self.action())
        self.assertEqual(self.request(server, "POST", "/api/review", data, post_headers)[0], 403)
        post_headers["Origin"] = server.origin
        self.assertEqual(self.request(server, "POST", "/api/review", data,
                                      {**post_headers, "X-Review-Token": "wrong"})[0], 403)
        self.assertEqual(self.request(server, "POST", "/api/review", b"",
                                      {**post_headers, "Content-Length": str(MAX_BODY + 1)})[0], 413)
        self.assertEqual(self.request(server, "POST", "/api/review", data, post_headers)[0], 200)
        self.assertEqual(self.request(server, "POST", "/api/review", data, post_headers)[0], 409)

    def test_http_source_ranges_head_and_unknown_paths(self) -> None:
        server = self.start_server()
        auth = self.authenticate(server)
        status, headers, body = self.request(server, "GET", "/media/clip-1", headers={**auth, "Range": "bytes=2-5"})
        self.assertEqual((status, body), (206, b"2345"))
        self.assertEqual(headers["Content-Range"], "bytes 2-5/10")
        status, headers, body = self.request(server, "HEAD", "/media/clip-1", headers={**auth, "Range": "bytes=-3"})
        self.assertEqual((status, body), (206, b""))
        self.assertEqual(headers["Content-Length"], "3")
        self.assertEqual(self.request(server, "GET", "/media/clip-1", headers={**auth, "Range": "bytes=2-"})[2], b"23456789")
        for invalid in ("bytes=99-", "bytes=1-2,4-5", "bytes=-0", "bytes=x-3"):
            self.assertEqual(self.request(server, "GET", "/media/clip-1", headers={**auth, "Range": invalid})[0], 416)
        self.assertEqual(self.request(server, "GET", "/media/../../project.yaml", headers=auth)[0], 404)
        self.assertEqual(self.request(server, "GET", "/frame/candidate-1", headers=auth)[2], b"jpeg")
        self.file.write_bytes(b"replaced")
        self.assertEqual(self.request(server, "GET", "/media/clip-1", headers=auth)[0], 409)

    def test_source_and_cached_frame_symlink_escapes_are_rejected(self) -> None:
        outside = self.root / "private.mp4"
        outside.write_bytes(b"private")
        self.file.unlink()
        self.file.symlink_to(outside)
        self.frame.unlink()
        self.frame.symlink_to(outside)
        server = self.start_server()
        auth = self.authenticate(server)
        self.assertEqual(self.request(server, "GET", "/media/clip-1", headers=auth)[0], 400)
        self.assertEqual(self.request(server, "GET", "/frame/candidate-1", headers=auth)[0], 404)
        with self.assertRaises(VideoSummaryError):
            apply_review_action(self.paths, self.action())

    def test_no_candidates_still_lists_all_sources(self) -> None:
        self.paths.candidates.unlink()
        self.paths.plan.unlink()
        catalog = build_review_catalog(self.paths)
        self.assertTrue(catalog["pending_reanalysis"])
        self.assertEqual(sum(len(event["candidates"]) for day in catalog["days"] for event in day["events"]), 2)

    def test_transcript_change_invalidates_revision(self) -> None:
        request = self.action()
        write_json(self.paths.transcripts / f"{self.clip.clip_id}.json", {"cues": [{"start": 2.0, "end": 5.0, "text": "food"}]})
        self.assertNotEqual(build_review_catalog(self.paths)["revision"], request["expected_revision"])
        with self.assertRaises(ReviewConflict):
            apply_review_action(self.paths, request)

    def test_include_without_observation_cannot_cut_known_speech(self) -> None:
        write_json(self.paths.transcripts / f"{self.clip.clip_id}.json", {"cues": [
            {"start": 1.0, "end": 4.0, "text": "전체 문장"},
        ]})
        before = self.paths.config.read_bytes()
        with self.assertRaises(VideoSummaryError):
            apply_review_action(self.paths, self.action(start=2.0, end=5.0))
        self.assertEqual(self.paths.config.read_bytes(), before)
        apply_review_action(self.paths, self.action(start=1.0, end=5.0))
        self.assertEqual(load_config(self.paths)["editing"]["reviewed_evidence"], [])

    def test_invalid_unrelated_eof_cue_does_not_block_early_include(self) -> None:
        write_json(self.paths.transcripts / f"{self.clip.clip_id}.json", {"cues": [
            {"start": 12.0, "end": 15.0, "text": "잘못된 종료 자막"},
        ]})
        apply_review_action(self.paths, self.action(start=2.0, end=5.0))

    def test_privacy_exclusion_can_cut_speech(self) -> None:
        write_json(self.paths.transcripts / f"{self.clip.clip_id}.json", {"cues": [
            {"start": 1.0, "end": 5.0, "text": "개인 정보"},
        ]})
        apply_review_action(self.paths, self.action("exclude", start=2.0, end=4.0))

    def test_catalog_does_not_tag_old_data_with_a_new_revision(self) -> None:
        original_load = load_config
        def read_then_edit(paths):
            config = original_load(paths)
            paths.config.write_text(paths.config.read_text() + "\n# concurrent change\n")
            return config
        with patch("video_summary.review.load_config", side_effect=read_then_edit):
            with self.assertRaises(ReviewConflict):
                build_review_catalog(self.paths)

    def test_clock_config_change_marks_scan_pending(self) -> None:
        config = load_config(self.paths)
        config["project"]["timezone"] = "UTC"
        save_config(self.paths, config)
        catalog = build_review_catalog(self.paths)
        self.assertTrue(catalog["pending_scan"])
        self.assertTrue(catalog["pending_reanalysis"])
        self.assertTrue(any("시간" in value for value in catalog["warnings"]))

    def test_rescanned_replacement_does_not_inherit_old_candidates(self) -> None:
        for duration in (10.0, 3.0):
            with self.subTest(duration=duration):
                manifest = json.loads(self.paths.manifest.read_text())
                manifest["clips"][0]["fingerprint"] = "replacement"
                manifest["clips"][0]["duration"] = duration
                write_json(self.paths.manifest, manifest)
                catalog = build_review_catalog(self.paths)
                candidates = [item for day in catalog["days"] for event in day["events"] for item in event["candidates"]]
                self.assertNotIn("candidate-1", [item["candidate_id"] for item in candidates])
                raw = next(item for item in candidates if item["clip_id"] == self.clip.clip_id)
                self.assertEqual(raw["end"], duration)
                self.assertEqual(raw["transcript"], "")
                self.assertTrue(catalog["plan_stale"])

    def test_replaced_source_never_relabels_old_transcript_as_current(self) -> None:
        write_json(self.paths.transcripts / f"{self.clip.clip_id}.json", {
            "fingerprint": "previous-source", "cues": [{"start": 2.0, "end": 5.0, "text": "old speech"}],
        })
        catalog = build_review_catalog(self.paths)
        evidence = next(item["evidence"] for day in catalog["days"] for event in day["events"]
                        for item in event["candidates"] if item["candidate_id"] == "candidate-1")
        self.assertEqual(evidence["transcript_cues"], [])
        self.assertIn("stale_transcript_cache", evidence["flags"])
        with self.assertRaises(ReviewConflict):
            apply_review_action(self.paths, self.action())
        apply_review_action(self.paths, self.action("exclude"))

    def test_legacy_candidate_id_preserves_source_binding_after_config_change(self) -> None:
        payload = json.loads(self.paths.candidates.read_text())
        payload.pop("source_fingerprints")
        for candidate in payload["candidates"]:
            candidate["candidate_id"] = "cand_" + stable_hash({
                "clip_id": self.clip.clip_id, "fingerprint": self.clip.fingerprint,
                "start": round(candidate["start"], 2), "end": round(candidate["end"], 2),
            }, length=18)
        write_json(self.paths.candidates, payload)
        config = load_config(self.paths)
        config["editing"]["reviewed_include_ranges"] = [{"match": "*.mp4", "start": 2.0, "end": 8.0, "reason": "review"}]
        save_config(self.paths, config)
        catalog = build_review_catalog(self.paths)
        self.assertTrue(catalog["pending_reanalysis"])
        candidates = [item for day in catalog["days"] for event in day["events"] for item in event["candidates"]]
        self.assertIn(payload["candidates"][0]["candidate_id"], [item["candidate_id"] for item in candidates])
        manifest = json.loads(self.paths.manifest.read_text())
        manifest["clips"][0]["fingerprint"] = "replacement"
        write_json(self.paths.manifest, manifest)
        candidates = [item for day in build_review_catalog(self.paths)["days"] for event in day["events"] for item in event["candidates"]]
        self.assertNotIn(payload["candidates"][0]["candidate_id"], [item["candidate_id"] for item in candidates])

    def test_byte_range_boundary_cases(self) -> None:
        self.assertEqual(_byte_range(None, 0), (0, -1, False))
        self.assertEqual(_byte_range("bytes=-999", 10), (0, 9, True))
        self.assertEqual(_byte_range("bytes=0-99", 10), (0, 9, True))
        for invalid in ("bytes=", "items=1-3", "bytes=5-4", "bytes=٠-١", "bytes=-"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                _byte_range(invalid, 10)


if __name__ == "__main__":
    unittest.main()
