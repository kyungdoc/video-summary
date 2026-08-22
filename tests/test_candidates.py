from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from video_summary.candidates import (
    INTERVIEW_DETECTION_POLICY_VERSION,
    VISUAL_SIGNAL_POLICY_VERSION,
    _detect_family_interview_events,
    _detect_interview_events,
    _candidate_cache_key,
    _candidate_location,
    _candidate_windows,
    _merge_overlapping_windows,
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

        self.assertEqual(payload["version"], 2)
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

    def test_candidate_limit_keeps_start_and_end_coverage(self) -> None:
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
        self.assertEqual(len(windows), 3)
        self.assertEqual(windows[0][0], 0.0)
        self.assertEqual(windows[-1][1], 90.0)


if __name__ == "__main__":
    unittest.main()
