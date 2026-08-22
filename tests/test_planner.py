from __future__ import annotations

import copy
import unittest
from pathlib import Path

from video_summary.models import Candidate
from video_summary.planner import (
    SHORT_DAY_KEEP_RATIO,
    _adaptive_day_target_seconds,
    _select_day_candidates,
    _planner_candidates_payload,
    _prompt_role_weights,
    _sample_contact_sheets,
    build_planner_request,
    local_plan,
    planner_schema,
    validate_and_normalize_plan,
)
from video_summary.project import DEFAULT_CONFIG, ProjectPaths
from video_summary.utils import VideoSummaryError


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

    def test_local_plan_pins_all_detected_interview_candidates_past_a_tight_target(self) -> None:
        self.config["editing"]["target_minutes_per_day"] = 5.0 / 60.0
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

        selected = _select_day_candidates(items, 20.0)

        self.assertEqual([item.candidate_id for item in selected], ["early", "run_a", "run_b", "late"])

    def test_short_day_uses_an_adaptive_ceiling_instead_of_every_candidate(self) -> None:
        items = [
            candidate(f"c{index}", f"2026-11-01T{8 + index:02d}:00:00-05:00")
            for index in range(5)
        ]

        adaptive_target = _adaptive_day_target_seconds(items, 60.0)
        selected = _select_day_candidates(items, 60.0)

        self.assertEqual(adaptive_target, 25.0 * SHORT_DAY_KEEP_RATIO)
        self.assertLess(len(selected), len(items))
        self.assertLessEqual(sum(item.duration for item in selected), adaptive_target)
        self.assertEqual(selected[0].candidate_id, "c0")
        self.assertEqual(selected[-1].candidate_id, "c4")

    def test_external_request_calls_the_day_target_a_soft_ceiling(self) -> None:
        request = build_planner_request(
            ProjectPaths(Path("/tmp"), "project"),
            self.config,
            "짧고 자연스럽게",
            self.candidates,
            "hash",
        )

        self.assertIn("채워야 하는 할당량이 아니라 상한", request)
        self.assertIn("usable candidate total: 10.0s", request)
        self.assertIn("recommended selection ceiling: 10.0s", request)
        self.assertIn("target_duration 필드는 12.0으로 유지", request)
        self.assertIn("location/caption은 null, speed는 1.0", request)

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

    def test_required_interview_runtime_can_extend_the_normal_plan_limit(self) -> None:
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
