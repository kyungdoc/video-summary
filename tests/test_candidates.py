from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from video_summary.candidates import (
    FULL_COVERAGE_PARTITION_POLICY_VERSION,
    INTERVIEW_DETECTION_POLICY_VERSION,
    JOURNEY_TRANSITION_POLICY_VERSION,
    MEAL_EVENT_POLICY_VERSION,
    PARTY_TRANSITION_CONTEXT_POLICY_VERSION,
    STORY_EVENT_CATALOG_POLICY_VERSION,
    VISUAL_SIGNAL_POLICY_VERSION,
    _assign_story_event_metadata,
    _candidate_exclusion_reason,
    _detect_family_interview_events,
    _detect_interview_events,
    _detect_journey_transition_windows,
    _detect_meal_events,
    _candidate_cache_key,
    _candidate_location,
    _candidate_windows,
    _journey_direction_destination,
    _journey_transition_signal,
    _merge_overlapping_windows,
    _meal_direct_signal,
    _partition_full_clip_coverage,
    build_candidates,
)
from video_summary.models import Candidate, Clip, TranscriptCue
from video_summary.project import DEFAULT_CONFIG, ProjectPaths


def _clip(
    clip_id: str = "clip",
    *,
    duration: float = 60.0,
    captured_at: str = "2026-08-20T10:00:00+09:00",
) -> Clip:
    return Clip(
        clip_id=clip_id,
        path=f"/tmp/{clip_id}.mp4",
        relative_path=f"{clip_id}.mp4",
        fingerprint=f"fp-{clip_id}",
        size_bytes=1,
        duration=duration,
        captured_at=captured_at,
        capture_source="filename",
        day_key="2026-08-20",
        travel_day=1,
        width=1920,
        height=1080,
        fps=30.0,
        codec="h264",
        rotation=0,
        has_audio=True,
    )


class CandidateCoverageTests(unittest.TestCase):
    def test_candidate_from_old_payload_defaults_required_event_ids(self) -> None:
        candidate = Candidate.from_dict(
            {
                "candidate_id": "candidate",
                "clip_id": "clip",
                "day_key": "2026-08-20",
                "travel_day": 1,
                "start": 0.0,
                "end": 2.0,
                "captured_at": "2026-08-20T10:00:00+09:00",
                "transcript": "",
                "roles": ["moment"],
                "score": 0.5,
                "speech_ratio": 0.0,
                "motion_score": 0.0,
                "visual_quality": 0.5,
                "location": None,
                "frame_path": "frames/candidate.jpg",
            }
        )

        self.assertEqual(candidate.required_event_ids, [])
        self.assertEqual(candidate.required_meal_event_ids, [])
        self.assertEqual(candidate.required_meal_context_ids, [])
        self.assertEqual(candidate.origin, "legacy")
        self.assertIsNone(candidate.story_event_id)
        self.assertEqual(candidate.story_stage, "body")
        self.assertEqual(candidate.importance, "supporting")
        self.assertEqual(candidate.speed_policy, "protected_1x")
        self.assertIsNone(candidate.exclusion_reason)

    def test_candidate_cache_key_tracks_visual_signal_policy(self) -> None:
        clip = Clip(
            clip_id="clip", path="/tmp/clip.mp4", relative_path="clip.mp4", fingerprint="fp",
            size_bytes=1, duration=10.0, captured_at="2026-08-20T10:00:00+09:00",
            capture_source="filename", day_key="2026-08-20", travel_day=1,
            width=1920, height=1080, fps=30.0, codec="h264", rotation=0, has_audio=True,
        )
        config = copy.deepcopy(DEFAULT_CONFIG)
        with tempfile.TemporaryDirectory() as temporary:
            paths = ProjectPaths(Path(temporary), "project")
            paths.ensure()
            current = _candidate_cache_key(paths, [clip], config)
            with patch(
                "video_summary.candidates.VISUAL_SIGNAL_POLICY_VERSION",
                VISUAL_SIGNAL_POLICY_VERSION + 1,
            ):
                changed = _candidate_cache_key(paths, [clip], config)

        self.assertNotEqual(current, changed)

    def test_candidate_cache_key_tracks_interview_policy_and_toggle(self) -> None:
        clip = _clip(duration=10.0)
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["editing"]["preserve_family_interviews"] = True
        with tempfile.TemporaryDirectory() as temporary:
            paths = ProjectPaths(Path(temporary), "project")
            paths.ensure()
            current = _candidate_cache_key(paths, [clip], config)
            disabled_config = copy.deepcopy(config)
            disabled_config["editing"]["preserve_family_interviews"] = False
            disabled = _candidate_cache_key(paths, [clip], disabled_config)
            with patch(
                "video_summary.candidates.INTERVIEW_DETECTION_POLICY_VERSION",
                INTERVIEW_DETECTION_POLICY_VERSION + 1,
            ):
                changed_policy = _candidate_cache_key(paths, [clip], config)

        self.assertNotEqual(current, disabled)
        self.assertNotEqual(current, changed_policy)

    def test_candidate_cache_key_tracks_journey_transition_policy(self) -> None:
        clip = _clip(duration=10.0)
        config = copy.deepcopy(DEFAULT_CONFIG)
        with tempfile.TemporaryDirectory() as temporary:
            paths = ProjectPaths(Path(temporary), "project")
            paths.ensure()
            current = _candidate_cache_key(paths, [clip], config)
            with patch(
                "video_summary.candidates.JOURNEY_TRANSITION_POLICY_VERSION",
                JOURNEY_TRANSITION_POLICY_VERSION + 1,
            ):
                changed = _candidate_cache_key(paths, [clip], config)
            with patch(
                "video_summary.candidates.PARTY_TRANSITION_CONTEXT_POLICY_VERSION",
                PARTY_TRANSITION_CONTEXT_POLICY_VERSION + 1,
            ):
                changed_context = _candidate_cache_key(paths, [clip], config)

        self.assertNotEqual(current, changed)
        self.assertNotEqual(current, changed_context)

    def test_candidate_cache_key_tracks_meal_policy_and_toggle(self) -> None:
        clip = _clip(duration=10.0)
        config = copy.deepcopy(DEFAULT_CONFIG)
        with tempfile.TemporaryDirectory() as temporary:
            paths = ProjectPaths(Path(temporary), "project")
            paths.ensure()
            current = _candidate_cache_key(paths, [clip], config)
            disabled_config = copy.deepcopy(config)
            disabled_config["editing"]["preserve_meal_events"] = False
            disabled = _candidate_cache_key(paths, [clip], disabled_config)
            with patch(
                "video_summary.candidates.MEAL_EVENT_POLICY_VERSION",
                MEAL_EVENT_POLICY_VERSION + 1,
            ):
                changed_policy = _candidate_cache_key(paths, [clip], config)

        self.assertNotEqual(current, disabled)
        self.assertNotEqual(current, changed_policy)

    def test_candidate_cache_key_tracks_full_coverage_event_policies_and_exclusions(self) -> None:
        clip = _clip(duration=10.0)
        config = copy.deepcopy(DEFAULT_CONFIG)
        with tempfile.TemporaryDirectory() as temporary:
            paths = ProjectPaths(Path(temporary), "project")
            paths.ensure()
            current = _candidate_cache_key(paths, [clip], config)
            excluded_config = copy.deepcopy(config)
            excluded_config["editing"]["exclude_ranges"] = [
                {
                    "match": "clip.mp4",
                    "start": 2.0,
                    "end": 4.0,
                    "reason": "사적 장면",
                }
            ]
            excluded = _candidate_cache_key(paths, [clip], excluded_config)
            with patch(
                "video_summary.candidates.FULL_COVERAGE_PARTITION_POLICY_VERSION",
                FULL_COVERAGE_PARTITION_POLICY_VERSION + 1,
            ):
                changed_partition = _candidate_cache_key(paths, [clip], config)
            with patch(
                "video_summary.candidates.STORY_EVENT_CATALOG_POLICY_VERSION",
                STORY_EVENT_CATALOG_POLICY_VERSION + 1,
            ):
                changed_event_catalog = _candidate_cache_key(paths, [clip], config)

        self.assertNotEqual(current, excluded)
        self.assertNotEqual(current, changed_partition)
        self.assertNotEqual(current, changed_event_catalog)

    def test_meal_direct_signal_requires_filmed_present_food_evidence(self) -> None:
        positives = [
            "라멘이 나왔습니다",
            "지금 초밥을 먹고 있어요",
            "이 아이스크림 엄청 맛있네요",
            "This pizza tastes delicious",
        ]
        negatives = [
            "내일 라멘 먹으러 갑니다",
            "어제 먹었던 음식이 맛있었어요",
            "밥 먹고 나왔는데 이제 숙소로 갑니다",
            "오늘 저녁을 먹었습니다",
            "요리책 속 고양이가 볶음밥을 먹네요",
            "보러 걸고기하고 나왔다니 눈이 엄청 많이 내려요",
            "고기를 먹고 식당에서 나왔어요",
        ]

        for text in positives:
            with self.subTest(text=text):
                self.assertIsNotNone(_meal_direct_signal(text, text))
        for text in negatives:
            with self.subTest(text=text):
                self.assertIsNone(_meal_direct_signal(text, text))

    def test_direct_meal_localization_keeps_group_level_retrospective_guard(self) -> None:
        clip = _clip("retrospective", duration=12.0)
        cues = {
            clip.clip_id: [
                TranscriptCue(1.0, 3.0, "어제 먹었던 피자는"),
                TranscriptCue(3.0, 5.0, "이거 진짜 맛있어요"),
            ]
        }

        self.assertEqual(_detect_meal_events([clip], cues), [])

    def test_sapporo_meal_brackets_recover_silent_body_clips(self) -> None:
        clips = [
            _clip("clip_1f584a801742bfc6", duration=30.0, captured_at="2026-08-20T08:00:00+09:00"),
            _clip("clip_e5b5516569fcbf68", duration=12.98, captured_at="2026-08-20T08:05:00+09:00"),
            _clip("clip_09993b62be79618f", duration=12.0, captured_at="2026-08-20T09:00:00+09:00"),
            _clip("clip_2d4c1871e944b838", duration=36.7, captured_at="2026-08-20T12:00:00+09:00"),
            _clip("clip_01d0a2498b9e63bc", duration=16.517, captured_at="2026-08-20T12:01:00+09:00"),
            _clip("clip_5efdd0224f934a16", duration=14.0, captured_at="2026-08-20T20:00:00+09:00"),
            _clip("clip_7866091dfdbc5251", duration=6.39, captured_at="2026-08-20T17:00:00+09:00"),
            _clip("clip_85b536e9d0aa58ba", duration=4.254, captured_at="2026-08-20T17:01:00+09:00"),
            _clip("clip_398d95c0dd2e8f5a", duration=5.956, captured_at="2026-08-20T19:00:00+09:00"),
            _clip("clip_b6e2656927e8d91c", duration=20.0, captured_at="2026-08-20T19:01:00+09:00"),
            _clip("clip_3fd035d9f5be4682", duration=182.0, captured_at="2026-08-20T20:51:00+09:00"),
        ]
        cues_by_clip = {
            "clip_1f584a801742bfc6": [TranscriptCue(20.0, 24.0, "자 밥 먹으러 갑시다 우리 비니 가자")],
            "clip_e5b5516569fcbf68": [TranscriptCue(1.0, 4.0, "안녕 안녕 안녕")],
            "clip_09993b62be79618f": [TranscriptCue(1.0, 4.0, "밥 먹고 왔더니 날이 어두워졌어요")],
            "clip_2d4c1871e944b838": [TranscriptCue(30.0, 35.0, "밥 집에 왔습니다. 도착했습니다")],
            "clip_01d0a2498b9e63bc": [TranscriptCue(2.0, 4.0, "고맙습니다")],
            "clip_5efdd0224f934a16": [TranscriptCue(1.0, 4.0, "이거 피자야 맛있게 먹자")],
            "clip_7866091dfdbc5251": [TranscriptCue(1.0, 3.0, "5시에 와서 대성공")],
            "clip_85b536e9d0aa58ba": [TranscriptCue(0.0, 3.0, "맛있게 먹고 돌아갑시다")],
            "clip_398d95c0dd2e8f5a": [TranscriptCue(1.0, 2.0, "빠파")],
            "clip_b6e2656927e8d91c": [TranscriptCue(0.0, 3.0, "밥 먹고 나왔는데 이제 돌아갑니다")],
            "clip_3fd035d9f5be4682": [
                TranscriptCue(1.0, 8.0, "그림책에서 고양이 친구들이 볶음밥을 만들어요"),
                TranscriptCue(9.0, 14.0, "다 같이 맛있게 먹고 잘 먹었습니다"),
            ],
        }

        events = _detect_meal_events(clips, cues_by_clip)
        by_clip = {
            option.clip_id: event
            for event in events
            for option in event.options
        }

        for clip_id in (
            "clip_e5b5516569fcbf68",
            "clip_01d0a2498b9e63bc",
            "clip_7866091dfdbc5251",
            "clip_398d95c0dd2e8f5a",
        ):
            self.assertIn(clip_id, by_clip)
            self.assertEqual(len(by_clip[clip_id].options), 1)
        self.assertNotIn("clip_1f584a801742bfc6", by_clip)
        self.assertNotIn("clip_09993b62be79618f", by_clip)
        self.assertNotIn("clip_b6e2656927e8d91c", by_clip)
        self.assertNotIn("clip_3fd035d9f5be4682", by_clip)

    def test_sapporo_snowy_street_exit_asr_is_not_served_food(self) -> None:
        clip = _clip(
            "clip_e791288eb18d9573",
            duration=6.31,
            captured_at="2025-02-05T11:44:14+09:00",
        )
        cues = {
            clip.clip_id: [
                TranscriptCue(
                    0.0,
                    6.0,
                    "보러 걸고기하고 나왔다니 눈이 엄청 많이 내려요",
                )
            ]
        }

        self.assertEqual(_detect_meal_events([clip], cues), [])

    def test_sapporo_ramen_shop_queue_asr_is_not_served_food(self) -> None:
        clip = _clip(
            "clip_bd060152d7b0a84c",
            duration=12.0,
            captured_at="2025-02-06T10:38:00+09:00",
        )
        cues = {
            clip.clip_id: [
                TranscriptCue(
                    0.0,
                    12.0,
                    "네, 이 치킨 라면이 나왔습니다. 가보시죠. 사람이 한 5명 정도 줄 사서 기다리고 있네요.",
                )
            ]
        }

        self.assertEqual(_detect_meal_events([clip], cues), [])

    def test_split_question_and_misheard_food_is_not_a_reveal(self) -> None:
        clip = _clip(
            "split-question",
            duration=26.26,
            captured_at="2025-02-05T13:01:10+09:00",
        )
        cues = {
            clip.clip_id: [
                TranscriptCue(0.0, 2.0, "이거 뭐야?"),
                TranscriptCue(2.0, 4.0, "이거 뭐야? 소금이야?"),
                TranscriptCue(4.0, 6.0, "뭐야?"),
                TranscriptCue(6.0, 8.0, "아빠 피자야"),
                TranscriptCue(8.0, 10.0, "나한테 거실게"),
            ]
        }

        self.assertEqual(_detect_meal_events([clip], cues), [])

    def test_lodging_arrival_before_meal_thanks_is_not_inferred_as_food(self) -> None:
        clips = [
            _clip(
                "lodging_arrival",
                duration=14.0,
                captured_at="2025-12-30T17:32:14+07:00",
            ),
            _clip(
                "meal_thanks",
                duration=8.0,
                captured_at="2025-12-30T17:32:40+07:00",
            ),
        ]
        cues = {
            "lodging_arrival": [
                TranscriptCue(
                    0.0,
                    6.0,
                    "멀큐리 푸꾸옥 리조트에 도착했습니다. 평화로운데?",
                )
            ],
            "meal_thanks": [TranscriptCue(0.0, 3.0, "맛있게 잘 먹었습니다")],
        }

        self.assertEqual(_detect_meal_events(clips, cues), [])

    def test_journey_words_do_not_hide_explicit_meal_body_after_setup(self) -> None:
        clips = [
            _clip(
                "breakfast_setup",
                duration=8.0,
                captured_at="2026-08-20T07:59:00+09:00",
            ),
            _clip(
                "breakfast_body",
                duration=10.0,
                captured_at="2026-08-20T08:00:00+09:00",
            ),
        ]
        cues = {
            "breakfast_setup": [TranscriptCue(0.0, 3.0, "아침 먹으러 갑니다")],
            "breakfast_body": [
                TranscriptCue(0.0, 4.0, "호텔 조식을 먹고 있어요")
            ],
        }

        events = _detect_meal_events(clips, cues)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].options[0].clip_id, "breakfast_body")
        self.assertEqual(events[0].setup_options[0].clip_id, "breakfast_setup")

    def test_scenery_between_meal_setup_and_closure_is_not_inferred_as_food(self) -> None:
        clips = [
            _clip("lunch_setup", duration=8.0, captured_at="2026-08-20T12:00:00+09:00"),
            _clip("sea_view", duration=9.0, captured_at="2026-08-20T12:01:00+09:00"),
            _clip("lunch_closure", duration=8.0, captured_at="2026-08-20T12:02:00+09:00"),
        ]
        cues = {
            "lunch_setup": [TranscriptCue(0.0, 3.0, "점심 먹으러 갑시다")],
            "sea_view": [TranscriptCue(0.0, 4.0, "바다가 정말 예쁘네요")],
            "lunch_closure": [TranscriptCue(0.0, 3.0, "점심 먹고 나왔어요")],
        }

        self.assertEqual(_detect_meal_events(clips, cues), [])

    def test_distant_silent_clip_after_restaurant_arrival_is_not_a_meal_body(self) -> None:
        clips = [
            _clip(
                "restaurant",
                duration=30.0,
                captured_at="2026-08-20T19:20:00+09:00",
            ),
            _clip(
                "hotel-show",
                duration=20.0,
                captured_at="2026-08-20T20:10:00+09:00",
            ),
        ]
        cues = {
            "restaurant": [TranscriptCue(20.0, 24.0, "저녁 식당에 도착했습니다")],
            "hotel-show": [],
        }

        self.assertEqual(_detect_meal_events(clips, cues), [])

    def test_meal_memory_or_plan_between_setup_and_closure_is_not_a_body(self) -> None:
        for body_text in (
            "어제 먹었던 피자는 정말 맛있었는데 또 먹고 싶어요",
            "내일은 피자를 먹을 예정이에요",
        ):
            with self.subTest(body_text=body_text):
                clips = [
                    _clip("setup", duration=8.0, captured_at="2026-08-20T12:00:00+09:00"),
                    _clip("body", duration=9.0, captured_at="2026-08-20T12:01:00+09:00"),
                    _clip("closure", duration=8.0, captured_at="2026-08-20T12:02:00+09:00"),
                ]
                cues = {
                    "setup": [TranscriptCue(0.0, 3.0, "점심 먹으러 갑시다")],
                    "body": [TranscriptCue(0.0, 4.0, body_text)],
                    "closure": [TranscriptCue(0.0, 3.0, "점심 먹고 나왔어요")],
                }

                self.assertEqual(_detect_meal_events(clips, cues), [])

    def test_retrospective_food_praise_is_not_a_current_meal_closure(self) -> None:
        clips = [
            _clip("opaque", duration=8.0, captured_at="2026-08-20T12:00:00+09:00"),
            _clip("memory", duration=8.0, captured_at="2026-08-20T12:01:00+09:00"),
        ]
        cues = {
            "opaque": [TranscriptCue(1.0, 2.0, "안녕하세요")],
            "memory": [TranscriptCue(1.0, 3.0, "어제 라멘 정말 맛있었어요")],
        }

        self.assertEqual(_detect_meal_events(clips, cues), [])

    def test_served_food_without_approach_context_remains_direct_meal(self) -> None:
        clip = _clip(
            "served-ramen",
            duration=10.0,
            captured_at="2026-08-20T12:00:00+09:00",
        )
        cues = {
            clip.clip_id: [TranscriptCue(1.0, 3.0, "라멘이 나왔습니다. 먹어보시죠.")]
        }

        events = _detect_meal_events([clip], cues)

        self.assertEqual(len(events), 1)
        self.assertEqual([option.clip_id for option in events[0].options], [clip.clip_id])
        self.assertEqual(events[0].setup_options, ())
        self.assertEqual(events[0].closure_options, ())

    def test_served_food_body_keeps_a_nearby_tasting_reaction(self) -> None:
        clip = _clip("ice-cream", duration=8.8)
        cues = {
            clip.clip_id: [
                TranscriptCue(0.14, 2.14, "아이스크림 받았습니다"),
                TranscriptCue(3.14, 5.14, "촉촉한게"),
                TranscriptCue(5.14, 7.14, "음 엄청 맛있네요"),
            ]
        }

        events = _detect_meal_events([clip], cues)

        self.assertEqual(len(events), 1)
        self.assertLessEqual(events[0].options[0].start, 0.14)
        self.assertGreaterEqual(events[0].options[0].end, 7.14)

    def test_direct_meal_keeps_separate_same_clip_setup_and_closure(self) -> None:
        clip = _clip(
            "same-clip-meal-story",
            duration=14.0,
            captured_at="2026-08-20T12:00:00+09:00",
        )
        cues = {
            clip.clip_id: [
                TranscriptCue(1.0, 2.0, "이제 저녁 먹으러 갑시다"),
                TranscriptCue(5.0, 6.0, "라멘이 나왔습니다"),
                TranscriptCue(9.0, 10.0, "밥 먹고 나왔는데 맛있었어요"),
            ]
        }

        events = _detect_meal_events([clip], cues)

        self.assertEqual(len(events), 1)
        self.assertEqual(len(events[0].options), 1)
        self.assertEqual(len(events[0].setup_options), 1)
        self.assertEqual(len(events[0].closure_options), 1)
        self.assertLess(events[0].setup_options[0].end, events[0].options[0].start)
        self.assertLess(events[0].options[0].end, events[0].closure_options[0].start)

    def test_meal_context_localizes_the_matching_cues_inside_a_long_group(self) -> None:
        clips = [
            _clip("arrival", duration=36.7, captured_at="2026-08-20T12:00:00+09:00"),
            _clip("body", duration=10.0, captured_at="2026-08-20T12:01:00+09:00"),
        ]
        cues = {
            "arrival": [
                TranscriptCue(16.24, 18.50, "엄마 사랑해"),
                TranscriptCue(18.50, 20.72, "아빠 사랑해"),
                TranscriptCue(20.72, 22.52, "사랑해는 무슨 말"),
                TranscriptCue(22.52, 24.26, "그렇게 하자"),
                TranscriptCue(24.26, 28.76, "오늘 재미있었지"),
                TranscriptCue(28.76, 30.96, "자 어쨌든 밥 집에 왔습니다"),
                TranscriptCue(30.96, 32.96, "도착했습니다"),
            ],
            "body": [TranscriptCue(1.0, 3.0, "고맙습니다")],
        }

        events = _detect_meal_events(clips, cues)

        self.assertEqual(len(events), 1)
        setup = events[0].setup_options[0]
        self.assertLessEqual(setup.start, 28.76)
        self.assertGreaterEqual(setup.end, 30.96)
        self.assertGreater(setup.start, 24.26)

    def test_direct_meal_keeps_context_when_a_clip_boundary_splits_the_story(self) -> None:
        cases = [
            (
                [
                    _clip("setup-body", duration=12.0, captured_at="2026-08-20T12:00:00+09:00"),
                    _clip("closure", duration=8.0, captured_at="2026-08-20T12:01:00+09:00"),
                ],
                {
                    "setup-body": [
                        TranscriptCue(1.0, 2.0, "저녁 먹으러 갑시다"),
                        TranscriptCue(5.0, 6.0, "라멘이 나왔습니다"),
                    ],
                    "closure": [TranscriptCue(1.0, 2.0, "밥 먹고 나왔는데 맛있었어요")],
                },
            ),
            (
                [
                    _clip("setup", duration=8.0, captured_at="2026-08-20T12:00:00+09:00"),
                    _clip("body-closure", duration=12.0, captured_at="2026-08-20T12:01:00+09:00"),
                ],
                {
                    "setup": [TranscriptCue(1.0, 2.0, "저녁 먹으러 갑시다")],
                    "body-closure": [
                        TranscriptCue(1.0, 2.0, "라멘이 나왔습니다"),
                        TranscriptCue(5.0, 6.0, "밥 먹고 나왔는데 맛있었어요"),
                    ],
                },
            ),
        ]

        for clips, cues in cases:
            with self.subTest(clips=[clip.clip_id for clip in clips]):
                events = _detect_meal_events(clips, cues)

                self.assertEqual(len(events), 1)
                self.assertEqual(len(events[0].setup_options), 1)
                self.assertEqual(len(events[0].options), 1)
                self.assertEqual(len(events[0].closure_options), 1)

    def test_setup_does_not_claim_a_closure_after_an_intervening_meal(self) -> None:
        clips = [
            _clip("lunch-setup", duration=8.0, captured_at="2026-08-20T12:00:00+09:00"),
            _clip("lunch-body", duration=8.0, captured_at="2026-08-20T12:01:00+09:00"),
            _clip("dinner-body", duration=8.0, captured_at="2026-08-20T14:00:00+09:00"),
            _clip("dinner-closure", duration=8.0, captured_at="2026-08-20T14:10:00+09:00"),
        ]
        cues = {
            "lunch-setup": [TranscriptCue(1.0, 2.0, "점심 먹으러 갑시다")],
            "lunch-body": [TranscriptCue(1.0, 2.0, "라멘이 나왔습니다")],
            "dinner-body": [TranscriptCue(1.0, 2.0, "피자가 나왔습니다")],
            "dinner-closure": [TranscriptCue(1.0, 2.0, "저녁 먹고 나왔어요")],
        }

        events = _detect_meal_events(clips, cues)
        lunch = next(
            event
            for event in events
            if any(option.clip_id == "lunch-body" for option in event.options)
        )
        dinner = next(
            event
            for event in events
            if any(option.clip_id == "dinner-body" for option in event.options)
        )

        self.assertEqual(len(lunch.setup_options), 1)
        self.assertEqual(lunch.closure_options, ())
        self.assertEqual(len(dinner.closure_options), 1)

    def test_setup_and_closure_stay_one_event_across_nearby_body_views(self) -> None:
        clips = [
            _clip("setup", duration=8.0, captured_at="2026-08-20T12:00:00+09:00"),
            _clip("body-wide", duration=8.0, captured_at="2026-08-20T12:01:00+09:00"),
            _clip("body-close", duration=8.0, captured_at="2026-08-20T12:02:00+09:00"),
            _clip("closure", duration=8.0, captured_at="2026-08-20T12:03:00+09:00"),
        ]
        cues = {
            "setup": [TranscriptCue(1.0, 2.0, "점심 먹으러 갑시다")],
            "body-wide": [TranscriptCue(1.0, 2.0, "라멘이 나왔습니다")],
            "body-close": [TranscriptCue(1.0, 2.0, "초밥이 나왔습니다")],
            "closure": [TranscriptCue(1.0, 2.0, "점심 먹고 나왔어요")],
        }

        events = _detect_meal_events(clips, cues)

        self.assertEqual(len(events), 1)
        self.assertEqual(
            [option.clip_id for option in events[0].options],
            ["body-wide", "body-close"],
        )
        self.assertEqual(len(events[0].setup_options), 1)
        self.assertEqual(len(events[0].closure_options), 1)

    def test_explicit_body_after_low_information_clip_wins_the_meal_bracket(self) -> None:
        clips = [
            _clip("setup", duration=8.0, captured_at="2026-08-20T12:00:00+09:00"),
            _clip("greeting", duration=8.0, captured_at="2026-08-20T12:01:00+09:00"),
            _clip("food", duration=8.0, captured_at="2026-08-20T12:02:00+09:00"),
            _clip("closure", duration=8.0, captured_at="2026-08-20T12:03:00+09:00"),
        ]
        cues = {
            "setup": [TranscriptCue(1.0, 2.0, "점심 먹으러 갑시다")],
            "greeting": [TranscriptCue(1.0, 2.0, "안녕하세요")],
            "food": [TranscriptCue(1.0, 2.0, "라멘이 나왔습니다")],
            "closure": [TranscriptCue(1.0, 2.0, "점심 먹고 나왔어요")],
        }

        events = _detect_meal_events(clips, cues)

        self.assertEqual(len(events), 1)
        self.assertEqual([option.clip_id for option in events[0].options], ["food"])
        self.assertEqual(events[0].setup_options[0].clip_id, "setup")
        self.assertEqual(events[0].closure_options[0].clip_id, "closure")

    def test_same_clip_setup_and_nearby_bodies_form_only_one_event(self) -> None:
        clips = [
            _clip("setup-body", duration=12.0, captured_at="2026-08-20T12:00:00+09:00"),
            _clip("body-close", duration=8.0, captured_at="2026-08-20T12:01:00+09:00"),
            _clip("closure", duration=8.0, captured_at="2026-08-20T12:02:00+09:00"),
        ]
        cues = {
            "setup-body": [
                TranscriptCue(1.0, 2.0, "점심 먹으러 갑시다"),
                TranscriptCue(5.0, 6.0, "라멘이 나왔습니다"),
            ],
            "body-close": [TranscriptCue(1.0, 2.0, "초밥이 나왔습니다")],
            "closure": [TranscriptCue(1.0, 2.0, "점심 먹고 나왔어요")],
        }

        events = _detect_meal_events(clips, cues)

        self.assertEqual(len(events), 1)
        self.assertEqual(
            [option.clip_id for option in events[0].options],
            ["setup-body", "body-close"],
        )
        self.assertEqual(events[0].setup_options[0].clip_id, "setup-body")
        self.assertEqual(events[0].closure_options[0].clip_id, "closure")

    def test_closure_claims_the_whole_nearby_direct_body_run(self) -> None:
        clips = [
            _clip("body-wide", duration=8.0, captured_at="2026-08-20T12:00:00+09:00"),
            _clip("body-close", duration=8.0, captured_at="2026-08-20T12:01:00+09:00"),
            _clip("closure", duration=8.0, captured_at="2026-08-20T12:02:00+09:00"),
        ]
        cues = {
            "body-wide": [TranscriptCue(1.0, 2.0, "라멘이 나왔습니다")],
            "body-close": [TranscriptCue(1.0, 2.0, "초밥이 나왔습니다")],
            "closure": [TranscriptCue(1.0, 2.0, "점심 먹고 나왔어요")],
        }

        events = _detect_meal_events(clips, cues)

        self.assertEqual(len(events), 1)
        self.assertEqual(
            [option.clip_id for option in events[0].options],
            ["body-wide", "body-close"],
        )
        self.assertEqual(events[0].setup_options, ())
        self.assertEqual(events[0].closure_options[0].clip_id, "closure")

    def test_closure_prefers_nearby_explicit_body_over_immediate_opaque_clip(self) -> None:
        clips = [
            _clip("food", duration=8.0, captured_at="2026-08-20T12:00:00+09:00"),
            _clip("greeting", duration=8.0, captured_at="2026-08-20T12:01:00+09:00"),
            _clip("closure", duration=8.0, captured_at="2026-08-20T12:02:00+09:00"),
        ]
        cues = {
            "food": [TranscriptCue(1.0, 2.0, "라멘이 나왔습니다")],
            "greeting": [TranscriptCue(1.0, 2.0, "안녕하세요")],
            "closure": [TranscriptCue(1.0, 2.0, "점심 먹고 나왔어요")],
        }

        events = _detect_meal_events(clips, cues)

        self.assertEqual(len(events), 1)
        self.assertEqual([option.clip_id for option in events[0].options], ["food"])
        self.assertEqual(events[0].closure_options[0].clip_id, "closure")

    def test_order_body_and_food_reaction_form_one_meal_story(self) -> None:
        for reaction_text in ("라멘 정말 맛있었어요", "라멘 진짜 맛있었어요"):
            with self.subTest(reaction_text=reaction_text):
                clips = [
                    _clip("order", duration=8.0, captured_at="2026-08-20T12:00:00+09:00"),
                    _clip("body", duration=8.0, captured_at="2026-08-20T12:01:00+09:00"),
                    _clip("reaction", duration=8.0, captured_at="2026-08-20T12:02:00+09:00"),
                ]
                cues = {
                    "order": [TranscriptCue(1.0, 2.0, "메뉴를 주문했습니다")],
                    "body": [TranscriptCue(1.0, 2.0, "라멘이 나왔습니다")],
                    "reaction": [TranscriptCue(1.0, 2.0, reaction_text)],
                }

                events = _detect_meal_events(clips, cues)

                self.assertEqual(len(events), 1)
                self.assertEqual([option.clip_id for option in events[0].options], ["body"])
                self.assertEqual(events[0].setup_options[0].clip_id, "order")
                self.assertEqual(events[0].closure_options[0].clip_id, "reaction")
                self.assertIn("meal_setup_order", events[0].signals)
                self.assertIn("meal_closure_reaction", events[0].signals)

    def test_order_context_never_becomes_an_inferred_meal_body(self) -> None:
        clips = [
            _clip("setup", duration=8.0, captured_at="2026-08-20T12:00:00+09:00"),
            _clip("order", duration=8.0, captured_at="2026-08-20T12:01:00+09:00"),
            _clip("closure", duration=8.0, captured_at="2026-08-20T12:02:00+09:00"),
        ]
        cues = {
            "setup": [TranscriptCue(1.0, 2.0, "점심 먹으러 갑시다")],
            "order": [TranscriptCue(1.0, 2.0, "메뉴를 주문했습니다")],
            "closure": [TranscriptCue(1.0, 2.0, "점심 먹고 나왔어요")],
        }

        self.assertEqual(_detect_meal_events(clips, cues), [])

    def test_short_unrelated_clip_is_not_an_opaque_meal_body(self) -> None:
        for body_text in ("주차했습니다", "강아지가 귀여워요", "약 먹어요"):
            with self.subTest(body_text=body_text):
                clips = [
                    _clip("setup", duration=8.0, captured_at="2026-08-20T12:00:00+09:00"),
                    _clip("body", duration=8.0, captured_at="2026-08-20T12:01:00+09:00"),
                    _clip("closure", duration=8.0, captured_at="2026-08-20T12:02:00+09:00"),
                ]
                cues = {
                    "setup": [TranscriptCue(1.0, 2.0, "점심 먹으러 갑시다")],
                    "body": [TranscriptCue(1.0, 2.0, body_text)],
                    "closure": [TranscriptCue(1.0, 2.0, "점심 먹고 나왔어요")],
                }

                self.assertEqual(_detect_meal_events(clips, cues), [])

    def test_direct_meals_cluster_only_nearby_same_subtype_views(self) -> None:
        clips = [
            _clip("lunch-wide", duration=10.0, captured_at="2026-08-20T12:00:00+09:00"),
            _clip("lunch-close", duration=10.0, captured_at="2026-08-20T12:05:00+09:00"),
            _clip("dessert", duration=10.0, captured_at="2026-08-20T13:00:00+09:00"),
        ]
        cues = {
            "lunch-wide": [TranscriptCue(1.0, 3.0, "라멘이 나왔습니다")],
            "lunch-close": [TranscriptCue(1.0, 3.0, "초밥이 나왔습니다")],
            "dessert": [TranscriptCue(1.0, 3.0, "아이스크림을 받았습니다")],
        }

        events = _detect_meal_events(clips, cues)

        self.assertEqual(len(events), 2)
        self.assertEqual(
            {option.clip_id for option in events[0].options},
            {"lunch-wide", "lunch-close"},
        )
        self.assertEqual([option.clip_id for option in events[1].options], ["dessert"])
        self.assertEqual(events[1].subtype, "dessert")

    def test_meal_plan_does_not_promote_an_unrelated_intervening_clip(self) -> None:
        clips = [
            _clip("meal-plan", duration=10.0, captured_at="2026-08-20T10:00:00+09:00"),
            _clip("unrelated-view", duration=10.0, captured_at="2026-08-20T10:05:00+09:00"),
            _clip("actual-lunch", duration=10.0, captured_at="2026-08-20T11:00:00+09:00"),
        ]
        cues = {
            "meal-plan": [TranscriptCue(1.0, 3.0, "이제 라멘 먹으러 갑니다")],
            "unrelated-view": [TranscriptCue(1.0, 3.0, "바다가 정말 예쁘네요")],
            "actual-lunch": [TranscriptCue(1.0, 3.0, "라멘이 나왔습니다")],
        }

        events = _detect_meal_events(clips, cues)
        option_clip_ids = {
            option.clip_id for event in events for option in event.options
        }

        self.assertEqual(option_clip_ids, {"actual-lunch"})

    def test_meal_boundaries_keep_only_compact_body_mandatory(self) -> None:
        windows = _merge_overlapping_windows(
            [
                (0.0, 30.0, "speech"),
                (13.25, 17.75, "meal"),
            ]
        )

        self.assertEqual(
            windows,
            [
                (0.0, 13.25, "speech"),
                (13.25, 17.75, "meal"),
                (17.75, 30.0, "speech"),
            ],
        )
        self.assertEqual(
            sum(end - start for start, end, origin in windows if origin == "meal"),
            4.5,
        )
        self.assertTrue(
            all(left[1] <= right[0] for left, right in zip(windows, windows[1:]))
        )

    def test_build_records_body_plus_detected_setup_and_closure_groups(self) -> None:
        clips = [
            _clip("meal-setup", duration=10.0, captured_at="2026-08-20T12:00:00+09:00"),
            _clip("meal-body", duration=10.0, captured_at="2026-08-20T12:01:00+09:00"),
            _clip("meal-closure", duration=10.0, captured_at="2026-08-20T12:02:00+09:00"),
        ]
        cues_by_clip = {
            "meal-setup": [TranscriptCue(1.0, 3.0, "자 밥 먹으러 갑시다")],
            "meal-body": [TranscriptCue(1.0, 3.0, "안녕")],
            "meal-closure": [TranscriptCue(1.0, 3.0, "밥 먹고 나왔는데")],
        }
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["project"]["name"] = "meal-event-test"
        config["analysis"]["max_candidates_per_clip"] = 1

        with tempfile.TemporaryDirectory() as temporary:
            paths = ProjectPaths(Path(temporary), "project")
            paths.ensure()
            with (
                patch(
                    "video_summary.candidates.load_transcript",
                    side_effect=lambda _paths, clip_id: cues_by_clip[clip_id],
                ),
                patch("video_summary.candidates.analyze_visual_signals", return_value=[]),
                patch("video_summary.candidates.extract_frame"),
            ):
                payload = build_candidates(paths, clips, config)

        meal_events = [
            event for event in payload["required_events"] if event["kind"] == "meal"
        ]
        self.assertEqual(payload["version"], 5)
        self.assertEqual(payload["policy_versions"]["meal_event"], MEAL_EVENT_POLICY_VERSION)
        self.assertEqual(
            payload["policy_versions"]["full_coverage_partition"],
            FULL_COVERAGE_PARTITION_POLICY_VERSION,
        )
        self.assertEqual(
            payload["policy_versions"]["story_event_catalog"],
            STORY_EVENT_CATALOG_POLICY_VERSION,
        )
        self.assertEqual(len(meal_events), 1)
        event = meal_events[0]
        self.assertEqual(event["selection_mode"], "one_of")
        self.assertEqual(len(event["option_ranges"]), 1)
        self.assertEqual(event["option_ranges"][0]["clip_id"], "meal-body")
        self.assertNotIn("transcript", event)
        self.assertNotIn("path", event)
        tagged = [
            candidate
            for candidate in payload["candidates"]
            if event["event_id"] in candidate["required_meal_event_ids"]
        ]
        self.assertTrue(tagged)
        self.assertEqual({candidate["clip_id"] for candidate in tagged}, {"meal-body"})
        self.assertEqual(event["candidate_ids"], [candidate["candidate_id"] for candidate in tagged])
        self.assertEqual(
            event["option_ranges"][0]["candidate_ids"],
            [candidate["candidate_id"] for candidate in tagged],
        )
        self.assertEqual(
            [
                (group["stage"], group["selection_mode"])
                for group in event["context_groups"]
            ],
            [("setup", "one_of"), ("closure", "one_of")],
        )
        context_groups = {group["stage"]: group for group in event["context_groups"]}
        setup_candidates = [
            candidate for candidate in payload["candidates"] if candidate["clip_id"] == "meal-setup"
        ]
        closure_candidates = [
            candidate
            for candidate in payload["candidates"]
            if candidate["clip_id"] == "meal-closure"
        ]
        self.assertTrue(any("food" in candidate["roles"] for candidate in setup_candidates))
        self.assertTrue(any("food" in candidate["roles"] for candidate in closure_candidates))
        self.assertTrue(
            all(not candidate["required_meal_event_ids"] for candidate in setup_candidates)
        )
        self.assertTrue(
            all(not candidate["required_meal_event_ids"] for candidate in closure_candidates)
        )
        for stage, stage_candidates in (
            ("setup", setup_candidates),
            ("closure", closure_candidates),
        ):
            group = context_groups[stage]
            tagged_context = [
                candidate
                for candidate in stage_candidates
                if group["context_id"] in candidate["required_meal_context_ids"]
            ]
            self.assertTrue(tagged_context)
            self.assertEqual(
                group["candidate_ids"],
                [candidate["candidate_id"] for candidate in tagged_context],
            )
        self.assertTrue(
            all(not candidate["required_meal_context_ids"] for candidate in tagged)
        )

    def test_disabling_meal_preservation_leaves_food_role_nonmandatory(self) -> None:
        clip = _clip("food-plan", duration=10.0)
        cues = [TranscriptCue(1.0, 3.0, "자 저녁 먹으러 갑시다")]
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["project"]["name"] = "meal-disabled-test"
        config["editing"]["preserve_meal_events"] = False

        with tempfile.TemporaryDirectory() as temporary:
            paths = ProjectPaths(Path(temporary), "project")
            paths.ensure()
            with (
                patch("video_summary.candidates.load_transcript", return_value=cues),
                patch("video_summary.candidates.analyze_visual_signals", return_value=[]),
                patch("video_summary.candidates.extract_frame"),
            ):
                payload = build_candidates(paths, [clip], config)

        self.assertEqual(payload["required_events"], [])
        self.assertTrue(any("food" in candidate["roles"] for candidate in payload["candidates"]))
        self.assertTrue(
            all(not candidate["required_meal_event_ids"] for candidate in payload["candidates"])
        )
        self.assertTrue(
            all(not candidate["required_meal_context_ids"] for candidate in payload["candidates"])
        )

    def test_detects_high_confidence_journey_transition_statements(self) -> None:
        examples = [
            "할머니, 할아버지 태우고 이제 공항으로 갑니다",
            "부모님을 픽업해서 함께 이동합니다",
            "오늘 도쿄에서 환승하고 삿포로로 갑니다",
            "렌터카를 반납하러 갑니다",
            "오릭스 렌터카러 갑니다",
            "호텔 체크인을 마쳤습니다",
            "우리의 호텔 방입니다",
            "여기가 우리 숙소입니다",
            "복스 라이트 호텔을 찾았습니다",
            "램프라이트 복스 호텔을 찾았습니다",
            "신치토세 공항에 도착했습니다",
            "공항에 내렸습니다",
            "샌프란시스코에 도착했습니다. 다행히 세관도 잘 통과했습니다",
            "우리 샌프라이 시스코 왕이 도착했습니다. 무사히 세관도 통과하고",
            "삿포로역에서 출발했습니다",
            "비행기에서 내렸습니다",
            "We picked up our grandparents and headed to the airport.",
            "We're transferring to another flight.",
            "We returned the rental car.",
            "We're checking out of the hotel.",
            "We arrived at the airport.",
            "We departed from the airport.",
            "We joined our family at the airport.",
            "We're getting off the train.",
            "We took the train.",
            "We arrived in San Francisco and cleared customs.",
        ]

        for text in examples:
            with self.subTest(text=text):
                self.assertIsNotNone(_journey_transition_signal(text))

    def test_rejects_questions_instructions_and_non_transport_arrivals(self) -> None:
        examples = [
            "우리 어디 가죠?",
            "할머니 태우고 어디 가죠?",
            "자, 이제 가자",
            "이제 출발",
            "출발합니다",
            "공항 탑승 안내 방송입니다",
            "승객 여러분, 비행기에 탑승해 주세요",
            "렌터카를 반납하세요",
            "호텔 체크인 안내입니다",
            "맛집에 도착했습니다",
            "식당에 도착했습니다",
            "오도리 공원에 도착했습니다",
            "관광지에 도착했습니다",
            "관광 지역에 도착했습니다",
            "행사 구역에 도착했습니다",
            "택시를 타고 가고 있습니다",
            "비행기 타고 삿포로로 가는 길입니다",
            "동키호텔에 왔습니다",
            "돈키호테 호텔에 왔습니다",
            "돈키호테 호텔을 찾았습니다",
            "호텔 방이 넓고 예쁘네요",
            "숙소가 좋아 보여요",
            "여행 전에 좋은 호텔을 찾았습니다",
            "인터넷에서 묵을 호텔을 찾았습니다",
            "샌프란시스코에 도착했습니다",
            "반납했습니다",
            "Where are we going?",
            "Please board the train.",
            "We arrived at the museum.",
            "We're taking the train to the airport.",
            "We're transferring photos over Wi-Fi.",
            "We're making a connection over Wi-Fi.",
            "We arrived in San Francisco.",
        ]

        for text in examples:
            with self.subTest(text=text):
                self.assertIsNone(_journey_transition_signal(text))

    def test_mid_clip_transition_survives_max_one_and_records_policy(self) -> None:
        clip = _clip("mid-clip-transition", duration=60.0)
        cues = [
            TranscriptCue(1.0, 4.0, "공항 맛집이 대박! 가족 여행이 정말 재미있어요"),
            TranscriptCue(30.0, 33.0, "할머니, 할아버지 태우고 이제 공항으로 갑니다"),
        ]
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["project"]["name"] = "journey-transition-test"
        config["analysis"]["max_candidates_per_clip"] = 1

        with tempfile.TemporaryDirectory() as temporary:
            paths = ProjectPaths(Path(temporary), "project")
            paths.ensure()
            with (
                patch("video_summary.candidates.load_transcript", return_value=cues),
                patch("video_summary.candidates.analyze_visual_signals", return_value=[]),
                patch("video_summary.candidates.extract_frame"),
            ):
                payload = build_candidates(paths, [clip], config)

        transitions = [
            candidate
            for candidate in payload["candidates"]
            if "transition" in candidate["roles"]
        ]
        self.assertEqual(
            payload["policy_versions"]["journey_transition"],
            JOURNEY_TRANSITION_POLICY_VERSION,
        )
        self.assertGreater(payload["count"], 1)
        self.assertEqual(len(transitions), 1)
        self.assertGreater(transitions[0]["start"], 20.0)
        self.assertEqual(transitions[0]["roles"][:2], ["transition", "journey"])
        self.assertGreaterEqual(transitions[0]["score"], 0.7)

    def test_family_pickup_transition_includes_adjacent_airport_direction(self) -> None:
        clip = _clip(
            "clip_dbcb8447ef93c8ac",
            duration=8.959,
            captured_at="2025-02-02T17:29:00+09:00",
        )
        cues = [
            TranscriptCue(0.0, 2.0, "잘가."),
            TranscriptCue(2.0, 4.0, "할머니, 할아버지 태우고"),
            TranscriptCue(4.0, 6.0, "잘가."),
            TranscriptCue(6.0, 8.0, "인창공항으로 갑니다."),
        ]
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["project"]["name"] = "family-pickup-direction-test"
        config["analysis"]["max_candidates_per_clip"] = 1

        with tempfile.TemporaryDirectory() as temporary:
            paths = ProjectPaths(Path(temporary), "project")
            paths.ensure()
            with (
                patch("video_summary.candidates.load_transcript", return_value=cues),
                patch("video_summary.candidates.analyze_visual_signals", return_value=[]),
                patch("video_summary.candidates.extract_frame"),
            ):
                payload = build_candidates(paths, [clip], config)

        transitions = [
            candidate
            for candidate in payload["candidates"]
            if "transition" in candidate["roles"]
        ]
        self.assertEqual(len(transitions), 1)
        self.assertIn("할머니, 할아버지 태우고", transitions[0]["transcript"])
        self.assertIn("인창공항으로 갑니다", transitions[0]["transcript"])
        self.assertGreaterEqual(transitions[0]["end"], 8.0)
        self.assertLessEqual(transitions[0]["duration"], 8.0)
        ordered = sorted(payload["candidates"], key=lambda item: item["start"])
        self.assertTrue(
            all(left["end"] <= right["start"] for left, right in zip(ordered, ordered[1:]))
        )

    def test_party_transition_direction_context_requires_a_concrete_waypoint(self) -> None:
        positives = [
            "인천공항으로 갑니다",
            "삿포로로 갑니다",
            "We are headed to the airport.",
            "We're headed to New York.",
        ]
        negatives = [
            "공항으로 가나요?",
            "공항으로 가세요",
            "식당으로 갑니다",
            "오도리 공원으로 갑니다",
            "좋은 곳으로 갑니다",
            "We are going to dinner.",
            "We're heading to the museum.",
            "We're heading to bed.",
        ]

        for text in positives:
            with self.subTest(text=text):
                self.assertTrue(_journey_direction_destination(text))
        for text in negatives:
            with self.subTest(text=text):
                self.assertFalse(_journey_direction_destination(text))

    def test_party_transition_does_not_extend_through_generic_destination_chatter(self) -> None:
        clip = _clip("party-generic-destination", duration=12.0)
        windows = _detect_journey_transition_windows(
            clip,
            [
                TranscriptCue(2.0, 4.0, "부모님을 픽업했습니다"),
                TranscriptCue(4.0, 6.0, "오늘도 신나네요"),
                TranscriptCue(6.0, 8.0, "식당으로 갑니다"),
            ],
        )

        self.assertEqual(len(windows), 1)
        self.assertLess(windows[0][1], 6.0)

    def test_english_party_pickup_can_include_named_destination_direction(self) -> None:
        windows = _detect_journey_transition_windows(
            _clip("english-party-destination", duration=12.0),
            [
                TranscriptCue(1.0, 3.0, "We picked up our family."),
                TranscriptCue(3.0, 5.0, "Everyone is ready."),
                TranscriptCue(5.0, 7.0, "We're headed to New York."),
            ],
        )

        self.assertEqual(len(windows), 1)
        self.assertGreaterEqual(windows[0][1], 7.0)

    def test_transition_boundaries_keep_mandatory_padding_compact(self) -> None:
        windows = _merge_overlapping_windows(
            [
                (0.0, 30.0, "speech"),
                (13.25, 17.75, "transition"),
            ]
        )

        self.assertEqual(
            windows,
            [
                (0.0, 13.25, "speech"),
                (13.25, 17.75, "transition"),
                (17.75, 30.0, "speech"),
            ],
        )
        transition_duration = sum(
            end - start
            for start, end, origin in windows
            if origin == "transition"
        )
        self.assertEqual(transition_duration, 4.5)
        self.assertEqual(sum(end - start for start, end, _ in windows), 30.0)
        self.assertTrue(
            all(left[1] <= right[0] for left, right in zip(windows, windows[1:]))
        )

    def test_transition_boundary_absorbs_a_tiny_ordinary_sliver(self) -> None:
        windows = _merge_overlapping_windows(
            [
                (0.0, 6.0, "speech"),
                (0.2, 2.7, "transition"),
            ]
        )

        self.assertEqual(
            windows,
            [
                (0.0, 2.7, "transition"),
                (2.7, 6.0, "speech"),
            ],
        )
        self.assertAlmostEqual(2.7 - 2.5, 0.2)
        self.assertEqual(sum(end - start for start, end, _ in windows), 6.0)
        self.assertTrue(all(end - start >= 0.75 for start, end, _ in windows))

    def test_repeated_nearby_transition_subtype_is_deduplicated(self) -> None:
        for index, transport_phrase in enumerate(("트램을", "택시를")):
            transport = transport_phrase[:-1]
            with self.subTest(transport=transport):
                windows = _detect_journey_transition_windows(
                    _clip(f"repeated-transport-{index}", duration=60.0),
                    [
                        TranscriptCue(20.0, 21.0, f"{transport_phrase} 탔습니다"),
                        TranscriptCue(30.0, 31.0, f"{transport_phrase} 탔어요"),
                    ],
                )

                self.assertEqual(len(windows), 1)
                self.assertEqual(windows[0][2], "transition")

    def test_distant_same_transition_subtype_is_not_deduplicated(self) -> None:
        windows = _detect_journey_transition_windows(
            _clip("distant-tram-boardings", duration=150.0),
            [
                TranscriptCue(20.0, 21.0, "트램을 탔습니다"),
                TranscriptCue(100.0, 101.0, "트램을 탔어요"),
            ],
        )

        self.assertEqual(len(windows), 2)
        self.assertTrue(all(origin == "transition" for _, _, origin in windows))

    def test_customs_destination_arrival_can_span_adjacent_cues(self) -> None:
        windows = _detect_journey_transition_windows(
            _clip("customs-arrival", duration=20.0),
            [
                TranscriptCue(1.0, 3.5, "샌프란시스코에 도착했습니다"),
                TranscriptCue(3.5, 5.5, "무사히 세관도 통과하고"),
            ],
        )

        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0][2], "transition")

    def test_interview_and_transition_merge_into_disjoint_dual_role_candidate(self) -> None:
        clip = _clip("interview-transition", duration=60.0)
        cues = [
            TranscriptCue(20.0, 22.0, "이번 여행 어땠나요?"),
            TranscriptCue(
                22.2,
                27.0,
                "할머니 할아버지 태우고 공항으로 가는 길이 정말 좋았어요",
            ),
        ]
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["project"]["name"] = "interview-transition-test"
        config["editing"]["preserve_family_interviews"] = True
        config["analysis"]["max_candidates_per_clip"] = 1

        with tempfile.TemporaryDirectory() as temporary:
            paths = ProjectPaths(Path(temporary), "project")
            paths.ensure()
            with (
                patch("video_summary.candidates.load_transcript", return_value=cues),
                patch("video_summary.candidates.analyze_visual_signals", return_value=[]),
                patch("video_summary.candidates.extract_frame"),
            ):
                payload = build_candidates(paths, [clip], config)

        event = payload["required_events"][0]
        event_candidates = [
            candidate
            for candidate in payload["candidates"]
            if event["event_id"] in candidate["required_event_ids"]
        ]
        self.assertTrue(
            any(
                {"interview", "transition", "journey"}.issubset(candidate["roles"])
                for candidate in event_candidates
            )
        )
        ordered = sorted(payload["candidates"], key=lambda item: item["start"])
        self.assertTrue(
            all(left["end"] <= right["start"] for left, right in zip(ordered, ordered[1:]))
        )

    def test_detects_korean_and_english_review_qa(self) -> None:
        korean = _detect_interview_events(
            _clip("korean"),
            [
                TranscriptCue(10.0, 12.0, "이번 여행에서 뭐가 제일 재미있었어요?"),
                TranscriptCue(12.4, 13.2, "수영"),
            ],
        )
        english = _detect_interview_events(
            _clip("english"),
            [
                TranscriptCue(5.0, 6.5, "How was the hotel?"),
                TranscriptCue(7.0, 9.0, "It was great and I loved the pool."),
            ],
        )

        self.assertEqual(len(korean), 1)
        self.assertEqual(len(english), 1)
        self.assertIn("spoken_answer", korean[0].signals)
        self.assertIn("spoken_answer", english[0].signals)

    def test_detects_multiple_questions_and_short_answer_in_one_cue(self) -> None:
        events = _detect_interview_events(
            _clip(),
            [
                TranscriptCue(
                    10.0,
                    15.0,
                    "여행 어때요? 뭐가 제일 재밌었어요? 수영",
                ),
            ],
        )

        self.assertEqual(len(events), 1)
        self.assertIn("same_cue_answer", events[0].signals)

        answer_before_next_question = _detect_interview_events(
            _clip("multi-question"),
            [
                TranscriptCue(
                    4.0,
                    9.0,
                    "여행 어땠나요? 좋았어요. 뭐가 제일 좋았나요?",
                )
            ],
        )
        self.assertEqual(len(answer_before_next_question), 1)
        self.assertLess(answer_before_next_question[0].start, 4.0)

    def test_interview_keywords_without_qa_do_not_trigger(self) -> None:
        events = _detect_interview_events(
            _clip(),
            [
                TranscriptCue(1.0, 2.5, "가족 인터뷰를 찍어 볼게요"),
                TranscriptCue(3.0, 4.5, "카메라 보고 한마디 해 주세요"),
            ],
        )

        self.assertEqual(events, [])

    def test_recording_direction_without_a_review_answer_does_not_trigger(self) -> None:
        events = _detect_interview_events(
            _clip("direction-without-answer", duration=18.285),
            [
                TranscriptCue(1.55, 3.55, "그라운 프라저에서 어땠나요?"),
                TranscriptCue(3.55, 5.55, "여기 호텔 어땠나요?"),
                TranscriptCue(5.55, 7.55, "여기요."),
                TranscriptCue(7.55, 9.55, "여기 보고 얘기해주세요."),
                TranscriptCue(9.55, 11.55, "엄마 차 보는거에요?"),
                TranscriptCue(11.55, 13.55, "네."),
                TranscriptCue(15.55, 17.55, "이제 다음 호텔로 갑시다."),
            ],
        )

        self.assertEqual(events, [])

    def test_phuquoc_recording_direction_keeps_the_complete_required_answer(self) -> None:
        clip = _clip("clip_dda1039f30cd9e79", duration=18.285)
        cues = [
            TranscriptCue(1.55, 3.55, "그라운 프라저에서 어땠나요?"),
            TranscriptCue(3.55, 5.55, "여기 호텔 어땠나요?"),
            TranscriptCue(5.55, 7.55, "여기요."),
            TranscriptCue(7.55, 9.55, "여기 보고 얘기해주세요."),
            TranscriptCue(9.55, 11.55, "엄마 차 보는거에요?"),
            TranscriptCue(11.55, 13.55, "네."),
            TranscriptCue(13.55, 15.55, "좋았어요."),
            TranscriptCue(15.55, 17.55, "이제 다음 호텔로 갑시다."),
        ]
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["project"]["name"] = "phuquoc-direction-test"
        config["editing"]["preserve_family_interviews"] = True

        with tempfile.TemporaryDirectory() as temporary:
            paths = ProjectPaths(Path(temporary), "project")
            paths.ensure()
            with (
                patch("video_summary.candidates.load_transcript", return_value=cues),
                patch("video_summary.candidates.analyze_visual_signals", return_value=[]),
                patch("video_summary.candidates.extract_frame"),
            ):
                payload = build_candidates(paths, [clip], config)

        self.assertEqual(len(payload["required_events"]), 1)
        event = payload["required_events"][0]
        self.assertEqual((event["start"], event["end"]), (1.2, 15.55))
        self.assertIn("interview_recording_direction", event["signals"])
        tagged = [
            candidate
            for candidate in payload["candidates"]
            if event["event_id"] in candidate["required_event_ids"]
        ]
        self.assertEqual(
            event["candidate_ids"],
            [candidate["candidate_id"] for candidate in tagged],
        )
        self.assertLessEqual(tagged[0]["start"], event["start"])
        self.assertGreaterEqual(tagged[-1]["end"], event["end"])
        self.assertTrue(
            all(
                left["end"] == right["start"]
                for left, right in zip(tagged, tagged[1:])
            )
        )

    def test_casual_present_tense_questions_do_not_trigger(self) -> None:
        events = _detect_interview_events(
            _clip(),
            [
                TranscriptCue(1.0, 2.5, "아침 식사 어때요?"),
                TranscriptCue(3.0, 4.0, "맛있어요"),
                TranscriptCue(6.0, 8.0, "로보 택시 탄 기분이 어때요?"),
                TranscriptCue(8.2, 9.5, "아무도 없어서 신기해요"),
            ],
        )

        self.assertEqual(events, [])

    def test_declarative_weather_description_does_not_trigger(self) -> None:
        events = _detect_interview_events(
            _clip("weather-narrative"),
            [
                TranscriptCue(1.0, 3.0, "그날 날씨가 어땠어요."),
                TranscriptCue(3.2, 6.0, "아침부터 비가 오고 바람도 많이 불었어요."),
            ],
        )

        self.assertEqual(events, [])

    def test_detects_natural_family_interview_prompts(self) -> None:
        examples = [
            ("이번 여행 소감?", "가족과 함께해서 정말 좋았어요."),
            ("한마디 해 주세요", "다음에도 다 같이 오고 싶어요."),
            ("이번 여행에서 기억에 남는 건?", "바다에서 수영한 게 기억에 남아요."),
            ("How was Okinawa?", "It was amazing and I loved the beach."),
        ]

        for index, (question, answer) in enumerate(examples):
            with self.subTest(question=question):
                events = _detect_interview_events(
                    _clip(f"natural-prompt-{index}"),
                    [
                        TranscriptCue(1.0, 3.0, question),
                        TranscriptCue(3.2, 6.0, answer),
                    ],
                )

                self.assertEqual(len(events), 1)
                self.assertIn("spoken_answer", events[0].signals)

    def test_detects_destination_review_and_stay_question(self) -> None:
        destination_review = _detect_interview_events(
            _clip("destination"),
            [
                TranscriptCue(1.0, 3.0, "오키나와 어땠나요?"),
                TranscriptCue(3.3, 5.0, "즐겁고 맛있었습니다"),
            ],
        )
        stay_review = _detect_interview_events(
            _clip("stay"),
            [
                TranscriptCue(10.0, 12.0, "며칠 더 있고 싶나요?"),
                TranscriptCue(12.5, 14.0, "조금 더 있고 싶어요"),
            ],
        )

        self.assertEqual(len(destination_review), 1)
        self.assertEqual(len(stay_review), 1)
        informal = _detect_interview_events(
            _clip("informal"),
            [
                TranscriptCue(1.0, 2.0, "미국은 어땠어요?"),
                TranscriptCue(2.3, 4.0, "정말 재미있었어요"),
            ],
        )
        self.assertEqual(len(informal), 1)

    def test_answer_only_followup_clip_is_linked_to_nearby_interview(self) -> None:
        anchor = _clip(
            "anchor",
            duration=15.0,
            captured_at="2026-08-20T10:00:00+09:00",
        )
        followup = _clip(
            "followup",
            duration=12.0,
            captured_at="2026-08-20T10:00:20+09:00",
        )
        events = _detect_family_interview_events(
            [anchor, followup],
            {
                anchor.clip_id: [
                    TranscriptCue(2.0, 4.0, "삿포로 여행 재밌었나요?"),
                    TranscriptCue(4.3, 5.5, "네 재미있었어요"),
                ],
                followup.clip_id: [
                    TranscriptCue(1.0, 5.0, "일본에 와서 눈 많이 보니까 재미있었어요. 끝!"),
                ],
            },
        )

        self.assertEqual(len(events[anchor.clip_id]), 1)
        self.assertEqual(len(events[followup.clip_id]), 1)
        continuation = events[followup.clip_id][0]
        self.assertEqual(continuation.anchor_event_id, events[anchor.clip_id][0].event_id)
        self.assertIn("cross_clip_continuation", continuation.signals)

    def test_required_interview_windows_survive_limit_and_persist_provenance(self) -> None:
        clip = _clip("long-interview", duration=70.0)
        cues = [
            TranscriptCue(20.0, 22.0, "여행에서 뭐가 제일 재미있었어요?"),
            TranscriptCue(22.5, 26.0, "수영이 재미있었어요"),
            TranscriptCue(27.0, 35.0, "바다에서도 오래 놀았어요"),
            TranscriptCue(36.0, 44.0, "가족이랑 같이 있어서 더 좋았어요"),
            TranscriptCue(45.0, 53.0, "다음에도 다시 오고 싶어요"),
        ]
        signals = [
            {"time": 2.0, "brightness": 0.5, "contrast": 0.5, "motion": 0.1},
            {"time": 30.0, "brightness": 0.5, "contrast": 0.6, "motion": 0.5},
        ]
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["project"]["name"] = "interview-test"
        config["editing"]["preserve_family_interviews"] = True
        config["analysis"]["max_candidates_per_clip"] = 1

        with tempfile.TemporaryDirectory() as temporary:
            paths = ProjectPaths(Path(temporary), "project")
            paths.ensure()
            with (
                patch("video_summary.candidates.load_transcript", return_value=cues),
                patch("video_summary.candidates.analyze_visual_signals", return_value=signals),
                patch("video_summary.candidates.extract_frame"),
            ):
                payload = build_candidates(paths, [clip], config)

        self.assertEqual(payload["version"], 5)
        self.assertEqual(len(payload["required_events"]), 1)
        event = payload["required_events"][0]
        tagged = [
            candidate
            for candidate in payload["candidates"]
            if event["event_id"] in candidate["required_event_ids"]
        ]
        overlapping = [
            candidate
            for candidate in payload["candidates"]
            if candidate["end"] > event["start"] and candidate["start"] < event["end"]
        ]
        self.assertGreater(len(tagged), 1)
        self.assertEqual(event["candidate_ids"], [candidate["candidate_id"] for candidate in tagged])
        self.assertEqual(tagged, overlapping)
        self.assertTrue(all(candidate["duration"] <= 18.0 for candidate in tagged))
        self.assertTrue(all("interview" in candidate["roles"] for candidate in tagged))

    def test_explicit_interview_context_requires_entire_short_clip(self) -> None:
        clip = _clip("family-sequence", duration=70.0)
        cues = [
            TranscriptCue(1.0, 8.0, "벌써 여행 마지막 날이라 아쉽고 다시 오고 싶네요"),
            TranscriptCue(12.0, 24.0, "아빠는 날씨가 좋았고 가족이 즐거워해서 좋았습니다"),
            TranscriptCue(28.0, 40.0, "엄마도 다음에 또 오고 싶어요"),
            TranscriptCue(48.0, 50.0, "이제 아이 인터뷰 잠깐만 할게요"),
            TranscriptCue(50.0, 52.0, "이번 여행 어땠나요?"),
            TranscriptCue(52.2, 54.0, "재밌었어요"),
            TranscriptCue(55.0, 57.0, "뭐가 제일 재밌었어요?"),
            TranscriptCue(57.2, 58.0, "수영"),
            TranscriptCue(66.0, 69.0, "다음에 또 만나요"),
        ]
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["project"]["name"] = "full-interview-test"
        config["editing"]["preserve_family_interviews"] = True
        config["analysis"]["max_candidates_per_clip"] = 1

        with tempfile.TemporaryDirectory() as temporary:
            paths = ProjectPaths(Path(temporary), "project")
            paths.ensure()
            with (
                patch("video_summary.candidates.load_transcript", return_value=cues),
                patch("video_summary.candidates.analyze_visual_signals", return_value=[]),
                patch("video_summary.candidates.extract_frame"),
            ):
                payload = build_candidates(paths, [clip], config)

        self.assertEqual(len(payload["required_events"]), 1)
        event = payload["required_events"][0]
        self.assertEqual((event["start"], event["end"]), (0.0, 70.0))
        self.assertIn("full_clip_interview", event["signals"])
        self.assertEqual(len(payload["candidates"]), 4)
        self.assertEqual(
            event["candidate_ids"],
            [candidate["candidate_id"] for candidate in payload["candidates"]],
        )
        self.assertTrue(
            all(candidate["duration"] <= 18.0 for candidate in payload["candidates"])
        )

    def test_non_explicit_review_qa_keeps_a_narrow_required_window(self) -> None:
        clip = _clip("ordinary-review", duration=70.0)
        events = _detect_interview_events(
            clip,
            [
                TranscriptCue(50.0, 52.0, "이번 여행 어땠나요?"),
                TranscriptCue(52.2, 54.0, "재밌었어요"),
            ],
        )

        self.assertEqual(len(events), 1)
        self.assertGreater(events[0].start, 0.0)
        self.assertLess(events[0].end, clip.duration)

    def test_long_continuous_answer_is_not_truncated_at_forty_five_seconds(self) -> None:
        clip = _clip("long-answer", duration=130.0)
        cues = [TranscriptCue(1.0, 3.0, "이번 여행 어땠나요?")]
        cues.extend(
            TranscriptCue(start, start + 2.5, f"여행 소감 답변 {index}")
            for index, start in enumerate(range(3, 99, 3), start=1)
        )

        events = _detect_interview_events(clip, cues)

        self.assertEqual(len(events), 1)
        self.assertGreater(events[0].end, 98.0)
        self.assertLess(events[0].end, clip.duration)

    def test_natural_pause_does_not_split_an_interview_answer(self) -> None:
        events = _detect_interview_events(
            _clip("paused-answer", duration=40.0),
            [
                TranscriptCue(1.0, 3.0, "이번 여행 어땠나요?"),
                TranscriptCue(3.5, 6.0, "정말 재미있었어요"),
                TranscriptCue(13.0, 16.0, "특히 바다가 기억에 남아요"),
            ],
        )

        self.assertEqual(len(events), 1)
        self.assertGreater(events[0].end, 16.0)

    def test_preserves_answered_reason_followup_in_same_interview(self) -> None:
        events = _detect_interview_events(
            _clip("multi-turn", duration=30.0),
            [
                TranscriptCue(1.0, 3.0, "이번 여행 어땠나요?"),
                TranscriptCue(3.2, 5.0, "정말 좋았어요"),
                TranscriptCue(5.5, 7.0, "왜 그렇게 생각해?"),
                TranscriptCue(7.2, 10.0, "가족과 수영해서 좋았어요"),
            ],
        )

        self.assertEqual(len(events), 1)
        self.assertGreater(events[0].end, 10.0)
        self.assertIn("multi_turn_followup", events[0].signals)
        self.assertIn("spoken_followup_answer", events[0].signals)

    def test_unrelated_question_does_not_extend_confirmed_interview(self) -> None:
        events = _detect_interview_events(
            _clip("interview-then-chatter", duration=30.0),
            [
                TranscriptCue(1.0, 3.0, "이번 여행 어땠나요?"),
                TranscriptCue(3.2, 5.0, "정말 좋았어요"),
                TranscriptCue(5.5, 7.0, "저녁은 뭐 먹을까요?"),
                TranscriptCue(7.2, 10.0, "라면을 먹어요"),
            ],
        )

        self.assertEqual(len(events), 1)
        self.assertLessEqual(events[0].end, 5.5)
        self.assertNotIn("multi_turn_followup", events[0].signals)

    def test_overlapping_unrelated_question_does_not_cut_confirmed_answer(self) -> None:
        events = _detect_interview_events(
            _clip("overlapping-transcript-cues", duration=30.0),
            [
                TranscriptCue(1.0, 3.0, "이번 여행 어땠나요?"),
                TranscriptCue(3.2, 8.0, "가족과 함께해서 정말 즐거웠어요"),
                TranscriptCue(7.0, 9.0, "저녁은 뭐 먹을까요?"),
                TranscriptCue(9.2, 11.0, "라면을 먹어요"),
            ],
        )

        self.assertEqual(len(events), 1)
        self.assertGreaterEqual(events[0].end, 8.0)
        self.assertNotIn("multi_turn_followup", events[0].signals)

    def test_minimal_answers_count_only_after_review_questions(self) -> None:
        examples = [
            ("삿포로 여행 재밌었나요?", "네"),
            ("삿포로 여행 재밌었나요?", "응"),
            ("삿포로 여행 재밌었나요?", "모르겠어"),
            ("삿포로 여행 재밌었나요?", "모르겠어요"),
            ("Did you enjoy the trip?", "Yes"),
        ]
        for index, (question, answer) in enumerate(examples):
            with self.subTest(answer=answer):
                events = _detect_interview_events(
                    _clip(f"affirmative-{index}"),
                    [
                        TranscriptCue(1.0, 3.0, question),
                        TranscriptCue(3.2, 4.0, answer),
                    ],
                )
                self.assertEqual(len(events), 1)

        ordinary_chatter = _detect_interview_events(
            _clip("ordinary-affirmative"),
            [
                TranscriptCue(1.0, 3.0, "오늘 숙제 했나요?"),
                TranscriptCue(3.2, 4.0, "네"),
            ],
        )
        self.assertEqual(ordinary_chatter, [])

        for index, filler in enumerate(("음", "어", "uh")):
            with self.subTest(filler=filler):
                events = _detect_interview_events(
                    _clip(f"filler-{index}"),
                    [
                        TranscriptCue(1.0, 3.0, "삿포로 여행 재밌었나요?"),
                        TranscriptCue(3.2, 4.0, filler),
                    ],
                )
                self.assertEqual(events, [])

    def test_same_cue_reason_followup_and_answer_extend_interview(self) -> None:
        events = _detect_interview_events(
            _clip("same-cue-followup", duration=30.0),
            [
                TranscriptCue(1.0, 3.0, "이번 여행 어땠나요?"),
                TranscriptCue(3.2, 5.0, "정말 좋았어요"),
                TranscriptCue(5.5, 9.0, "왜 그렇게 생각해? 가족과 와서요"),
            ],
        )

        self.assertEqual(len(events), 1)
        self.assertGreater(events[0].end, 9.0)
        self.assertIn("multi_turn_followup", events[0].signals)
        self.assertIn("spoken_followup_answer", events[0].signals)

    def test_uncertainty_answers_are_valid_for_open_ended_prompts(self) -> None:
        examples = [
            ("뭐가 제일 좋았나요?", "모르겠어"),
            ("이번 여행 소감?", "모르겠어요"),
            ("What was your favorite part?", "I don't know"),
            ("What was your favorite part?", "not sure"),
        ]
        for index, (question, answer) in enumerate(examples):
            with self.subTest(question=question, answer=answer):
                events = _detect_interview_events(
                    _clip(f"open-uncertainty-{index}"),
                    [
                        TranscriptCue(1.0, 3.0, question),
                        TranscriptCue(3.2, 4.2, answer),
                    ],
                )
                self.assertEqual(len(events), 1)

        for index, (question, answer) in enumerate(
            [
                ("뭐가 제일 좋았나요?", "네"),
                ("이번 여행 소감?", "응"),
                ("What was your favorite part?", "Yes"),
            ]
        ):
            with self.subTest(open_question=question, affirmative=answer):
                events = _detect_interview_events(
                    _clip(f"open-affirmative-{index}"),
                    [
                        TranscriptCue(1.0, 3.0, question),
                        TranscriptCue(3.2, 4.2, answer),
                    ],
                )
                self.assertEqual(events, [])

    def test_long_explicit_interview_expands_only_contiguous_run(self) -> None:
        for duration in (90.001, 120.0):
            with self.subTest(duration=duration):
                events = _detect_interview_events(
                    _clip(f"long-explicit-{duration}", duration=duration),
                    [
                        TranscriptCue(10.0, 14.0, "엄마는 가족과 와서 좋았습니다"),
                        TranscriptCue(20.0, 22.0, "이제 아이 인터뷰를 해 볼게요"),
                        TranscriptCue(22.2, 24.0, "이번 여행 어땠나요?"),
                        TranscriptCue(24.2, 27.0, "수영해서 정말 좋았어요"),
                        TranscriptCue(32.0, 35.0, "다음에도 또 오고 싶어요"),
                        TranscriptCue(80.0, 82.0, "이제 짐을 챙겨서 출발합니다"),
                    ],
                )

                self.assertEqual(len(events), 1)
                self.assertLessEqual(events[0].start, 9.65)
                self.assertGreater(events[0].end, 35.0)
                self.assertLess(events[0].end, 40.0)
                self.assertIn("contiguous_interview_run", events[0].signals)

    def test_distant_explicit_context_does_not_expand_long_clip_event(self) -> None:
        events = _detect_interview_events(
            _clip("distant-context", duration=120.0),
            [
                TranscriptCue(1.0, 3.0, "나중에 가족 인터뷰도 찍어 볼게요"),
                TranscriptCue(40.0, 42.0, "이번 여행 어땠나요?"),
                TranscriptCue(42.2, 44.0, "정말 좋았어요"),
            ],
        )

        self.assertEqual(len(events), 1)
        self.assertGreater(events[0].start, 39.0)
        self.assertLess(events[0].end, 45.0)
        self.assertNotIn("contiguous_interview_run", events[0].signals)

    def test_transitive_chatter_cannot_extend_interview_beyond_max_span(self) -> None:
        cues = [
            TranscriptCue(1.0, 3.0, "이번 여행 어땠나요?"),
            TranscriptCue(3.2, 5.0, "정말 좋았어요"),
        ]
        cues.extend(
            TranscriptCue(float(start), float(start + 2), f"계속 이어지는 이야기 {start}")
            for start in range(10, 591, 10)
        )

        events = _detect_interview_events(
            _clip("transitive-chatter", duration=600.0),
            cues,
        )

        self.assertEqual(len(events), 1)
        self.assertLessEqual(events[0].end - events[0].start, 180.0)
        self.assertLess(events[0].end, 200.0)

    def test_transitively_connected_distant_context_does_not_expand_event(self) -> None:
        cues = [TranscriptCue(1.0, 3.0, "가족 인터뷰를 시작할게요")]
        cues.extend(
            TranscriptCue(float(start), float(start + 2), f"여행 이야기 {start}")
            for start in range(10, 500, 10)
        )
        cues.extend(
            [
                TranscriptCue(500.0, 502.0, "이번 여행 어땠나요?"),
                TranscriptCue(502.2, 504.0, "정말 좋았어요"),
            ]
        )
        cues.extend(
            TranscriptCue(float(start), float(start + 2), f"마무리 이야기 {start}")
            for start in range(510, 591, 10)
        )

        events = _detect_interview_events(
            _clip("distant-connected-context", duration=600.0),
            cues,
        )

        self.assertEqual(len(events), 1)
        self.assertGreater(events[0].start, 499.0)
        self.assertLessEqual(events[0].end - events[0].start, 180.0)
        self.assertNotIn("contiguous_interview_run", events[0].signals)

    def test_explicit_long_run_expansion_is_capped(self) -> None:
        cues = [
            TranscriptCue(float(start), float(start + 2), f"여행 이야기 {start}")
            for start in range(10, 480, 10)
        ]
        cues.extend(
            [
                TranscriptCue(480.0, 482.0, "이제 가족 인터뷰를 할게요"),
                TranscriptCue(490.0, 492.0, "카메라를 보면서 차례로 이야기해요"),
                TranscriptCue(500.0, 502.0, "이번 여행 어땠나요?"),
                TranscriptCue(502.2, 504.0, "정말 좋았어요"),
            ]
        )
        cues.extend(
            TranscriptCue(float(start), float(start + 2), f"마무리 이야기 {start}")
            for start in range(510, 591, 10)
        )

        events = _detect_interview_events(
            _clip("bounded-connected-context", duration=600.0),
            cues,
        )

        self.assertEqual(len(events), 1)
        self.assertLessEqual(events[0].start, 480.0)
        self.assertGreaterEqual(events[0].end, 504.0)
        self.assertLessEqual(events[0].end - events[0].start, 180.0)
        self.assertGreater(events[0].start, 300.0)
        self.assertIn("contiguous_interview_run", events[0].signals)

    def test_unrecognized_leading_question_is_kept_with_the_review_qa(self) -> None:
        events = _detect_interview_events(
            _clip("leading-question", duration=30.0),
            [
                TranscriptCue(5.0, 7.0, "즐거웠나요?"),
                TranscriptCue(7.0, 9.0, "오키나와 어땠나요?"),
                TranscriptCue(9.0, 11.0, "재미있었어요"),
            ],
        )

        self.assertEqual(len(events), 1)
        self.assertLessEqual(events[0].start, 4.65)

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

    def test_candidate_limit_does_not_truncate_full_source_partition(self) -> None:
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
        self.assertGreater(len(windows), 3)
        self.assertEqual(windows[0][0], 0.0)
        self.assertEqual(windows[-1][1], 90.0)
        self.assertTrue(
            all(
                abs(left[1] - right[0]) <= 0.001
                for left, right in zip(windows, windows[1:])
            )
        )
        self.assertAlmostEqual(
            sum(end - start for start, end, _origin in windows),
            clip.duration,
            places=6,
        )

    def test_full_coverage_partition_fills_every_gap_with_bounded_units(self) -> None:
        windows = _partition_full_clip_coverage(
            [
                (5.0, 9.0, "speech"),
                (20.0, 26.0, "meal"),
                (50.0, 55.0, "closer"),
            ],
            60.0,
        )

        self.assertEqual(windows[0][0], 0.0)
        self.assertEqual(windows[-1][1], 60.0)
        self.assertTrue(all(end > start for start, end, _origin in windows))
        self.assertTrue(all(end - start <= 18.0 for start, end, _origin in windows))
        self.assertTrue(
            all(
                abs(left[1] - right[0]) <= 0.001
                for left, right in zip(windows, windows[1:])
            )
        )
        self.assertAlmostEqual(
            sum(end - start for start, end, _origin in windows),
            60.0,
            places=6,
        )
        self.assertEqual(
            [(start, end, origin) for start, end, origin in windows if origin != "coverage"],
            [
                (5.0, 9.0, "speech"),
                (20.0, 26.0, "meal"),
                (50.0, 55.0, "closer"),
            ],
        )

    def test_exclude_ranges_match_relative_or_base_name_and_require_overlap(self) -> None:
        clip = _clip("private", duration=20.0)
        clip.relative_path = "DAY 2/private.mp4"
        rules = [
            {
                "match": "**/private.mp4",
                "start": 4.0,
                "end": 9.0,
                "reason": "옷을 갈아입는 사적 장면",
            }
        ]

        self.assertEqual(
            _candidate_exclusion_reason(clip, 6.0, 8.0, rules),
            "옷을 갈아입는 사적 장면",
        )
        self.assertIsNone(_candidate_exclusion_reason(clip, 9.001, 12.0, rules))

        basename_rule = [{"match": "private.mp4", "reason": "전체 제외"}]
        self.assertEqual(
            _candidate_exclusion_reason(clip, 0.0, 2.0, basename_rule),
            "전체 제외",
        )

        root_clip = _clip("private", duration=20.0)
        self.assertEqual(
            _candidate_exclusion_reason(root_clip, 6.0, 8.0, rules),
            "옷을 갈아입는 사적 장면",
        )

    def test_story_event_metadata_protects_body_and_allows_only_silent_coverage_to_speed(self) -> None:
        first = Candidate(
            candidate_id="setup",
            clip_id="clip",
            day_key="2026-08-20",
            travel_day=1,
            start=0.0,
            end=5.0,
            captured_at="2026-08-20T10:00:00+09:00",
            transcript="식당에 도착했어요",
            roles=["food", "dialogue"],
            score=0.8,
            speech_ratio=0.6,
            motion_score=0.2,
            visual_quality=0.7,
            location="식당",
            frame_path="frames/setup.jpg",
            required_meal_context_ids=["meal_1:setup"],
            origin="meal_setup",
        )
        body = Candidate.from_dict(
            {
                **first.to_dict(),
                "candidate_id": "body",
                "start": 5.0,
                "end": 12.0,
                "captured_at": "2026-08-20T10:00:05+09:00",
                "transcript": "",
                "speech_ratio": 0.0,
                "required_meal_context_ids": [],
                "required_meal_event_ids": ["meal_1"],
                "origin": "meal",
            }
        )
        bridge = Candidate.from_dict(
            {
                **first.to_dict(),
                "candidate_id": "bridge",
                "start": 12.0,
                "end": 30.0,
                "captured_at": "2026-08-20T10:00:12+09:00",
                "transcript": "",
                "roles": ["moment"],
                "speech_ratio": 0.0,
                "required_meal_context_ids": [],
                "origin": "coverage",
            }
        )

        _assign_story_event_metadata([first, body, bridge])

        self.assertEqual(len({first.story_event_id, body.story_event_id, bridge.story_event_id}), 1)
        self.assertEqual(first.story_stage, "setup")
        self.assertEqual(body.story_stage, "body")
        self.assertEqual(first.speed_policy, "protected_1x")
        self.assertEqual(body.speed_policy, "protected_1x")
        self.assertEqual(bridge.story_stage, "bridge")
        self.assertEqual(bridge.speed_policy, "allow_fast")

    def test_story_event_metadata_splits_separate_activities_after_five_minutes(self) -> None:
        first = Candidate(
            candidate_id="pool-a",
            clip_id="pool-a",
            day_key="2026-08-20",
            travel_day=1,
            start=0.0,
            end=10.0,
            captured_at="2026-08-20T10:00:00+09:00",
            transcript="수영장에서 놀아요",
            roles=["fun", "dialogue"],
            score=0.8,
            speech_ratio=0.3,
            motion_score=0.5,
            visual_quality=0.7,
            location=None,
            frame_path="frames/pool-a.jpg",
        )
        second = Candidate.from_dict(
            {
                **first.to_dict(),
                "candidate_id": "pool-b",
                "clip_id": "pool-b",
                "captured_at": "2026-08-20T10:06:00+09:00",
                "frame_path": "frames/pool-b.jpg",
            }
        )

        _assign_story_event_metadata([first, second])

        self.assertNotEqual(first.story_event_id, second.story_event_id)

    def test_story_event_metadata_splits_strong_activity_changes_inside_one_clip(self) -> None:
        food = Candidate(
            candidate_id="food",
            clip_id="continuous",
            day_key="2026-08-20",
            travel_day=1,
            start=0.0,
            end=5.0,
            captured_at="2026-08-20T10:00:00+09:00",
            transcript="식사 중",
            roles=["food", "dialogue"],
            score=0.8,
            speech_ratio=0.3,
            motion_score=0.2,
            visual_quality=0.7,
            location=None,
            frame_path="frames/food.jpg",
        )
        fun = Candidate.from_dict(
            {
                **food.to_dict(),
                "candidate_id": "fun",
                "start": 5.0,
                "end": 10.0,
                "captured_at": "2026-08-20T10:00:05+09:00",
                "transcript": "놀이 시작",
                "roles": ["fun", "dialogue"],
                "frame_path": "frames/fun.jpg",
            }
        )
        scenery = Candidate.from_dict(
            {
                **food.to_dict(),
                "candidate_id": "scenery",
                "start": 10.0,
                "end": 15.0,
                "captured_at": "2026-08-20T10:00:10+09:00",
                "transcript": "바깥 풍경",
                "roles": ["scenery", "dialogue"],
                "frame_path": "frames/scenery.jpg",
            }
        )

        _assign_story_event_metadata([food, fun, scenery])

        self.assertEqual(len({food.story_event_id, fun.story_event_id, scenery.story_event_id}), 3)

    def test_story_event_metadata_keeps_same_meal_arc_across_silent_coverage(self) -> None:
        setup = Candidate(
            candidate_id="meal-setup",
            clip_id="meal",
            day_key="2026-08-20",
            travel_day=1,
            start=0.0,
            end=5.0,
            captured_at="2026-08-20T10:00:00+09:00",
            transcript="식당에 도착",
            roles=["food", "dialogue"],
            score=0.8,
            speech_ratio=0.3,
            motion_score=0.2,
            visual_quality=0.7,
            location=None,
            frame_path="frames/meal-setup.jpg",
            required_meal_context_ids=["meal_1:setup"],
            origin="meal_setup",
        )
        bridge = Candidate.from_dict(
            {
                **setup.to_dict(),
                "candidate_id": "meal-bridge",
                "start": 5.0,
                "end": 10.0,
                "captured_at": "2026-08-20T10:00:05+09:00",
                "transcript": "",
                "roles": ["moment"],
                "speech_ratio": 0.0,
                "required_meal_context_ids": [],
                "origin": "coverage",
                "frame_path": "frames/meal-bridge.jpg",
            }
        )
        body = Candidate.from_dict(
            {
                **setup.to_dict(),
                "candidate_id": "meal-body",
                "start": 10.0,
                "end": 15.0,
                "captured_at": "2026-08-20T10:00:10+09:00",
                "transcript": "아이들이 즐거워해요",
                "roles": ["fun", "dialogue"],
                "required_meal_context_ids": [],
                "required_meal_event_ids": ["meal_1"],
                "origin": "meal",
                "frame_path": "frames/meal-body.jpg",
            }
        )
        closure = Candidate.from_dict(
            {
                **setup.to_dict(),
                "candidate_id": "meal-closure",
                "start": 15.0,
                "end": 20.0,
                "captured_at": "2026-08-20T10:00:15+09:00",
                "transcript": "식사 뒤 풍경",
                "roles": ["scenery", "dialogue"],
                "required_meal_context_ids": ["meal_1:closure"],
                "origin": "meal_closure",
                "frame_path": "frames/meal-closure.jpg",
            }
        )

        _assign_story_event_metadata([setup, bridge, body, closure])

        self.assertEqual(
            len(
                {
                    setup.story_event_id,
                    bridge.story_event_id,
                    body.story_event_id,
                    closure.story_event_id,
                }
            ),
            1,
        )

    def test_story_event_metadata_splits_distinct_meal_and_interview_ids(self) -> None:
        meal_one = Candidate(
            candidate_id="meal-one",
            clip_id="continuous",
            day_key="2026-08-20",
            travel_day=1,
            start=0.0,
            end=5.0,
            captured_at="2026-08-20T10:00:00+09:00",
            transcript="첫 번째 식사",
            roles=["food", "dialogue"],
            score=0.8,
            speech_ratio=0.3,
            motion_score=0.2,
            visual_quality=0.7,
            location=None,
            frame_path="frames/meal-one.jpg",
            required_meal_event_ids=["meal_1"],
            origin="meal",
        )
        meal_two = Candidate.from_dict(
            {
                **meal_one.to_dict(),
                "candidate_id": "meal-two",
                "start": 5.0,
                "end": 10.0,
                "captured_at": "2026-08-20T10:00:05+09:00",
                "transcript": "두 번째 식사",
                "required_meal_event_ids": ["meal_2"],
                "frame_path": "frames/meal-two.jpg",
            }
        )
        interview_one = Candidate.from_dict(
            {
                **meal_one.to_dict(),
                "candidate_id": "interview-one",
                "start": 10.0,
                "end": 15.0,
                "captured_at": "2026-08-20T10:00:10+09:00",
                "transcript": "첫 번째 인터뷰",
                "roles": ["interview", "dialogue"],
                "required_meal_event_ids": [],
                "required_event_ids": ["interview_1"],
                "origin": "interview",
                "frame_path": "frames/interview-one.jpg",
            }
        )
        interview_two = Candidate.from_dict(
            {
                **interview_one.to_dict(),
                "candidate_id": "interview-two",
                "start": 15.0,
                "end": 20.0,
                "captured_at": "2026-08-20T10:00:15+09:00",
                "transcript": "두 번째 인터뷰",
                "required_event_ids": ["interview_2"],
                "frame_path": "frames/interview-two.jpg",
            }
        )

        _assign_story_event_metadata([meal_one, meal_two, interview_one, interview_two])

        self.assertNotEqual(meal_one.story_event_id, meal_two.story_event_id)
        self.assertNotEqual(interview_one.story_event_id, interview_two.story_event_id)


if __name__ == "__main__":
    unittest.main()
