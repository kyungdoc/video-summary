from __future__ import annotations

import copy
import unittest

from video_summary.models import Candidate
from video_summary.planner import (
    _planner_candidates_payload,
    _prompt_role_weights,
    local_plan,
    validate_and_normalize_plan,
)
from video_summary.project import DEFAULT_CONFIG
from video_summary.utils import VideoSummaryError


def candidate(candidate_id: str, captured_at: str, start: float = 0.0) -> Candidate:
    return Candidate(
        candidate_id=candidate_id,
        clip_id=f"clip_{candidate_id}",
        day_key="2026-11-01",
        travel_day=1,
        start=start,
        end=start + 5.0,
        captured_at=captured_at,
        transcript="우와 정말 맛있다",
        roles=["fun", "food"],
        score=0.8,
        speech_ratio=0.5,
        motion_score=0.3,
        visual_quality=0.7,
        location="뉴욕",
        frame_path=f"frames/{candidate_id}.jpg",
    )


class PlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.config["project"]["name"] = "Test Trip"
        self.config["editing"]["target_minutes_per_day"] = 0.2
        self.candidates = [
            candidate("c1", "2026-11-01T08:00:00-05:00"),
            candidate("c2", "2026-11-01T09:00:00-05:00", 6.0),
        ]

    def valid_payload(self) -> dict:
        return {
            "version": 1,
            "project": "Test Trip",
            "candidate_set_hash": "hash",
            "episodes": [
                {
                    "day_key": "2026-11-01",
                    "travel_day": 1,
                    "title": "DAY 1",
                    "subtitle": "New York",
                    "summary": "첫날",
                    "target_duration": 12.0,
                    "segments": [
                        {"candidate_id": "c1", "role": "fun", "reason": "재미있는 시작"},
                        {"candidate_id": "c2", "role": "food", "reason": "식사 흐름"},
                    ],
                }
            ],
        }

    def test_valid_plan_is_normalized(self) -> None:
        plan = validate_and_normalize_plan(
            self.valid_payload(), self.config, "재미와 음식을 살려줘", self.candidates, "hash", "file"
        )
        self.assertEqual(plan.episodes[0].target_duration, 12.0)
        self.assertEqual(len(plan.episodes[0].segments), 2)

    def test_hash_and_required_fields_are_strict(self) -> None:
        payload = self.valid_payload()
        del payload["candidate_set_hash"]
        with self.assertRaises(VideoSummaryError):
            validate_and_normalize_plan(payload, self.config, "x", self.candidates, "hash", "file")
        payload = self.valid_payload()
        del payload["episodes"][0]["segments"][0]["reason"]
        with self.assertRaises(VideoSummaryError):
            validate_and_normalize_plan(payload, self.config, "x", self.candidates, "hash", "file")

    def test_plan_must_include_the_earliest_candidate_as_the_day_start(self) -> None:
        payload = self.valid_payload()
        payload["episodes"][0]["segments"] = [payload["episodes"][0]["segments"][1]]
        with self.assertRaisesRegex(VideoSummaryError, "가장 이른 후보"):
            validate_and_normalize_plan(payload, self.config, "x", self.candidates, "hash", "file")

    def test_hook_is_only_allowed_first(self) -> None:
        payload = self.valid_payload()
        payload["episodes"][0]["segments"][1]["role"] = "hook"
        with self.assertRaises(VideoSummaryError):
            validate_and_normalize_plan(payload, self.config, "x", self.candidates, "hash", "file")

    def test_hook_must_also_follow_capture_chronology(self) -> None:
        payload = self.valid_payload()
        payload["episodes"][0]["segments"] = [
            {"candidate_id": "c2", "role": "hook", "reason": "후반 장면"},
            {"candidate_id": "c1", "role": "journey", "reason": "앞선 장면"},
        ]
        with self.assertRaisesRegex(VideoSummaryError, "시간순"):
            validate_and_normalize_plan(payload, self.config, "x", self.candidates, "hash", "file")

        payload["episodes"][0]["segments"] = [
            {"candidate_id": "c1", "role": "hook", "reason": "첫 장면"},
            {"candidate_id": "c2", "role": "journey", "reason": "후속 장면"},
        ]
        plan = validate_and_normalize_plan(
            payload, self.config, "x", self.candidates, "hash", "file"
        )
        self.assertEqual([item.candidate_id for item in plan.episodes[0].segments], ["c1", "c2"])

    def test_local_plan_never_moves_a_later_fun_candidate_forward(self) -> None:
        self.config["editing"]["cold_open"] = True
        self.candidates[0].roles = ["journey"]
        self.candidates[0].score = 0.2
        self.candidates[1].roles = ["fun"]
        self.candidates[1].score = 1.0
        plan = local_plan(self.config, "여정과 재미", self.candidates, "hash")
        segments = plan.episodes[0].segments
        self.assertEqual([item.candidate_id for item in segments], ["c1", "c2"])
        self.assertNotEqual(segments[0].role, "hook")

    def test_local_plan_preserves_the_day_start_and_end_anchors(self) -> None:
        self.config["editing"]["target_minutes_per_day"] = 10.0 / 60.0
        anchors = [
            candidate("early", "2026-11-01T08:00:00-05:00"),
            candidate("middle", "2026-11-01T09:00:00-05:00", 6.0),
            candidate("late", "2026-11-01T10:00:00-05:00", 12.0),
        ]
        anchors[0].score = 0.05
        anchors[1].score = 1.0
        anchors[2].score = 0.1
        plan = local_plan(self.config, "여정", anchors, "hash")
        self.assertEqual(
            [item.candidate_id for item in plan.episodes[0].segments],
            ["early", "late"],
        )

    def test_local_plan_keeps_a_strong_silent_visual_anchor(self) -> None:
        self.config["editing"]["target_minutes_per_day"] = 15.0 / 60.0
        anchors = [
            candidate("early", "2026-11-01T08:00:00-05:00"),
            candidate("talk", "2026-11-01T09:00:00-05:00", 6.0),
            candidate("view", "2026-11-01T10:00:00-05:00", 12.0),
            candidate("late", "2026-11-01T11:00:00-05:00", 18.0),
        ]
        anchors[0].score = 0.1
        anchors[1].score = 1.0
        anchors[2].score = 0.2
        anchors[2].roles = ["scenery"]
        anchors[2].speech_ratio = 0.0
        anchors[2].visual_quality = 0.95
        anchors[3].score = 0.1
        plan = local_plan(self.config, "여정과 풍경", anchors, "hash")
        self.assertEqual(
            [item.candidate_id for item in plan.episodes[0].segments],
            ["early", "view", "late"],
        )

    def test_chronology_uses_absolute_time_across_dst_fold(self) -> None:
        fold_candidates = [
            candidate("c1", "2026-11-01T01:15:00-05:00"),
            candidate("c2", "2026-11-01T01:30:00-04:00", 6.0),
        ]
        with self.assertRaisesRegex(VideoSummaryError, "시간순"):
            validate_and_normalize_plan(
                self.valid_payload(), self.config, "x", fold_candidates, "hash", "file"
            )

    def test_prompt_semantics_produce_role_weights(self) -> None:
        weights = _prompt_role_weights("음식과 재미있는 대화를 중심으로")
        self.assertIn("food", weights)
        self.assertIn("fun", weights)
        self.assertIn("dialogue", weights)

    def test_external_candidate_payload_only_contains_bounded_excerpt(self) -> None:
        item = candidate("private", "2026-11-01T10:00:00-05:00")
        item.transcript = "민감한 대화 " * 80
        payload = _planner_candidates_payload([item], "hash")
        exported = payload["candidates"][0]
        self.assertNotIn("transcript", exported)
        self.assertLessEqual(len(exported["transcript_excerpt"]), 240)

    def test_plan_rejects_overlapping_ranges_from_the_same_clip(self) -> None:
        first, second = self.candidates
        second.clip_id = first.clip_id
        second.captured_at = "2026-11-01T08:00:03-05:00"
        second.start = 3.0
        second.end = 8.0
        with self.assertRaisesRegex(VideoSummaryError, "선택 구간이 겹칩니다"):
            validate_and_normalize_plan(
                self.valid_payload(), self.config, "x", self.candidates, "hash", "file"
            )

    def test_local_plan_never_selects_overlapping_ranges_from_the_same_clip(self) -> None:
        candidates = [
            candidate("a", "2026-11-01T08:00:00-05:00", 0.0),
            candidate("b", "2026-11-01T08:00:03-05:00", 3.0),
            candidate("c", "2026-11-01T08:00:08-05:00", 8.0),
        ]
        for item in candidates:
            item.clip_id = "shared"
        candidates[0].score = 1.0
        candidates[1].score = 0.9
        candidates[2].score = 0.8

        plan = local_plan(self.config, "자연스러운 시간순 여행", candidates, "hash")

        self.assertEqual([segment.candidate_id for segment in plan.episodes[0].segments], ["a", "c"])


if __name__ == "__main__":
    unittest.main()
