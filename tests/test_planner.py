from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from video_summary.models import Candidate
from video_summary.planner import (
    _planner_candidates_payload,
    _prompt_role_weights,
    _sample_contact_sheets,
    _select_day_candidates,
    build_planner_request,
    local_plan,
    plan_project,
    planner_schema,
    validate_and_normalize_plan,
)
from video_summary.project import DEFAULT_CONFIG, ProjectPaths
from video_summary.utils import VideoSummaryError, write_json


def candidate(
    candidate_id: str,
    captured_at: str,
    start: float = 0.0,
    *,
    required_event_ids: list[str] | None = None,
) -> Candidate:
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
        required_event_ids=list(required_event_ids or []),
    )


class MealTaggedCandidate(Candidate):
    """Compatibility fixture while meal provenance is carried by Candidate tags."""


def meal_candidate(
    candidate_id: str,
    captured_at: str,
    event_ids: list[str],
    start: float = 0.0,
) -> Candidate:
    item = MealTaggedCandidate.from_dict(
        candidate(candidate_id, captured_at, start).to_dict()
    )
    item.required_meal_event_ids = list(event_ids)
    return item


def meal_context_candidate(
    candidate_id: str,
    captured_at: str,
    context_ids: list[str],
    start: float = 0.0,
) -> Candidate:
    item = candidate(candidate_id, captured_at, start)
    item.required_meal_context_ids = list(context_ids)
    return item


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

    def test_plan_cache_tracks_candidate_policy_versions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = ProjectPaths(Path(temporary), "project")
            paths.ensure()

            def write_candidate_policy(version: int) -> None:
                write_json(
                    paths.candidates,
                    {
                        "version": 2,
                        "candidate_set_hash": "unchanged-candidates",
                        "policy_versions": {"journey_transition": version},
                    },
                )

            write_candidate_policy(1)
            with (
                patch("video_summary.planner.load_candidates", return_value=self.candidates),
                patch("video_summary.planner.local_plan", wraps=local_plan) as planned,
            ):
                plan_project(paths, self.config)
                write_candidate_policy(2)
                plan_project(paths, self.config)
                plan_project(paths, self.config)

            self.assertEqual(planned.call_count, 2)

    def test_plan_cache_tracks_the_story_soft_maximum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = ProjectPaths(Path(temporary), "project")
            paths.ensure()
            write_json(
                paths.candidates,
                {
                    "version": 2,
                    "candidate_set_hash": "unchanged-candidates",
                    "policy_versions": {},
                },
            )
            with (
                patch("video_summary.planner.load_candidates", return_value=self.candidates),
                patch("video_summary.planner.local_plan", wraps=local_plan) as planned,
            ):
                plan_project(paths, self.config)
                self.config["editing"]["soft_max_minutes_per_day"] = 9.0
                plan_project(paths, self.config)
                plan_project(paths, self.config)

            self.assertEqual(planned.call_count, 2)

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
        self.config["editing"]["soft_max_minutes_per_day"] = 10.0 / 60.0
        anchors = [
            candidate("early", "2026-11-01T08:00:00-05:00"),
            candidate("middle", "2026-11-01T09:00:00-05:00", 6.0),
            candidate("late", "2026-11-01T10:00:00-05:00", 12.0),
        ]
        anchors[0].score = 0.05
        anchors[1].score = 1.0
        anchors[2].roles = ["journey", "closer"]
        anchors[2].score = 0.55
        plan = local_plan(self.config, "여정", anchors, "hash")
        self.assertEqual(
            [item.candidate_id for item in plan.episodes[0].segments],
            ["early", "late"],
        )

    def test_local_plan_keeps_a_strong_silent_visual_anchor(self) -> None:
        self.config["editing"]["soft_max_minutes_per_day"] = 15.0 / 60.0
        anchors = [
            candidate("early", "2026-11-01T08:00:00-05:00"),
            candidate("talk", "2026-11-01T09:00:00-05:00", 6.0),
            candidate("view", "2026-11-01T10:00:00-05:00", 12.0),
            candidate("late", "2026-11-01T11:00:00-05:00", 18.0),
        ]
        anchors[0].score = 0.1
        anchors[1].score = 0.2
        anchors[1].roles = ["dialogue"]
        anchors[1].visual_quality = 0.4
        anchors[2].score = 0.2
        anchors[2].roles = ["scenery"]
        anchors[2].speech_ratio = 0.0
        anchors[2].visual_quality = 0.95
        anchors[3].roles = ["journey", "closer"]
        anchors[3].score = 0.55
        plan = local_plan(self.config, "여정과 풍경", anchors, "hash")
        self.assertEqual(
            [item.candidate_id for item in plan.episodes[0].segments],
            ["early", "view", "late"],
        )

    def test_local_plan_pins_all_detected_interviews_past_a_tight_soft_maximum(self) -> None:
        self.config["editing"]["soft_max_minutes_per_day"] = 0.1
        items = [
            candidate("early", "2026-11-01T08:00:00-05:00"),
            candidate(
                "interview_a",
                "2026-11-01T09:00:00-05:00",
                required_event_ids=["family_interview_1"],
            ),
            candidate(
                "interview_b",
                "2026-11-01T10:00:00-05:00",
                required_event_ids=["family_interview_2"],
            ),
            candidate("late", "2026-11-01T11:00:00-05:00"),
        ]
        for item in items[1:3]:
            item.roles = ["interview", "dialogue"]

        plan = local_plan(self.config, "가족 인터뷰와 여행 흐름", items, "hash")
        segments = plan.episodes[0].segments

        self.assertEqual(
            [item.candidate_id for item in segments],
            ["early", "interview_a", "interview_b"],
        )
        self.assertEqual([item.role for item in segments], ["hook", "interview", "interview"])
        self.assertTrue(all(item.speed == 1.0 for item in segments[1:]))
        self.assertTrue(all(item.caption is None for item in segments[1:]))
        self.assertTrue(all(item.location is None for item in segments[1:]))

    def test_local_plan_pins_transition_waypoints_past_a_tight_soft_maximum(self) -> None:
        self.config["editing"]["soft_max_minutes_per_day"] = 0.1
        items = [
            candidate("early", "2026-11-01T08:00:00-05:00"),
            candidate("pickup", "2026-11-01T09:00:00-05:00"),
            candidate("airport", "2026-11-01T10:00:00-05:00"),
            candidate("late", "2026-11-01T11:00:00-05:00"),
        ]
        items[1].roles = ["transition"]
        items[2].roles = ["transition"]

        plan = local_plan(self.config, "픽업과 공항 이동 거점을 보존해줘", items, "hash")
        segments = plan.episodes[0].segments

        self.assertEqual(
            [item.candidate_id for item in segments],
            ["early", "pickup", "airport"],
        )
        self.assertEqual([item.role for item in segments], ["hook", "transition", "transition"])
        self.assertTrue(all(item.speed == 1.0 for item in segments[1:]))
        self.assertTrue(all("이동 거점" in item.reason for item in segments[1:]))
        self.assertIn("주요 이동 거점", plan.episodes[0].summary)

    def test_local_plan_selects_one_best_meal_option_past_a_tight_soft_maximum(self) -> None:
        self.config["editing"]["soft_max_minutes_per_day"] = 0.1
        items = [
            candidate("early", "2026-11-01T08:00:00-05:00"),
            meal_candidate("breakfast_low", "2026-11-01T09:00:00-05:00", ["meal_1"]),
            meal_candidate("breakfast_best", "2026-11-01T10:00:00-05:00", ["meal_1"]),
            meal_candidate("dinner", "2026-11-01T11:00:00-05:00", ["meal_2"]),
            candidate("late", "2026-11-01T12:00:00-05:00"),
        ]
        items[1].score = 0.2
        items[2].score = 0.95
        items[3].score = 0.7

        plan = local_plan(self.config, "모든 식사 경험을 보존해줘", items, "hash")
        segments = plan.episodes[0].segments

        self.assertEqual(
            [item.candidate_id for item in segments],
            ["early", "breakfast_best", "dinner"],
        )
        self.assertEqual([item.role for item in segments], ["hook", "food", "food"])
        self.assertTrue(all(item.speed == 1.0 for item in segments[1:]))
        self.assertTrue(all("식사 경험" in item.reason for item in segments[1:]))
        self.assertIn("먹거리", plan.episodes[0].summary)

    def test_local_plan_pins_detected_meal_setup_body_and_closure(self) -> None:
        self.config["editing"]["soft_max_minutes_per_day"] = 0.1
        items = [
            candidate("early", "2026-11-01T08:00:00-05:00"),
            meal_context_candidate(
                "setup_low",
                "2026-11-01T09:00:00-05:00",
                ["meal_1:setup"],
            ),
            meal_context_candidate(
                "setup_best",
                "2026-11-01T09:30:00-05:00",
                ["meal_1:setup"],
            ),
            meal_candidate("body", "2026-11-01T10:00:00-05:00", ["meal_1"]),
            meal_context_candidate(
                "closure",
                "2026-11-01T11:00:00-05:00",
                ["meal_1:closure"],
            ),
            candidate("late", "2026-11-01T12:00:00-05:00"),
        ]
        items[1].score = 0.2
        items[2].score = 0.95

        plan = local_plan(self.config, "식사 서사를 자연스럽게 보존해줘", items, "hash")
        segments = plan.episodes[0].segments

        self.assertEqual(
            [item.candidate_id for item in segments],
            ["early", "setup_best", "body", "closure"],
        )
        self.assertEqual([item.role for item in segments], ["hook", "food", "food", "food"])
        self.assertTrue(all(item.speed == 1.0 for item in segments[1:]))
        self.assertTrue(all("식사 경험" in item.reason for item in segments[1:]))

    def test_local_meal_group_reuses_an_already_mandatory_transition_option(self) -> None:
        self.config["editing"]["soft_max_minutes_per_day"] = 0.1
        transition = meal_candidate(
            "restaurant_arrival",
            "2026-11-01T09:00:00-05:00",
            ["meal_1"],
        )
        transition.roles = ["transition", "journey", "food"]
        transition.score = 0.1
        alternative = meal_candidate(
            "restaurant_closeup",
            "2026-11-01T10:00:00-05:00",
            ["meal_1"],
        )
        alternative.score = 1.0
        items = [
            candidate("early", "2026-11-01T08:00:00-05:00"),
            transition,
            alternative,
        ]

        plan = local_plan(self.config, "식사와 이동을 보존해줘", items, "hash")

        self.assertEqual(
            [item.candidate_id for item in plan.episodes[0].segments],
            ["early", "restaurant_arrival"],
        )
        self.assertEqual(plan.episodes[0].segments[-1].role, "closing")

    def test_transition_waypoint_wins_deduplication_over_a_higher_score_overlap(self) -> None:
        items = [
            candidate("early", "2026-11-01T08:00:00-05:00"),
            candidate("waypoint", "2026-11-01T09:00:00-05:00", 0.0),
            candidate("flashy_overlap", "2026-11-01T09:00:00-05:00", 1.0),
        ]
        items[1].clip_id = items[2].clip_id = "shared"
        items[1].roles = ["transition"]
        items[1].score = 0.01
        items[2].score = 1.0

        selected = _select_day_candidates(items, 5.0)

        self.assertEqual([item.candidate_id for item in selected], ["early", "waypoint"])

    def test_local_plan_is_unchanged_when_no_interview_is_detected(self) -> None:
        plan = local_plan(self.config, "여행 흐름", self.candidates, "hash")

        self.assertEqual(
            [item.candidate_id for item in plan.episodes[0].segments],
            ["c1", "c2"],
        )
        self.assertNotIn("interview", {item.role for item in plan.episodes[0].segments})

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
        weights = _prompt_role_weights("음식과 재미있는 대화, 공항 픽업 이동 거점을 중심으로")
        self.assertIn("food", weights)
        self.assertIn("fun", weights)
        self.assertIn("dialogue", weights)
        self.assertIn("transition", weights)

    def test_external_candidate_payload_only_contains_bounded_excerpt(self) -> None:
        item = candidate("private", "2026-11-01T10:00:00-05:00")
        item.transcript = "민감한 대화 " * 80
        payload = _planner_candidates_payload([item], "hash")
        exported = payload["candidates"][0]
        self.assertEqual(payload["version"], 2)
        self.assertEqual(payload["story_selection_contract"]["strategy"], "event_first")
        self.assertFalse(payload["story_selection_contract"]["fill_quota"])
        self.assertEqual(
            payload["story_selection_contract"]["soft_ceiling_scope"],
            "total_day_runtime",
        )
        self.assertTrue(payload["story_event_groups"])
        self.assertNotIn("transcript", exported)
        self.assertLessEqual(len(exported["transcript_excerpt"]), 240)
        self.assertEqual(exported["required_event_ids"], [])

    def test_external_candidate_payload_and_request_expose_the_interview_contract(self) -> None:
        interview = candidate(
            "interview",
            "2026-11-01T10:00:00-05:00",
            required_event_ids=["family_interview_1", "family_interview_2"],
        )
        interview.roles = ["interview", "dialogue"]

        payload = _planner_candidates_payload([self.candidates[0], interview], "hash")
        request = build_planner_request(
            ProjectPaths(Path("/tmp"), "project"),
            self.config,
            "가족 인터뷰를 보존해줘",
            [self.candidates[0], interview],
            "hash",
        )

        self.assertEqual(
            payload["candidates"][1]["required_event_ids"],
            ["family_interview_1", "family_interview_2"],
        )
        self.assertIn("mandatory family interview candidates: interview", request)
        self.assertIn("family_interview_1,family_interview_2", request)
        self.assertIn("목표 시간을 넘더라도", request)
        self.assertIn("speed=1.0, location=null, caption=null, role=interview", request)

    def test_external_request_exposes_mandatory_transition_ids_and_contract(self) -> None:
        self.candidates[1].roles = ["transition", "journey"]

        payload = _planner_candidates_payload(self.candidates, "hash")
        request = build_planner_request(
            ProjectPaths(Path("/tmp"), "project"),
            self.config,
            "이동 거점을 보존해줘",
            self.candidates,
            "hash",
        )

        self.assertIn("transition", payload["candidates"][1]["roles"])
        self.assertIn("mandatory transition waypoint candidates: c2", request)
        self.assertIn("목표 시간과 soft maximum을 넘더라도", request)
        self.assertIn("speed=1.0, role=transition", request)
        self.assertIn("role=journey만 있는 후보는 필수가 아닙니다", request)

    def test_external_payload_and_request_expose_meal_one_of_groups(self) -> None:
        options = [
            meal_context_candidate(
                "setup_a",
                "2026-11-01T08:30:00-05:00",
                ["meal_1:setup"],
            ),
            meal_context_candidate(
                "setup_b",
                "2026-11-01T08:45:00-05:00",
                ["meal_1:setup"],
            ),
            meal_candidate("meal_a", "2026-11-01T09:00:00-05:00", ["meal_1"]),
            meal_candidate("meal_b", "2026-11-01T10:00:00-05:00", ["meal_1", "meal_2"]),
            meal_context_candidate(
                "closure",
                "2026-11-01T11:00:00-05:00",
                ["meal_1:closure"],
            ),
        ]

        payload = _planner_candidates_payload([self.candidates[0], *options], "hash")
        request = build_planner_request(
            ProjectPaths(Path("/tmp"), "project"),
            self.config,
            "모든 식사를 보존해줘",
            [self.candidates[0], *options],
            "hash",
        )

        self.assertEqual(
            payload["meal_event_selection_contract"]["selection_mode"],
            "one_of",
        )
        self.assertEqual(
            payload["meal_event_option_groups"],
            [
                {
                    "event_id": "meal_1",
                    "day_key": "2026-11-01",
                    "selection_mode": "one_of",
                    "one_of_candidate_ids": ["meal_a", "meal_b"],
                },
                {
                    "event_id": "meal_2",
                    "day_key": "2026-11-01",
                    "selection_mode": "one_of",
                    "one_of_candidate_ids": ["meal_b"],
                },
            ],
        )
        self.assertEqual(
            next(
                item["required_meal_event_ids"]
                for item in payload["candidates"]
                if item["candidate_id"] == "meal_b"
            ),
            ["meal_1", "meal_2"],
        )
        self.assertEqual(
            payload["meal_context_option_groups"],
            [
                {
                    "context_id": "meal_1:setup",
                    "stage": "setup",
                    "day_key": "2026-11-01",
                    "selection_mode": "one_of",
                    "one_of_candidate_ids": ["setup_a", "setup_b"],
                },
                {
                    "context_id": "meal_1:closure",
                    "stage": "closure",
                    "day_key": "2026-11-01",
                    "selection_mode": "one_of",
                    "one_of_candidate_ids": ["closure"],
                },
            ],
        )
        self.assertIn("mandatory meal event meal_1 one_of candidates: meal_a, meal_b", request)
        self.assertIn(
            "mandatory meal narrative meal_1:setup one_of candidates: setup_a, setup_b",
            request,
        )
        self.assertIn(
            "mandatory meal narrative meal_1:closure one_of candidates: closure",
            request,
        )
        self.assertIn("각 이벤트의 one_of 후보 중 최소 1개", request)
        self.assertIn("speed=1.0, role=food", request)
        self.assertIn(
            "required_meal_event_ids와 required_meal_context_ids가 모두 비어 있는 후보는 필수가 아닙니다",
            request,
        )

    def test_planner_schema_const_fields_also_declare_their_json_type(self) -> None:
        schema = planner_schema()
        self.assertEqual(schema["properties"]["version"], {"type": "integer", "const": 1})
        segment_schema = schema["properties"]["episodes"]["items"]["properties"]["segments"]["items"]
        self.assertEqual(set(segment_schema["required"]), set(segment_schema["properties"]))
        self.assertIn("interview", segment_schema["properties"]["role"]["enum"])

    def test_contact_sheet_sampling_spans_the_full_trip(self) -> None:
        sheets = [Path(f"sheet-{index:03d}.jpg") for index in range(1, 31)]

        selected = _sample_contact_sheets(sheets)

        self.assertEqual(len(selected), 20)
        self.assertEqual(selected[0], sheets[0])
        self.assertEqual(selected[-1], sheets[-1])
        self.assertEqual(selected, sorted(selected))
        self.assertEqual(len(set(selected)), len(selected))
        self.assertGreater(selected[10], sheets[10])

    def test_contact_sheet_sampling_keeps_a_small_middle_day(self) -> None:
        sheets = [Path(f"sheet-{index:03d}.jpg") for index in range(1, 31)]
        sheet_day_keys = (
            [("2026-08-19",)] * 2
            + [("2026-08-20",)]
            + [("2026-08-21",)] * 27
        )

        selected = _sample_contact_sheets(sheets, sheet_day_keys)

        self.assertEqual(len(selected), 20)
        self.assertIn(sheets[2], selected)
        self.assertEqual(selected[0], sheets[0])
        self.assertEqual(selected[-1], sheets[-1])

    def test_contact_sheet_sampling_preserves_small_inputs(self) -> None:
        sheets = [Path("sheet-001.jpg"), Path("sheet-002.jpg")]
        self.assertEqual(_sample_contact_sheets(sheets), sheets)

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
        candidates[2].roles = ["journey", "closer"]

        plan = local_plan(self.config, "자연스러운 시간순 여행", candidates, "hash")

        self.assertEqual([segment.candidate_id for segment in plan.episodes[0].segments], ["a", "c"])

    def test_local_selection_prefers_a_contiguous_run_over_an_isolated_score_edge(self) -> None:
        items = [
            candidate("early", "2026-11-01T08:00:00-05:00", 0.0),
            candidate("run_a", "2026-11-01T09:00:00-05:00", 0.0),
            candidate("run_b", "2026-11-01T09:00:05-05:00", 5.0),
            candidate("isolated", "2026-11-01T10:00:00-05:00", 0.0),
            candidate("late", "2026-11-01T11:00:00-05:00", 0.0),
        ]
        items[1].clip_id = items[2].clip_id = "continuous"
        items[0].score = items[4].score = 0.05
        items[1].score = 0.79
        items[2].score = 0.74
        items[3].score = 0.82
        items[4].roles = ["journey", "closer"]
        items[4].score = 0.55

        selected = _select_day_candidates(items, 20.0)

        self.assertEqual([item.candidate_id for item in selected], ["early", "run_a", "run_b", "late"])

    def test_event_first_stops_after_coverage_without_filling_the_soft_maximum(self) -> None:
        items = [
            candidate("early", "2026-11-01T08:00:00-05:00"),
            candidate("strong", "2026-11-01T08:02:00-05:00"),
            candidate("near_duplicate", "2026-11-01T08:04:00-05:00"),
            candidate("weak_fragment", "2026-11-01T08:06:00-05:00"),
            candidate("closing", "2026-11-01T08:08:00-05:00"),
        ]
        for item in items:
            item.roles = ["journey"]
        items[0].score = 0.1
        items[1].score = 0.95
        items[2].score = 0.90
        items[3].score = 0.2
        items[3].visual_quality = 0.3
        items[4].roles = ["journey", "closer"]
        items[4].score = 0.55

        selected = _select_day_candidates(items, 600.0)

        self.assertEqual(
            [item.candidate_id for item in selected],
            ["early", "strong", "closing"],
        )
        self.assertEqual(sum(item.duration for item in selected), 15.0)

    def test_event_first_balances_dense_events_across_the_full_day(self) -> None:
        items: list[Candidate] = []
        for index in range(13):
            total_minutes = 8 * 60 + index * 31
            hour, minute = divmod(total_minutes, 60)
            item = candidate(
                f"event_{index:02d}",
                f"2026-11-01T{hour:02d}:{minute:02d}:00-05:00",
            )
            item.end = 50.0
            item.roles = ["scenery"]
            item.score = 0.95
            item.speech_ratio = 0.0
            item.visual_quality = 0.95
            items.append(item)
        items[-1].roles.append("closer")

        selected = _select_day_candidates(items, 300.0)
        selected_indexes = {
            int(item.candidate_id.rsplit("_", 1)[-1])
            for item in selected
        }

        self.assertLessEqual(sum(item.duration for item in selected), 300.0)
        self.assertIn(0, selected_indexes)
        self.assertIn(12, selected_indexes)
        self.assertTrue(selected_indexes & set(range(1, 4)))
        self.assertTrue(selected_indexes & set(range(4, 9)))
        self.assertTrue(selected_indexes & set(range(9, 12)))

    def test_soft_maximum_includes_mandatory_runtime_before_story_events(self) -> None:
        items = [
            candidate("early", "2026-11-01T08:00:00-05:00"),
            candidate(
                "interview",
                "2026-11-01T09:00:00-05:00",
                required_event_ids=["family_interview_1"],
            ),
            candidate("event_a", "2026-11-01T10:00:00-05:00"),
            candidate("event_b", "2026-11-01T11:00:00-05:00"),
        ]
        items[0].end = 5.0
        items[0].score = 0.1
        items[1].roles = ["interview", "dialogue"]
        items[1].end = 195.0
        for item in items[2:]:
            item.roles = ["scenery"]
            item.score = 0.95
            item.speech_ratio = 0.0
            item.visual_quality = 0.95
            item.end = 200.0

        selected = _select_day_candidates(items, 600.0)

        self.assertEqual(
            [item.candidate_id for item in selected],
            ["early", "interview", "event_a", "event_b"],
        )
        self.assertEqual(sum(item.duration for item in selected), 600.0)

    def test_mandatory_runtime_over_soft_maximum_blocks_optional_events(self) -> None:
        interview = candidate(
            "interview",
            "2026-11-01T08:00:00-05:00",
            required_event_ids=["family_interview_1"],
        )
        interview.roles = ["interview", "dialogue"]
        interview.end = 650.0
        optional = candidate("optional", "2026-11-01T09:00:00-05:00")
        optional.roles = ["scenery"]
        optional.score = optional.visual_quality = 0.95
        optional.speech_ratio = 0.0

        selected = _select_day_candidates([interview, optional], 600.0)

        self.assertEqual([item.candidate_id for item in selected], ["interview"])

    def test_legacy_target_changes_metadata_but_not_event_selection(self) -> None:
        items = [
            candidate("early", "2026-11-01T08:00:00-05:00"),
            candidate("event", "2026-11-01T09:00:00-05:00"),
        ]
        five_minute_config = copy.deepcopy(self.config)
        six_minute_config = copy.deepcopy(self.config)
        five_minute_config["editing"]["target_minutes_per_day"] = 5.0
        six_minute_config["editing"]["target_minutes_per_day"] = 6.0

        five_minute_plan = local_plan(five_minute_config, "여행", items, "hash")
        six_minute_plan = local_plan(six_minute_config, "여행", items, "hash")

        self.assertEqual(
            [item.candidate_id for item in five_minute_plan.episodes[0].segments],
            [item.candidate_id for item in six_minute_plan.episodes[0].segments],
        )
        self.assertEqual(five_minute_plan.episodes[0].target_duration, 300.0)
        self.assertEqual(six_minute_plan.episodes[0].target_duration, 360.0)

    def test_external_request_declares_event_first_and_a_separate_soft_ceiling(self) -> None:
        request = build_planner_request(
            ProjectPaths(Path("/tmp"), "project"),
            self.config,
            "짧고 자연스럽게",
            self.candidates,
            "hash",
        )

        self.assertIn("semantic event group", request)
        self.assertIn("채워야 하는 할당량이 아닙니다", request)
        self.assertIn("약한 fragment나 near-duplicate", request)
        self.assertIn("usable candidate total: 10.0s", request)
        self.assertIn("soft maximum: 600.0s", request)
        self.assertIn("Legacy target_duration per day: 0.2 minutes", request)
        self.assertIn("Soft maximum per day: 10.0 minutes", request)
        self.assertIn("DAY 전체 runtime은 soft maximum 600.0초 이내", request)
        self.assertIn("target_duration 필드는 호환성을 위해 12.0으로 유지", request)
        self.assertIn("location/caption은 null, speed는 1.0", request)

    def test_broad_journey_role_alone_is_not_mandatory(self) -> None:
        self.candidates[1].roles = ["journey"]
        payload = self.valid_payload()
        payload["episodes"][0]["segments"] = [payload["episodes"][0]["segments"][0]]

        plan = validate_and_normalize_plan(
            payload, self.config, "x", self.candidates, "hash", "file"
        )

        self.assertEqual(
            [item.candidate_id for item in plan.episodes[0].segments],
            ["c1"],
        )

    def test_broad_food_role_without_a_meal_event_tag_is_not_mandatory(self) -> None:
        self.candidates[1].roles = ["food"]
        payload = self.valid_payload()
        payload["episodes"][0]["segments"] = [payload["episodes"][0]["segments"][0]]

        plan = validate_and_normalize_plan(
            payload, self.config, "x", self.candidates, "hash", "file"
        )

        self.assertEqual(
            [item.candidate_id for item in plan.episodes[0].segments],
            ["c1"],
        )

    def test_validator_uses_soft_maximum_instead_of_the_legacy_target(self) -> None:
        self.config["editing"]["soft_max_minutes_per_day"] = 3.0
        self.candidates[1].end = self.candidates[1].start + 150.0

        plan = validate_and_normalize_plan(
            self.valid_payload(),
            self.config,
            "x",
            self.candidates,
            "hash",
            "file",
        )

        self.assertEqual(plan.episodes[0].target_duration, 12.0)

    def test_validator_rejects_ordinary_runtime_past_remaining_soft_maximum(self) -> None:
        self.config["editing"]["soft_max_minutes_per_day"] = 0.1

        with self.assertRaisesRegex(VideoSummaryError, "soft maximum 6.0초"):
            validate_and_normalize_plan(
                self.valid_payload(),
                self.config,
                "x",
                self.candidates,
                "hash",
                "file",
            )

    def test_meaningful_closing_can_extend_the_soft_maximum(self) -> None:
        self.config["editing"]["soft_max_minutes_per_day"] = 0.1
        self.candidates[1].roles = ["journey", "closer"]
        self.candidates[1].score = 0.8
        payload = self.valid_payload()
        payload["episodes"][0]["segments"][1]["role"] = "closing"

        plan = validate_and_normalize_plan(
            payload,
            self.config,
            "x",
            self.candidates,
            "hash",
            "file",
        )

        self.assertEqual(
            [item.candidate_id for item in plan.episodes[0].segments],
            ["c1", "c2"],
        )

    def test_external_planners_require_one_option_from_each_meal_group(self) -> None:
        candidates = [
            self.candidates[0],
            meal_candidate("meal_a", "2026-11-01T09:00:00-05:00", ["meal_1"]),
            meal_candidate("meal_b", "2026-11-01T10:00:00-05:00", ["meal_1"]),
        ]
        payload = self.valid_payload()
        payload["episodes"][0]["segments"] = [payload["episodes"][0]["segments"][0]]

        for planner_name in ("file", "codex", "claude"):
            with self.subTest(planner=planner_name):
                with self.assertRaisesRegex(
                    VideoSummaryError,
                    "필수 식사 이벤트 meal_1.*one_of.*meal_a.*meal_b",
                ):
                    validate_and_normalize_plan(
                        payload,
                        self.config,
                        "x",
                        candidates,
                        "hash",
                        planner_name,
                    )

    def test_external_planners_require_detected_meal_setup_and_closure_groups(self) -> None:
        candidates = [
            self.candidates[0],
            meal_context_candidate(
                "setup",
                "2026-11-01T09:00:00-05:00",
                ["meal_1:setup"],
            ),
            meal_candidate("body", "2026-11-01T10:00:00-05:00", ["meal_1"]),
            meal_context_candidate(
                "closure",
                "2026-11-01T11:00:00-05:00",
                ["meal_1:closure"],
            ),
        ]
        missing_cases = {
            "meal_1:setup": ["c1", "body", "closure"],
            "meal_1:closure": ["c1", "setup", "body"],
        }

        for planner_name in ("file", "codex", "claude"):
            for context_id, selected_ids in missing_cases.items():
                with self.subTest(planner=planner_name, missing=context_id):
                    payload = self.valid_payload()
                    payload["episodes"][0]["segments"] = [
                        {
                            "candidate_id": candidate_id,
                            "role": "fun" if candidate_id == "c1" else "food",
                            "reason": "식사 서사",
                            "speed": 1.0,
                        }
                        for candidate_id in selected_ids
                    ]
                    with self.assertRaisesRegex(
                        VideoSummaryError,
                        f"필수 식사 서사 {context_id}.*one_of",
                    ):
                        validate_and_normalize_plan(
                            payload,
                            self.config,
                            "x",
                            candidates,
                            "hash",
                            planner_name,
                        )

    def test_meal_context_candidates_require_normal_speed_and_food_role(self) -> None:
        context = meal_context_candidate(
            "setup",
            "2026-11-01T09:00:00-05:00",
            ["meal_1:setup"],
        )
        candidates = [self.candidates[0], context]

        for planner_name in ("file", "codex", "claude"):
            for field, value, message in (
                ("speed", 1.25, "speed=1.0"),
                ("role", "moment", "role=food"),
            ):
                with self.subTest(planner=planner_name, violation=field):
                    payload = self.valid_payload()
                    payload["episodes"][0]["segments"] = [
                        payload["episodes"][0]["segments"][0],
                        {
                            "candidate_id": "setup",
                            "role": "food",
                            "reason": "식사 준비",
                            "speed": 1.0,
                        },
                    ]
                    payload["episodes"][0]["segments"][-1][field] = value
                    with self.assertRaisesRegex(VideoSummaryError, message):
                        validate_and_normalize_plan(
                            payload,
                            self.config,
                            "x",
                            candidates,
                            "hash",
                            planner_name,
                        )

    def test_external_planners_accept_one_meal_option_and_reject_its_speed_or_role(self) -> None:
        candidates = [
            self.candidates[0],
            meal_candidate("meal_a", "2026-11-01T09:00:00-05:00", ["meal_1"]),
            meal_candidate("meal_b", "2026-11-01T10:00:00-05:00", ["meal_1"]),
        ]

        for planner_name in ("file", "codex", "claude"):
            with self.subTest(planner=planner_name, contract="one_of"):
                payload = self.valid_payload()
                payload["episodes"][0]["segments"] = [
                    payload["episodes"][0]["segments"][0],
                    {
                        "candidate_id": "meal_a",
                        "role": "food",
                        "reason": "대표 식사",
                        "speed": 1.0,
                    },
                ]
                plan = validate_and_normalize_plan(
                    payload,
                    self.config,
                    "x",
                    candidates,
                    "hash",
                    planner_name,
                )
                self.assertEqual(plan.episodes[0].segments[-1].candidate_id, "meal_a")

            with self.subTest(planner=planner_name, violation="speed"):
                payload["episodes"][0]["segments"][-1]["speed"] = 1.25
                with self.assertRaisesRegex(
                    VideoSummaryError,
                    "필수 식사 이벤트 선택 후보.*speed=1.0",
                ):
                    validate_and_normalize_plan(
                        payload,
                        self.config,
                        "x",
                        candidates,
                        "hash",
                        planner_name,
                    )

            with self.subTest(planner=planner_name, violation="role"):
                payload["episodes"][0]["segments"][-1].update(
                    {"speed": 1.0, "role": "moment"}
                )
                with self.assertRaisesRegex(
                    VideoSummaryError,
                    "필수 식사 이벤트 선택 후보.*role=food",
                ):
                    validate_and_normalize_plan(
                        payload,
                        self.config,
                        "x",
                        candidates,
                        "hash",
                        planner_name,
                    )

    def test_meal_option_contract_defers_to_interview_and_transition_roles(self) -> None:
        interview = meal_candidate(
            "interview_meal",
            "2026-11-01T09:00:00-05:00",
            ["meal_1"],
        )
        interview.required_event_ids = ["family_interview_1"]
        interview.required_meal_context_ids = ["meal_1:setup"]
        interview.roles = ["interview", "food"]
        transition = meal_candidate(
            "transition_meal",
            "2026-11-01T10:00:00-05:00",
            ["meal_2"],
        )
        transition.roles = ["transition", "journey", "food"]
        transition.required_meal_context_ids = ["meal_2:closure"]
        candidates = [self.candidates[0], interview, transition]
        payload = self.valid_payload()
        payload["episodes"][0]["segments"] = [
            payload["episodes"][0]["segments"][0],
            {
                "candidate_id": "interview_meal",
                "role": "interview",
                "reason": "식사 소감 인터뷰",
                "location": None,
                "caption": None,
                "speed": 1.0,
            },
            {
                "candidate_id": "transition_meal",
                "role": "closing",
                "reason": "식당 도착",
                "speed": 1.0,
            },
        ]

        plan = validate_and_normalize_plan(
            payload, self.config, "x", candidates, "hash", "file"
        )

        self.assertEqual(
            [item.role for item in plan.episodes[0].segments],
            ["fun", "interview", "closing"],
        )

    def test_meal_option_allows_hook_and_closing_only_at_episode_boundaries(self) -> None:
        first_meal = meal_candidate(
            "c1",
            "2026-11-01T08:00:00-05:00",
            ["meal_1"],
        )
        payload = self.valid_payload()
        payload["episodes"][0]["segments"][0]["role"] = "hook"
        first_boundary = validate_and_normalize_plan(
            payload,
            self.config,
            "x",
            [first_meal, self.candidates[1]],
            "hash",
            "file",
        )
        self.assertEqual(first_boundary.episodes[0].segments[0].role, "hook")

        last_meal = meal_candidate(
            "c2",
            "2026-11-01T09:00:00-05:00",
            ["meal_1"],
            6.0,
        )
        payload = self.valid_payload()
        payload["episodes"][0]["segments"][1]["role"] = "closing"
        last_boundary = validate_and_normalize_plan(
            payload,
            self.config,
            "x",
            [self.candidates[0], last_meal],
            "hash",
            "file",
        )
        self.assertEqual(last_boundary.episodes[0].segments[-1].role, "closing")

    def test_external_planners_reject_a_missing_transition_waypoint(self) -> None:
        self.candidates[1].roles = ["transition", "journey"]
        payload = self.valid_payload()
        payload["episodes"][0]["segments"] = [payload["episodes"][0]["segments"][0]]

        for planner_name in ("file", "codex", "claude"):
            with self.subTest(planner=planner_name):
                with self.assertRaisesRegex(VideoSummaryError, "필수 이동 거점 후보.*c2"):
                    validate_and_normalize_plan(
                        payload,
                        self.config,
                        "x",
                        self.candidates,
                        "hash",
                        planner_name,
                    )

    def test_external_planners_reject_transition_speed_or_role_changes(self) -> None:
        self.candidates[1].roles = ["transition", "journey"]

        for planner_name in ("file", "codex", "claude"):
            with self.subTest(planner=planner_name, violation="speed"):
                payload = self.valid_payload()
                payload["episodes"][0]["segments"][1].update(
                    {"role": "transition", "speed": 1.25}
                )
                with self.assertRaisesRegex(VideoSummaryError, "필수 이동 거점 후보.*speed=1.0"):
                    validate_and_normalize_plan(
                        payload,
                        self.config,
                        "x",
                        self.candidates,
                        "hash",
                        planner_name,
                    )

            with self.subTest(planner=planner_name, violation="role"):
                payload = self.valid_payload()
                payload["episodes"][0]["segments"][1].update(
                    {"role": "journey", "speed": 1.0}
                )
                with self.assertRaisesRegex(VideoSummaryError, "필수 이동 거점 후보.*role=transition"):
                    validate_and_normalize_plan(
                        payload,
                        self.config,
                        "x",
                        self.candidates,
                        "hash",
                        planner_name,
                    )

    def test_transition_waypoint_allows_only_boundary_role_exceptions(self) -> None:
        self.candidates[0].roles = ["transition"]
        payload = self.valid_payload()
        payload["episodes"][0]["segments"][0]["role"] = "hook"
        first_boundary = validate_and_normalize_plan(
            payload, self.config, "x", self.candidates, "hash", "file"
        )
        self.assertEqual(first_boundary.episodes[0].segments[0].role, "hook")

        self.candidates[0].roles = ["fun", "food"]
        self.candidates[1].roles = ["transition"]
        payload = self.valid_payload()
        payload["episodes"][0]["segments"][1]["role"] = "closing"
        last_boundary = validate_and_normalize_plan(
            payload, self.config, "x", self.candidates, "hash", "file"
        )
        self.assertEqual(last_boundary.episodes[0].segments[-1].role, "closing")

    def test_plan_rejects_a_missing_required_interview_candidate(self) -> None:
        self.candidates[1].required_event_ids = ["family_interview_1"]
        self.candidates[1].roles = ["interview", "dialogue"]
        payload = self.valid_payload()
        payload["episodes"][0]["segments"] = [payload["episodes"][0]["segments"][0]]

        with self.assertRaisesRegex(VideoSummaryError, "필수 가족 인터뷰 후보.*c2"):
            validate_and_normalize_plan(
                payload, self.config, "x", self.candidates, "hash", "file"
            )

    def test_plan_rejects_speed_or_caption_changes_to_a_required_interview(self) -> None:
        self.candidates[1].required_event_ids = ["family_interview_1"]
        self.candidates[1].roles = ["interview", "dialogue"]
        payload = self.valid_payload()
        payload["episodes"][0]["segments"][1].update(
            {"role": "interview", "speed": 1.25}
        )
        with self.assertRaisesRegex(VideoSummaryError, "speed=1.0"):
            validate_and_normalize_plan(
                payload, self.config, "x", self.candidates, "hash", "file"
            )

        payload["episodes"][0]["segments"][1].update(
            {"speed": 1.0, "caption": "요약된 소감"}
        )
        with self.assertRaisesRegex(VideoSummaryError, "caption은 null"):
            validate_and_normalize_plan(
                payload, self.config, "x", self.candidates, "hash", "file"
            )

        payload["episodes"][0]["segments"][1].update(
            {"caption": None, "location": "인터뷰 장소"}
        )
        with self.assertRaisesRegex(VideoSummaryError, "location은 null"):
            validate_and_normalize_plan(
                payload, self.config, "x", self.candidates, "hash", "file"
            )

    def test_required_interview_runtime_can_extend_the_soft_maximum(self) -> None:
        self.config["editing"]["soft_max_minutes_per_day"] = 0.1
        self.candidates[1].required_event_ids = ["family_interview_1"]
        self.candidates[1].roles = ["interview", "dialogue"]
        self.candidates[1].end = self.candidates[1].start + 200.0
        payload = self.valid_payload()
        payload["episodes"][0]["segments"][1].update(
            {"role": "interview", "speed": 1.0, "caption": None}
        )

        plan = validate_and_normalize_plan(
            payload, self.config, "x", self.candidates, "hash", "file"
        )

        self.assertEqual(
            [item.candidate_id for item in plan.episodes[0].segments],
            ["c1", "c2"],
        )


if __name__ == "__main__":
    unittest.main()
