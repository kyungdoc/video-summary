from __future__ import annotations

import copy
import math
import unittest

from video_summary.editorial_evidence import (
    build_candidate_evidence,
    transcript_evidence,
    validate_human_observation,
)
from video_summary.models import Candidate, TranscriptCue
from video_summary.utils import VideoSummaryError


class EditorialEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clip = {"clip_id": "clip-a", "fingerprint": "source-fingerprint", "duration": 60.0, "source_kind": "phone"}
        self.candidate = {"candidate_id": "candidate-a", "clip_id": "clip-a", "start": 10.0, "end": 20.0, "roles": ["food"], "transcript": "밥 먹으러 가자", "story_stage": "setup"}
        self.cues = [TranscriptCue(10.0, 12.0, "밥 먹으러 가자")]
        self.record = {
            "clip_id": "clip-a", "source_fingerprint": "source-fingerprint",
            "kind": "meal_body", "basis": "visual", "description": "음식이 나오고 가족이 먹는 장면을 직접 확인함",
            "core_range": {"start": 12.0, "end": 18.0},
            "context_range": {"start": 8.0, "end": 22.0},
        }

    def test_meal_approach_is_not_visual_eating_evidence(self) -> None:
        view = build_candidate_evidence(self.candidate, self.clip, self.cues)
        self.assertEqual(view["confirmed_visual_kinds"], [])
        self.assertIn("meal_body_needs_visual_review", view["flags"])
        self.assertTrue(all(item["basis"] == "inferred" for item in view["classifications"]))
        self.assertEqual(view["review_status"], "needs_review")

    def test_silent_high_scoring_iphone_is_not_falsely_verified(self) -> None:
        self.candidate.update(roles=["scenery"], transcript="", motion_score=1.0, visual_quality=1.0, score=100.0)
        view = build_candidate_evidence(self.candidate, self.clip)
        self.assertEqual(view["confirmed_visual_kinds"], [])
        self.assertIn("phone_without_transcript_visual_review", view["flags"])
        self.assertEqual(view["observations"], [])

    def test_human_action_result_record_does_not_require_speech(self) -> None:
        self.record["kind"] = "action_result"
        self.record["description"] = "아이가 뽑은 장난감을 꺼내고 웃는 것을 확인함"
        saved = validate_human_observation(self.record, self.clip)
        view = build_candidate_evidence(self.candidate, self.clip, observations=[saved])
        self.assertEqual(view["confirmed_visual_kinds"], ["action_result"])
        self.assertEqual(view["review_status"], "has_human_observations")

    def test_human_meal_body_removes_review_warning(self) -> None:
        view = build_candidate_evidence(self.candidate, self.clip, self.cues, [self.record])
        self.assertEqual(view["confirmed_visual_kinds"], ["meal_body"])
        self.assertNotIn("meal_body_needs_visual_review", view["flags"])

    def test_inferred_observation_does_not_verify_visual_coverage(self) -> None:
        self.record["basis"] = "inferred"
        view = build_candidate_evidence(self.candidate, self.clip, observations=[self.record])
        self.assertEqual(view["confirmed_visual_kinds"], [])
        self.assertIn("meal_body_needs_visual_review", view["flags"])

    def test_partial_observation_overlap_does_not_count_as_core_coverage(self) -> None:
        self.candidate["end"] = 14.0
        view = build_candidate_evidence(self.candidate, self.clip, observations=[self.record])
        self.assertEqual(len(view["observations"]), 1)
        self.assertEqual(view["confirmed_visual_kinds"], [])
        self.assertIn("observed_core_not_fully_in_candidate", view["flags"])

    def test_source_records_survive_candidate_regeneration(self) -> None:
        first = build_candidate_evidence(self.candidate, self.clip, observations=[self.record])
        self.candidate["candidate_id"] = "regenerated-id"
        second = build_candidate_evidence(self.candidate, self.clip, observations=[self.record])
        self.assertEqual(first["observations"], second["observations"])

    def test_stale_fingerprint_is_rejected_and_not_counted(self) -> None:
        self.record["source_fingerprint"] = "old-fingerprint"
        with self.assertRaisesRegex(VideoSummaryError, "source_fingerprint"):
            validate_human_observation(self.record, self.clip)
        view = build_candidate_evidence(self.candidate, self.clip, observations=[self.record])
        self.assertEqual(view["observations"], [])
        self.assertIn("stale_or_invalid_observation", view["flags"])

    def test_other_source_records_do_not_leak_into_view(self) -> None:
        self.record["clip_id"] = "another-source"
        with self.assertRaisesRegex(VideoSummaryError, "clip_id"):
            validate_human_observation(self.record, self.clip)
        view = build_candidate_evidence(self.candidate, self.clip, observations=[self.record])
        self.assertEqual(view["observations"], [])
        self.assertNotIn("stale_or_invalid_observation", view["flags"])

    def test_boundary_only_touch_is_not_overlap(self) -> None:
        self.candidate.update(start=18.0, end=20.0)
        view = build_candidate_evidence(self.candidate, self.clip, observations=[self.record])
        self.assertEqual(view["observations"], [])

    def test_empty_context_defaults_to_core_when_omitted(self) -> None:
        del self.record["context_range"]
        saved = validate_human_observation(self.record, self.clip)
        self.assertEqual(saved["core_range"], saved["context_range"])

    def test_context_must_enclose_core(self) -> None:
        self.record["context_range"] = {"start": 13.0, "end": 17.0}
        with self.assertRaisesRegex(VideoSummaryError, "context_range"):
            validate_human_observation(self.record, self.clip)

    def test_invalid_numeric_types_and_non_finite_ranges(self) -> None:
        for bad in (True, False, "12", None, math.nan, math.inf, -math.inf, 10**1000):
            for name in ("core_range", "context_range"):
                for endpoint in ("start", "end"):
                    with self.subTest(value=repr(bad)[:30], name=name, endpoint=endpoint):
                        record = copy.deepcopy(self.record)
                        record[name][endpoint] = bad
                        with self.assertRaises(VideoSummaryError):
                            validate_human_observation(record, self.clip)

    def test_invalid_order_or_out_of_source_ranges(self) -> None:
        for bounds in ((-1, 10), (10, 61), (20, 12), (12, 12)):
            with self.subTest(bounds=bounds):
                self.record["core_range"] = dict(zip(("start", "end"), bounds))
                with self.assertRaises(VideoSummaryError):
                    validate_human_observation(self.record, self.clip)

    def test_unknown_kind_basis_version_and_bad_description(self) -> None:
        for key, bad in (("kind", "food"), ("kind", []), ("basis", "automatic"), ("basis", []), ("version", True), ("version", 2), ("description", " "), ("description", "x" * 2001), ("description", None)):
            with self.subTest(key=key, bad=str(bad)[:30]):
                record = {**self.record, key: bad}
                with self.assertRaises(VideoSummaryError):
                    validate_human_observation(record, self.clip)

    def test_speech_basis_requires_current_source_cue(self) -> None:
        self.record.update(basis="speech", core_range={"start": 10.0, "end": 18.0})
        with self.assertRaisesRegex(VideoSummaryError, "발화 근거"):
            validate_human_observation(self.record, self.clip, self.cues)
        cue_id = transcript_evidence(self.cues, self.clip)[0]["cue_id"]
        self.record["source_cue_ids"] = [cue_id]
        saved = validate_human_observation(self.record, self.clip, self.cues)
        self.assertEqual(saved["source_cue_ids"], [cue_id])
        view = build_candidate_evidence(self.candidate, self.clip, self.cues, [saved])
        self.assertEqual(view["confirmed_visual_kinds"], [])

    def test_unknown_changed_or_other_source_cue_is_rejected(self) -> None:
        self.record.update(basis="speech", core_range={"start": 10.0, "end": 18.0})
        wrong_ids = ["cue-unknown", transcript_evidence(self.cues, {**self.clip, "clip_id": "another"})[0]["cue_id"], transcript_evidence([TranscriptCue(10.0, 12.0, "different")], self.clip)[0]["cue_id"]]
        for cue_id in wrong_ids:
            with self.subTest(cue_id=cue_id):
                self.record["source_cue_ids"] = [cue_id]
                with self.assertRaisesRegex(VideoSummaryError, "발화 ID"):
                    validate_human_observation(self.record, self.clip, self.cues)

    def test_cited_speech_must_be_fully_in_core(self) -> None:
        self.record.update(basis="speech", source_cue_ids=[transcript_evidence(self.cues, self.clip)[0]["cue_id"]])
        with self.assertRaisesRegex(VideoSummaryError, "발화 전체"):
            validate_human_observation(self.record, self.clip, self.cues)

    def test_invalid_or_duplicate_cue_ids_are_rejected(self) -> None:
        cue_id = transcript_evidence(self.cues, self.clip)[0]["cue_id"]
        for bad in (None, "cue", [True], [cue_id, cue_id]):
            with self.subTest(bad=bad):
                self.record["source_cue_ids"] = bad
                with self.assertRaises(VideoSummaryError):
                    validate_human_observation(self.record, self.clip, self.cues)

    def test_even_visual_core_cannot_cut_uncited_known_speech(self) -> None:
        self.record["core_range"]["start"] = 11.0
        with self.assertRaisesRegex(VideoSummaryError, "중간에 자릅니다"):
            validate_human_observation(self.record, self.clip, self.cues)

    def test_expands_core_through_overlapping_speech_chain(self) -> None:
        cues = [TranscriptCue(8.0, 11.0, "question"), TranscriptCue(7.0, 9.0, "overlap"), TranscriptCue(19.0, 24.0, "answer")]
        view = build_candidate_evidence(self.candidate, self.clip, cues, context_seconds=0)
        self.assertEqual(view["suggested_core_range"], {"start": 7.0, "end": 24.0})
        self.assertIn("speech_boundary_cut", view["flags"])
        self.record.update(core_range=view["suggested_core_range"], context_range=view["context_range"])
        validate_human_observation(self.record, self.clip, cues)

    def test_context_expands_partial_nearby_speech_and_clamps_source(self) -> None:
        self.candidate.update(start=1.0, end=58.0)
        view = build_candidate_evidence(self.candidate, self.clip, context_seconds=4.0)
        self.assertEqual(view["context_range"], {"start": 0.0, "end": 60.0})
        self.candidate.update(start=10.0, end=20.0)
        view = build_candidate_evidence(self.candidate, self.clip, [TranscriptCue(22.0, 27.0, "next thought")])
        self.assertEqual(view["context_range"]["end"], 27.0)

    def test_context_padding_rejects_invalid_values(self) -> None:
        for value in (True, "4", -1, math.nan, math.inf):
            with self.subTest(value=value), self.assertRaises(VideoSummaryError):
                build_candidate_evidence(self.candidate, self.clip, context_seconds=value)

    def test_cue_ids_are_stable_under_reordering_and_addition(self) -> None:
        first = transcript_evidence(self.cues, self.clip)[0]
        extended = transcript_evidence([TranscriptCue(1.0, 2.0, "intro"), *self.cues], self.clip)
        self.assertEqual(first, extended[1])
        duplicated = transcript_evidence([*self.cues, *self.cues], self.clip)
        self.assertEqual(len(duplicated), 1)

    def test_inputs_are_not_mutated(self) -> None:
        original = copy.deepcopy((self.record, self.candidate, self.clip))
        build_candidate_evidence(self.candidate, self.clip, self.cues, [self.record])
        self.assertEqual(original, (self.record, self.candidate, self.clip))

    def test_cue_input_ids_are_not_trusted(self) -> None:
        cue = {"cue_id": "user-controlled", "start": 10, "end": 12, "text": "hello"}
        self.assertNotEqual(transcript_evidence([cue], self.clip)[0]["cue_id"], "user-controlled")

    def test_invalid_source_and_candidate_are_rejected(self) -> None:
        for clip in ({**self.clip, "duration": True}, {**self.clip, "fingerprint": ""}, {**self.clip, "duration": 0}):
            with self.subTest(clip=clip), self.assertRaises(VideoSummaryError):
                build_candidate_evidence(self.candidate, clip)
        with self.assertRaisesRegex(VideoSummaryError, "clip_id"):
            build_candidate_evidence({**self.candidate, "clip_id": "unknown"}, self.clip)

    def test_current_candidate_dataclass_is_supported(self) -> None:
        candidate = Candidate(
            **self.candidate, day_key="2025-02-01", travel_day=1,
            captured_at="2025-02-01T12:00:00+09:00", score=0.9,
            speech_ratio=0.2, motion_score=0.4, visual_quality=0.6,
            location=None, frame_path="",
        )
        self.assertEqual(build_candidate_evidence(candidate, self.clip)["candidate_id"], "candidate-a")

    def test_malformed_record_is_reported_without_hiding_other_records(self) -> None:
        view = build_candidate_evidence(self.candidate, self.clip, observations=[None, self.record])
        self.assertIn("stale_or_invalid_observation", view["flags"])
        self.assertEqual(view["confirmed_visual_kinds"], ["meal_body"])

    def test_bad_legacy_transcript_is_displayable_but_not_treated_as_silence(self) -> None:
        for bounds in ((-1, 15), (15, 65), (14, 14), (math.nan, 12), (True, 12)):
            with self.subTest(bounds=bounds):
                bad_cue = {"start": bounds[0], "end": bounds[1], "text": "legacy timing"}
                view = build_candidate_evidence(self.candidate, self.clip, [bad_cue], [self.record])
                self.assertIn("invalid_transcript_evidence", view["flags"])
                self.assertIn("observation_needs_transcript_review", view["flags"])
                self.assertNotIn("no_transcript_evidence", view["flags"])
                self.assertNotIn("phone_without_transcript_visual_review", view["flags"])
                self.assertEqual(view["confirmed_visual_kinds"], [])
                self.assertEqual(view["transcript_cues"], [])
                with self.assertRaises(VideoSummaryError):
                    validate_human_observation(self.record, self.clip, [bad_cue])

    def test_valid_cues_still_display_when_another_legacy_cue_is_invalid(self) -> None:
        cues = [*self.cues, {"start": 59.0, "end": 64.0, "text": "outside source"}]
        view = build_candidate_evidence(self.candidate, self.clip, cues)
        self.assertEqual(len(view["transcript_cues"]), 1)
        self.assertEqual(view["transcript_cues"][0]["text"], "밥 먹으러 가자")
        self.assertIn("invalid_transcript_evidence", view["flags"])

    def test_authoritative_phone_source_wins_over_unknown_candidate(self) -> None:
        self.candidate["source_kind"] = "unknown"
        view = build_candidate_evidence(self.candidate, self.clip)
        self.assertIn("phone_without_transcript_visual_review", view["flags"])

    def test_bad_eof_cue_does_not_block_unrelated_early_human_core(self) -> None:
        clip = {**self.clip, "duration": 15.0}
        candidate = {**self.candidate, "start": 0.0, "end": 2.0}
        record = {**self.record, "core_range": {"start": 0.0, "end": 2.0},
                  "context_range": {"start": 0.0, "end": 5.0}}
        cues = [{"start": 10.0, "end": 40.0, "text": "bad EOF cue"}]
        saved = validate_human_observation(record, clip, cues)
        view = build_candidate_evidence(candidate, clip, cues, [saved])
        self.assertIn("invalid_transcript_evidence", view["flags"])
        self.assertNotIn("observation_needs_transcript_review", view["flags"])
        self.assertEqual(view["confirmed_visual_kinds"], ["meal_body"])
        self.assertEqual(saved["core_range"], {"start": 0.0, "end": 2.0})
        self.assertEqual(cues[0]["end"], 40.0)
        with self.assertRaises(VideoSummaryError):
            transcript_evidence(cues, clip)

    def test_bad_eof_cue_blocks_intersecting_core_without_clamping(self) -> None:
        clip = {**self.clip, "duration": 15.0}
        candidate = {**self.candidate, "start": 12.0, "end": 14.0}
        record = {**self.record, "core_range": {"start": 12.0, "end": 14.0},
                  "context_range": {"start": 10.0, "end": 15.0}}
        cues = [{"start": 10.0, "end": 40.0, "text": "bad EOF cue"}]
        with self.assertRaisesRegex(VideoSummaryError, "기존 발화 시간 오류"):
            validate_human_observation(record, clip, cues)
        view = build_candidate_evidence(candidate, clip, cues, [record])
        self.assertEqual(view["confirmed_visual_kinds"], [])
        self.assertIn("observation_needs_transcript_review", view["flags"])

    def test_unlocatable_cue_blocks_even_early_core(self) -> None:
        record = {**self.record, "core_range": {"start": 0.0, "end": 2.0},
                  "context_range": {"start": 0.0, "end": 5.0}}
        cues = [{"start": math.nan, "end": 40.0, "text": "unlocatable cue"}]
        with self.assertRaisesRegex(VideoSummaryError, "기존 발화 시간 오류"):
            validate_human_observation(record, self.clip, cues)

    def test_out_of_source_cue_cannot_be_cited_even_if_core_is_elsewhere(self) -> None:
        record = {**self.record, "basis": "speech", "source_cue_ids": ["invalid-cue-id"]}
        cues = [{"cue_id": "invalid-cue-id", "start": 61.0, "end": 70.0, "text": "outside"}]
        with self.assertRaisesRegex(VideoSummaryError, "발화 ID"):
            validate_human_observation(record, self.clip, cues)


if __name__ == "__main__":
    unittest.main()
