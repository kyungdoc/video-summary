from __future__ import annotations

import math
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .media import VISUAL_SIGNAL_POLICY_VERSION, analyze_visual_signals, extract_frame, load_clips, resolve_location
from .models import Candidate, Clip, TranscriptCue
from .project import ProjectPaths
from .state import StateStore
from .transcribe import load_transcript
from .utils import VideoSummaryError, file_fingerprint, print_status, read_json, stable_hash, unique_preserving_order, write_json


JOURNEY_WORDS = {
    "출발", "도착", "공항", "비행기", "기차", "버스", "택시", "렌터카", "이동", "체크인", "체크아웃",
    "숙소", "호텔", "리조트", "귀가", "집으로", "departure", "arrival", "airport", "train", "bus", "hotel",
}
FUN_WORDS = {
    "웃", "ㅋㅋ", "ㅎㅎ", "대박", "헐", "우와", "미쳤", "신나", "재밌", "최고", "놀라", "웃기",
    "haha", "wow", "amazing", "funny", "awesome",
}
FOOD_WORDS = {
    "맛", "먹", "식당", "조식", "점심", "저녁", "카페", "커피", "디저트", "간식", "음식", "메뉴",
    "breakfast", "lunch", "dinner", "cafe", "coffee", "food", "delicious",
}
SCENERY_WORDS = {
    "바다", "해변", "등산", "산 정상", "노을", "야경", "풍경", "전망", "거리", "시장", "수영장", "하늘", "구름",
    "beach", "mountain", "sunset", "view", "street", "market", "pool",
}
MAX_CANDIDATE_DURATION_SECONDS = 18.0
INTERVIEW_DETECTION_POLICY_VERSION = 3
INTERVIEW_ANSWER_WAIT_SECONDS = 15.0
INTERVIEW_CONTINUATION_GAP_SECONDS = 12.0
INTERVIEW_EVENT_MAX_SPAN_SECONDS = 180.0
INTERVIEW_CONTEXT_EVENT_MAX_DISTANCE_SECONDS = 90.0


@dataclass(frozen=True, slots=True)
class _InterviewEvent:
    event_id: str
    clip_id: str
    start: float
    end: float
    confidence: float
    signals: tuple[str, ...]
    anchor_event_id: str | None = None


_INTERVIEW_QUESTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ko_trip_review_question",
        re.compile(
            r"(?:이번\s*)?(?:여행|여행지|휴가|오늘|하루|호텔|숙소|리조트|수영장|체험|관광|일정)"
            r".{0,28}?(?:어땠(?:습니까|나요|어요|어)?|어때(?:요)?|어떠(?:셨|했))"
        ),
    ),
    (
        "ko_enjoyment_question",
        re.compile(
            r"(?:여행|여행지|휴가|오늘|하루|호텔|숙소|리조트|수영장|체험|관광|일정)"
            r".{0,28}?(?:재밌었|재미있었|좋았|즐거웠|맛있었|신났)"
            r"(?:나요|니|습니까|어\s*[?？]|어요\s*[?？])"
        ),
    ),
    (
        "ko_evaluation_question",
        re.compile(
            r"어땠(?:나요|습니까)(?:\s*[?？])?"
            r"|어땠(?:어요|어)\s*[?？]"
        ),
    ),
    (
        "ko_favorite_question",
        re.compile(
            r"(?:뭐|무엇|어디|어떤|누가|언제).{0,32}?(?:제일|가장).{0,32}?"
            r"(?:좋|재밌|재미|기억|맛있|인상|신나|행복|추천)"
            r"|(?:제일|가장).{0,32}?(?:뭐|무엇|어디|어떤).{0,32}?"
            r"(?:좋|재밌|재미|기억|맛있|인상|신나|행복|추천)?"
            r"|(?:뭐|무엇|어디|어떤)(?:가|이|를|을)?.{0,20}?"
            r"(?:좋았|재밌었|재미있었|기억에\s*남|맛있었|인상적)"
        ),
    ),
    (
        "ko_trip_memory_question",
        re.compile(
            r"(?:여행|휴가|오늘|이번).{0,36}?(?:기억에\s*남|좋았|재밌었|재미있었|인상적)"
            r".{0,24}?(?:뭐|무엇|어디|어떤)"
            r"|(?:이번\s*)?(?:여행|휴가)(?:에서|중|의|은|는)?\s*.{0,24}?"
            r"기억에\s*남는\s*(?:건|것(?:은|이)?|게)(?:\s*[?？])?"
        ),
    ),
    (
        "ko_reflection_question",
        re.compile(
            r"(?:소감|느낌).{0,24}?(?:어때|어땠|말해|들려|한마디|뭐|어떤)"
            r"|(?:(?:이번\s*)?(?:여행|휴가)(?:에서|의|은|는)?\s*)?"
            r"(?:소감|느낌)(?:은|이|도)?\s*[?？]"
            r"|(?:한\s*마디|한마디)\s*(?:해|말해)\s*(?:주(?:세요|십시오)|줘)"
            r"|(?:몇\s*점|점수).{0,20}?(?:줄|줘|인가|이야|입니까)"
            r"|(?:다시|또).{0,24}?(?:오고|가고|하고|먹고).{0,16}?"
            r"(?:싶(?:니|나요|습니까)|(?:싶어|싶어요)\s*[?？])"
        ),
    ),
    (
        "ko_stay_or_return_question",
        re.compile(
            r"(?:며칠|얼마나).{0,24}?(?:더\s*)?(?:있고|머물고).{0,12}?싶(?:나요|니|습니까)"
            r"|(?:가고|오고|있고|머물고|돌아가고).{0,12}?싶(?:나요|니|습니까)"
        ),
    ),
    (
        "en_trip_review_question",
        re.compile(
            r"\bhow\s+(?:was|is|did\s+you\s+like)\s+"
            r"(?:(?:your|the|this|our)\s+)?"
            r"(?:trip|travel|vacation|holiday|day|hotel|resort|pool|tour|experience|flight)\b"
        ),
    ),
    (
        "en_favorite_question",
        re.compile(
            r"\bwhat\b.{0,42}?\b(?:favorite|favourite|best|most\s+fun|liked\s+most|remember\s+most)\b"
            r"|\b(?:favorite|favourite|best)\s+(?:part|thing|place|food|memory).{0,24}?\bwhat\b"
        ),
    ),
    (
        "en_reflection_question",
        re.compile(
            r"\b(?:did\s+you\s+enjoy|would\s+you\s+(?:come\s+back|visit\s+again|recommend))\b"
        ),
    ),
    (
        "en_open_reflection_question",
        re.compile(
            r"\btell\s+(?:us|me).{0,28}?\b(?:favorite|favourite|best|thoughts?)\b"
        ),
    ),
)

_EN_DESTINATION_REVIEW_PATTERN = re.compile(
    r"\b[Hh]ow\s+(?:was|is)\s+"
    r"[A-Z][A-Za-z'’-]*(?:\s+[A-Z][A-Za-z'’-]*){0,3}\s*[?？]"
)

_INTERVIEW_FOLLOWUP_QUESTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ko_reason_followup",
        re.compile(
            r"^(?:(?:그건|그게)\s*)?왜(?:\s+그렇게)?"
            r"(?:\s+(?:생각|느끼|느껴|좋|재밌|재미있)[^?？]*)?"
            r"\s*(?:요)?\s*[?？]"
        ),
    ),
    (
        "en_reason_followup",
        re.compile(
            r"^why(?:\s+(?:do|did)\s+you\s+(?:think|feel)\s+(?:so|that))?\s*[?？]"
            r"|^what\s+(?:made|makes)\s+you\s+(?:say|think|feel)\s+that\s*[?？]"
        ),
    ),
)

_INTERVIEW_CONTEXT_PATTERN = re.compile(
    r"(?:인터뷰|소감|한마디|카메라\s*보고|interview|on\s+camera)",
)

_INTERVIEW_RECORDING_DIRECTION_PATTERN = re.compile(
    r"(?:카메라|렌즈|여기|저기|이쪽|저쪽).{0,12}?"
    r"(?:보고|보면서|봐).{0,18}?(?:말|얘기|이야기|대답)"
    r"|\b(?:look|face).{0,18}\b(?:camera|lens)\b",
)

_INTERVIEW_SETUP_BRIDGE_PATTERN = re.compile(
    r"^(?:여기|저기|이쪽|저쪽|이거|저거)(?:를|을|요)?$",
)

_INTERVIEW_SEQUENCE_END_PATTERN = re.compile(
    r"^(?:자\s*[,，]?\s*)?(?:이제|그럼|그러면).{0,30}?"
    r"(?:갑시다|가자|출발|이동|마치|끝내|종료)",
)

_INTERVIEW_CONTINUATION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ko_evaluation_answer",
        re.compile(
            r"(?:제일|가장|와서|가서|해서|보니까|먹어\s*보니까).{0,42}?"
            r"(?:재밌었|재미있었|좋았|맛있었|기억에\s*남|최고였|즐거웠|행복했|신났)"
            r"|(?:재밌었|재미있었|좋았|맛있었|기억에\s*남|최고였|즐거웠|행복했|신났)"
            r".{0,28}?(?:여행|휴가|하루|곳|장소|음식|체험|끝)"
            r"|(?:또|다시).{0,24}?(?:오고|가고|하고|먹고).{0,16}?싶"
        ),
    ),
    (
        "en_evaluation_answer",
        re.compile(
            r"\b(?:my\s+(?:favorite|favourite)|the\s+best)\b.{0,36}?\bwas\b"
            r"|\bi\s+(?:really\s+)?(?:liked|loved|enjoyed)\b"
            r"|\bit\s+was\s+(?:really\s+)?(?:fun|great|amazing|awesome|memorable)\b"
            r"|\b(?:come\s+back|visit\s+again|do\s+it\s+again)\b"
        ),
    ),
)

_NON_ANSWER_TEXTS = {
    "어",
    "어어",
    "음",
    "으음",
    "네",
    "예",
    "응",
    "아",
    "글쎄",
    "모르겠어",
    "모르겠어요",
    "yes",
    "yeah",
    "yep",
    "ok",
    "okay",
    "um",
    "uh",
    "hmm",
}

_AFFIRMATIVE_ANSWER_TEXTS = {
    "네",
    "예",
    "응",
    "yes",
    "yeah",
    "yep",
}

_PERSONAL_SHORT_ANSWER_TEXTS = {
    "모르겠어",
    "모르겠어요",
    "i don't know",
    "i don’t know",
    "not sure",
}

_AFFIRMATIVE_QUESTION_SIGNALS = {
    "ko_trip_review_question",
    "ko_enjoyment_question",
    "ko_evaluation_question",
    "ko_stay_or_return_question",
    "en_trip_review_question",
    "en_reflection_question",
    "en_destination_review_question",
}

_UNCERTAINTY_QUESTION_SIGNALS = _AFFIRMATIVE_QUESTION_SIGNALS | {
    "ko_favorite_question",
    "ko_trip_memory_question",
    "ko_reflection_question",
    "en_favorite_question",
    "en_open_reflection_question",
}


def build_candidates(
    paths: ProjectPaths,
    clips: list[Clip],
    config: dict[str, Any],
    *,
    force: bool = False,
) -> dict[str, Any]:
    analysis = config["analysis"]
    interval = float(analysis.get("sample_interval_seconds", 3.0))
    max_per_clip = int(analysis.get("max_candidates_per_clip", 8))
    cache_key = _candidate_cache_key(paths, clips, config)
    state = StateStore(paths.state)
    if not force and paths.candidates.exists() and state.is_complete("candidates", cache_key):
        print_status("candidates: 캐시 사용")
        return read_json(paths.candidates)

    state.mark_running("candidates", cache_key)
    try:
        candidates: list[Candidate] = []
        required_interview_events: list[tuple[Clip, _InterviewEvent]] = []
        preserve_family_interviews = _preserve_family_interviews(config)
        cues_by_clip = {
            clip.clip_id: load_transcript(paths, clip.clip_id)
            for clip in clips
            if clip.duration > 0
        }
        interview_events_by_clip = (
            _detect_family_interview_events(clips, cues_by_clip)
            if preserve_family_interviews
            else {}
        )
        for index, clip in enumerate(clips, start=1):
            if clip.duration <= 0:
                continue
            print_status(f"candidates {index}/{len(clips)}: {Path(clip.path).name}")
            cues = cues_by_clip[clip.clip_id]
            signals = analyze_visual_signals(paths, clip, interval, force=force)
            interview_events = interview_events_by_clip.get(clip.clip_id, [])
            required_interview_events.extend((clip, event) for event in interview_events)
            windows = _candidate_windows(
                clip,
                cues,
                signals,
                max_per_clip,
                required_events=interview_events,
            )
            for start, end, origin in windows:
                text = _window_transcript(cues, start, end)
                roles = _roles(text, clip, start, end, origin)
                required_event_ids = [
                    event.event_id
                    for event in interview_events
                    if _ranges_overlap(start, end, event.start, event.end)
                ]
                if required_event_ids:
                    roles = unique_preserving_order(["interview", *roles])
                motion, quality = _window_signals(signals, start, end)
                speech_duration = sum(
                    max(0.0, min(cue.end, end) - max(cue.start, start))
                    for cue in cues
                    if cue.end > start and cue.start < end
                )
                speech_ratio = min(1.0, speech_duration / max(0.1, end - start))
                location = _candidate_location(clip, text, config.get("locations", []))
                score = _score_candidate(roles, speech_ratio, motion, quality, start, end, clip.duration)
                candidate_id = "cand_" + stable_hash(
                    {
                        "clip_id": clip.clip_id,
                        "fingerprint": clip.fingerprint,
                        "start": round(start, 2),
                        "end": round(end, 2),
                    },
                    length=18,
                )
                frame_path = paths.frames / f"{candidate_id}.jpg"
                if force or not frame_path.exists():
                    extract_frame(clip, (start + end) / 2.0, frame_path)
                clip_captured_at = datetime.fromisoformat(clip.captured_at)
                captured_at = (
                    clip_captured_at.astimezone(timezone.utc)
                    + timedelta(seconds=start)
                ).astimezone(clip_captured_at.tzinfo).isoformat()
                candidates.append(
                    Candidate(
                        candidate_id=candidate_id,
                        clip_id=clip.clip_id,
                        day_key=clip.day_key,
                        travel_day=clip.travel_day,
                        start=round(start, 3),
                        end=round(end, 3),
                        captured_at=captured_at,
                        transcript=text,
                        roles=roles,
                        score=round(score, 5),
                        speech_ratio=round(speech_ratio, 5),
                        motion_score=round(motion, 5),
                        visual_quality=round(quality, 5),
                        location=location,
                        frame_path=str(frame_path.relative_to(paths.root)),
                        required_event_ids=required_event_ids,
                    )
                )

        candidates.sort(key=lambda item: (_candidate_timestamp(item), item.candidate_id))
        if not candidates:
            raise VideoSummaryError("편집 후보를 만들지 못했습니다.")
        required_events = _required_events_payload(required_interview_events, candidates)
        payload = {
            "version": 2,
            "project": config["project"]["name"],
            "cache_key": cache_key,
            "candidate_set_hash": stable_hash([candidate.to_dict() for candidate in candidates], length=32),
            "count": len(candidates),
            "days": _day_summary(candidates),
            "required_events": required_events,
            "candidates": [candidate.to_dict() for candidate in candidates],
        }
        write_json(paths.candidates, payload)
        state.mark_complete(
            "candidates",
            cache_key,
            {
                "candidate_count": len(candidates),
                "required_event_count": len(required_events),
            },
        )
        return payload
    except BaseException as exc:
        state.mark_failed("candidates", cache_key, str(exc))
        raise


def load_candidates(paths: ProjectPaths, config: dict[str, Any] | None = None) -> list[Candidate]:
    if not paths.candidates.exists():
        raise VideoSummaryError("먼저 analyze 또는 run을 실행하세요.")
    payload = read_json(paths.candidates)
    if config is not None:
        clips = load_clips(paths, config)
        if payload.get("cache_key") != _candidate_cache_key(paths, clips, config):
            raise VideoSummaryError("분석 설정이나 전사가 변경되었습니다. analyze를 다시 실행하세요.")
    return [Candidate.from_dict(item) for item in payload.get("candidates", [])]


def _candidate_cache_key(paths: ProjectPaths, clips: list[Clip], config: dict[str, Any]) -> str:
    analysis = config["analysis"]
    transcript_keys: list[dict[str, str | None] | None] = []
    for clip in clips:
        path = paths.transcripts / f"{clip.clip_id}.json"
        if path.exists():
            transcript_keys.append(
                {
                    "cache_key": read_json(path).get("cache_key"),
                    "fingerprint": file_fingerprint(path),
                }
            )
        else:
            transcript_keys.append(None)
    return stable_hash(
        {
            "version": 11,
            "visual_signal_policy": VISUAL_SIGNAL_POLICY_VERSION,
            "family_interview_detection": {
                "policy": INTERVIEW_DETECTION_POLICY_VERSION,
                "preserve": _preserve_family_interviews(config),
            },
            "project": config["project"]["name"],
            "clips": [
                (
                    clip.clip_id,
                    clip.fingerprint,
                    clip.captured_at,
                    clip.day_key,
                    clip.travel_day,
                    clip.location,
                )
                for clip in clips
            ],
            "transcripts": transcript_keys,
            "transcription_settings": {
                "language": config["project"].get("language", "auto"),
                "asr_backend": analysis.get("asr_backend", "auto"),
                "asr_model": analysis.get("asr_model", "small"),
                "whisper_cpp_model": analysis.get("whisper_cpp_model") or os.environ.get("WHISPER_CPP_MODEL"),
                "whisper_cpp_vad_model": analysis.get("whisper_cpp_vad_model") or os.environ.get("WHISPER_CPP_VAD_MODEL"),
                "offline": analysis.get("offline", False),
            },
            "interval": float(analysis.get("sample_interval_seconds", 3.0)),
            "max_per_clip": int(analysis.get("max_candidates_per_clip", 8)),
            "locations": config.get("locations", []),
        }
    )


def _preserve_family_interviews(config: dict[str, Any]) -> bool:
    return config.get("editing", {}).get("preserve_family_interviews", True) is True


def _detect_family_interview_events(
    clips: list[Clip],
    cues_by_clip: dict[str, list[TranscriptCue]],
) -> dict[str, list[_InterviewEvent]]:
    events_by_clip = {
        clip.clip_id: _detect_interview_events(clip, cues_by_clip.get(clip.clip_id, []))
        for clip in clips
        if clip.duration > 0
    }
    anchors = [
        (clip, event)
        for clip in clips
        for event in events_by_clip.get(clip.clip_id, [])
    ]
    ordered_clips = sorted(clips, key=_clip_start_timestamp)
    for clip in ordered_clips:
        if clip.duration <= 0 or clip.duration > 90.0 or events_by_clip.get(clip.clip_id):
            continue
        clip_start = _clip_start_timestamp(clip)
        nearby_anchors = [
            (anchor_clip, anchor_event)
            for anchor_clip, anchor_event in anchors
            if anchor_clip.day_key == clip.day_key
            and _clip_start_timestamp(anchor_clip) < clip_start
            and -5.0
            <= clip_start - (_clip_start_timestamp(anchor_clip) + anchor_clip.duration)
            <= 120.0
        ]
        if not nearby_anchors:
            continue
        _, anchor_event = max(
            nearby_anchors,
            key=lambda item: _clip_start_timestamp(item[0]),
        )
        continuation = _detect_interview_continuation(
            clip,
            cues_by_clip.get(clip.clip_id, []),
            anchor_event,
        )
        if continuation is not None:
            events_by_clip[clip.clip_id] = [continuation]
    return events_by_clip


def _detect_interview_events(clip: Clip, cues: list[TranscriptCue]) -> list[_InterviewEvent]:
    """Find high-confidence travel-review Q&A without identifying any speaker."""
    ordered = sorted(
        (cue for cue in cues if cue.end > cue.start and cue.text.strip()),
        key=lambda cue: (cue.start, cue.end),
    )
    events: list[_InterviewEvent] = []
    for question_index, question_cue in enumerate(ordered):
        questions = _interview_questions(question_cue.text)
        if not questions:
            continue
        question_signal, _, question_match_end = questions[-1]
        answer_index: int | None = None
        answer_followed_recording_direction = False
        for option_index, (signal, _, match_end) in enumerate(questions):
            next_question_start = (
                questions[option_index + 1][1]
                if option_index + 1 < len(questions)
                else None
            )
            same_cue_answer = _answer_after_question(
                question_cue.text,
                match_end,
                stop_at=next_question_start,
            )
            if _is_substantive_interview_answer(
                same_cue_answer,
                allow_affirmative=_allows_affirmative_answer(signal),
                allow_uncertainty=_allows_uncertainty_answer(signal),
            ):
                question_signal = signal
                question_match_end = match_end
                answer_index = question_index
                break
        if answer_index is None:
            recording_direction_seen = False
            for index in range(question_index + 1, len(ordered)):
                answer_cue = ordered[index]
                if answer_cue.start - question_cue.end > INTERVIEW_ANSWER_WAIT_SECONDS:
                    break
                if _is_interview_sequence_end(answer_cue.text):
                    break
                if _is_interview_recording_direction(answer_cue.text):
                    recording_direction_seen = True
                    continue
                if _looks_like_question(answer_cue.text):
                    continue
                if (
                    not recording_direction_seen
                    and _is_interview_setup_bridge(answer_cue.text)
                    and index + 1 < len(ordered)
                    and ordered[index + 1].start - answer_cue.end
                    <= INTERVIEW_CONTINUATION_GAP_SECONDS
                    and _is_interview_recording_direction(
                        ordered[index + 1].text
                    )
                ):
                    continue
                if (
                    recording_direction_seen
                    and _normalized_interview_text(answer_cue.text)
                    in _AFFIRMATIVE_ANSWER_TEXTS
                ):
                    continue
                if _is_substantive_interview_answer(
                    answer_cue.text,
                    allow_affirmative=_allows_affirmative_answer(question_signal),
                    allow_uncertainty=_allows_uncertainty_answer(question_signal),
                ):
                    answer_index = index
                    answer_followed_recording_direction = recording_direction_seen
                    break
        if answer_index is None:
            continue

        answer_cue = ordered[answer_index]
        anchor_end = max(question_cue.end, answer_cue.end)
        start = max(0.0, question_cue.start - 0.35)
        next_question_start = question_cue.start
        for previous in reversed(ordered[:question_index]):
            if next_question_start - previous.end > INTERVIEW_CONTINUATION_GAP_SECONDS:
                break
            if not _looks_like_question(previous.text):
                break
            proposed_start = max(0.0, previous.start - 0.35)
            if anchor_end - proposed_start > INTERVIEW_EVENT_MAX_SPAN_SECONDS:
                break
            start = proposed_start
            next_question_start = previous.start
        event_end = anchor_end
        span_limit = start + INTERVIEW_EVENT_MAX_SPAN_SECONDS
        last_cue = answer_cue
        followup_signals: list[str] = []
        unrelated_question_start: float | None = None
        continuation_index = answer_index + 1
        while continuation_index < len(ordered):
            continuation = ordered[continuation_index]
            if continuation.start >= span_limit:
                break
            if continuation.start - last_cue.end > INTERVIEW_CONTINUATION_GAP_SECONDS:
                break
            if _is_interview_sequence_end(continuation.text):
                unrelated_question_start = continuation.start
                break
            if _looks_like_question(continuation.text):
                followup = _interview_followup_question(continuation.text)
                if followup is None:
                    unrelated_question_start = continuation.start
                    break
                followup_signal, followup_match_end = followup
                followup_answer_index: int | None = None
                same_cue_followup_answer = _answer_after_question(
                    continuation.text,
                    followup_match_end,
                )
                if _is_substantive_interview_answer(same_cue_followup_answer):
                    followup_answer_index = continuation_index
                else:
                    for index in range(continuation_index + 1, len(ordered)):
                        possible_answer = ordered[index]
                        if possible_answer.start >= span_limit:
                            break
                        if (
                            possible_answer.start - continuation.end
                            > INTERVIEW_ANSWER_WAIT_SECONDS
                        ):
                            break
                        if _looks_like_question(possible_answer.text):
                            break
                        if _is_substantive_interview_answer(possible_answer.text):
                            followup_answer_index = index
                            break
                if followup_answer_index is None:
                    unrelated_question_start = continuation.start
                    break
                followup_answer = ordered[followup_answer_index]
                event_end = max(event_end, continuation.end, followup_answer.end)
                last_cue = followup_answer
                followup_signals.extend(
                    ["multi_turn_followup", followup_signal, "spoken_followup_answer"]
                )
                continuation_index = followup_answer_index + 1
                continue
            event_end = max(event_end, continuation.end)
            last_cue = continuation
            continuation_index += 1

        end = min(
            clip.duration,
            event_end + 0.75,
            start + INTERVIEW_EVENT_MAX_SPAN_SECONDS,
            max(event_end, unrelated_question_start)
            if unrelated_question_start is not None
            else clip.duration,
        )
        if end - start < 0.75:
            continue
        signals = [question_signal, "spoken_answer", *followup_signals]
        if answer_followed_recording_direction:
            signals.append("interview_recording_direction")
        if answer_index == question_index:
            signals.append("same_cue_answer")
        nearby_context = " ".join(
            cue.text.casefold()
            for cue in ordered[max(0, question_index - 1) : min(len(ordered), answer_index + 2)]
        )
        if _INTERVIEW_CONTEXT_PATTERN.search(nearby_context):
            signals.append("interview_context")
        confidence = min(
            0.99,
            0.90
            + (0.04 if "interview_context" in signals else 0.0)
            + (0.03 if "same_cue_answer" in signals else 0.0),
        )
        event_id = "interview_" + stable_hash(
            {
                "policy": INTERVIEW_DETECTION_POLICY_VERSION,
                "clip_id": clip.clip_id,
                "fingerprint": clip.fingerprint,
                "start": round(start, 2),
                "end": round(end, 2),
            },
            length=18,
        )
        events.append(
            _InterviewEvent(
                event_id=event_id,
                clip_id=clip.clip_id,
                start=round(start, 3),
                end=round(end, 3),
                confidence=round(confidence, 3),
                signals=tuple(unique_preserving_order(signals)),
            )
        )
    merged = _merge_interview_events(clip, events)
    return _expand_explicit_interview_clip(clip, ordered, merged)


def _detect_interview_continuation(
    clip: Clip,
    cues: list[TranscriptCue],
    anchor_event: _InterviewEvent,
) -> _InterviewEvent | None:
    ordered = sorted(
        (cue for cue in cues if cue.end > cue.start and cue.text.strip()),
        key=lambda cue: (cue.start, cue.end),
    )
    for answer_index, answer_cue in enumerate(ordered):
        normalized = " ".join(answer_cue.text.casefold().split())
        signal = next(
            (
                name
                for name, pattern in _INTERVIEW_CONTINUATION_PATTERNS
                if pattern.search(normalized) is not None
            ),
            None,
        )
        if signal is None or not _is_substantive_interview_answer(answer_cue.text):
            continue
        start = max(0.0, answer_cue.start - 0.35)
        event_end = answer_cue.end
        last_cue = answer_cue
        for continuation in ordered[answer_index + 1 :]:
            if _looks_like_question(continuation.text):
                break
            if continuation.start - last_cue.end > INTERVIEW_CONTINUATION_GAP_SECONDS:
                break
            event_end = max(event_end, continuation.end)
            last_cue = continuation
        end = min(clip.duration, event_end + 0.75)
        if end - start < 0.75:
            return None
        event_id = "interview_" + stable_hash(
            {
                "policy": INTERVIEW_DETECTION_POLICY_VERSION,
                "clip_id": clip.clip_id,
                "fingerprint": clip.fingerprint,
                "start": round(start, 2),
                "end": round(end, 2),
                "anchor_event_id": anchor_event.event_id,
            },
            length=18,
        )
        return _InterviewEvent(
            event_id=event_id,
            clip_id=clip.clip_id,
            start=round(start, 3),
            end=round(end, 3),
            confidence=0.9,
            signals=("cross_clip_continuation", signal, "spoken_answer"),
            anchor_event_id=anchor_event.event_id,
        )
    return None


def _interview_question(text: str) -> tuple[str, int] | None:
    questions = _interview_questions(text)
    if not questions:
        return None
    signal, _, end = questions[0]
    return signal, end


def _interview_questions(text: str) -> list[tuple[str, int, int]]:
    case_preserving = " ".join(text.split())
    normalized = case_preserving.casefold()
    matches = sorted(
        [
            (signal, match.start(), match.end())
            for signal, pattern in _INTERVIEW_QUESTION_PATTERNS
            for match in pattern.finditer(normalized)
        ]
        + [
            ("en_destination_review_question", match.start(), match.end())
            for match in _EN_DESTINATION_REVIEW_PATTERN.finditer(case_preserving)
        ],
        key=lambda item: (item[2] - item[1], item[1], item[2]),
    )
    selected: list[tuple[str, int, int]] = []
    for candidate in matches:
        if any(
            min(candidate[2], current[2]) - max(candidate[1], current[1]) > 0
            for current in selected
        ):
            continue
        selected.append(candidate)
    return sorted(selected, key=lambda item: (item[1], item[2], item[0]))


def _answer_after_question(
    text: str,
    match_end: int,
    *,
    stop_at: int | None = None,
) -> str:
    normalized = " ".join(text.casefold().split())
    question_mark = min(
        (
            index
            for index in (normalized.find("?", match_end), normalized.find("？", match_end))
            if index >= 0 and (stop_at is None or index < stop_at)
        ),
        default=-1,
    )
    answer_start = question_mark + 1 if question_mark >= 0 else match_end
    answer_end = len(normalized) if stop_at is None else stop_at
    return normalized[answer_start:answer_end].strip(" \t\r\n,.;:!?？~-—")


def _looks_like_question(text: str) -> bool:
    normalized = " ".join(text.casefold().split()).strip()
    return (
        _interview_question(normalized) is not None
        or "?" in normalized
        or "？" in normalized
        or re.search(r"(?:나요|니|습니까|까요)\s*[.!…]*$", normalized) is not None
    )


def _normalized_interview_text(text: str) -> str:
    return " ".join(text.casefold().split()).strip(" \t\r\n,.;:!?？~-—")


def _is_interview_recording_direction(text: str) -> bool:
    return (
        _INTERVIEW_RECORDING_DIRECTION_PATTERN.search(
            _normalized_interview_text(text)
        )
        is not None
    )


def _is_interview_setup_bridge(text: str) -> bool:
    return (
        _INTERVIEW_SETUP_BRIDGE_PATTERN.fullmatch(
            _normalized_interview_text(text)
        )
        is not None
    )


def _is_interview_sequence_end(text: str) -> bool:
    return (
        _INTERVIEW_SEQUENCE_END_PATTERN.search(
            _normalized_interview_text(text)
        )
        is not None
    )


def _interview_followup_question(text: str) -> tuple[str, int] | None:
    normalized = " ".join(text.casefold().split()).strip()
    for signal, pattern in _INTERVIEW_FOLLOWUP_QUESTION_PATTERNS:
        match = pattern.search(normalized)
        if match is not None:
            return signal, match.end()
    return None


def _allows_affirmative_answer(question_signal: str) -> bool:
    return question_signal in _AFFIRMATIVE_QUESTION_SIGNALS


def _allows_uncertainty_answer(question_signal: str) -> bool:
    return question_signal in _UNCERTAINTY_QUESTION_SIGNALS


def _is_substantive_interview_answer(
    text: str,
    *,
    allow_affirmative: bool = False,
    allow_uncertainty: bool = False,
) -> bool:
    normalized = " ".join(text.casefold().split()).strip(" \t\r\n,.;:!?？~-—")
    if not normalized:
        return False
    if normalized in _AFFIRMATIVE_ANSWER_TEXTS:
        return allow_affirmative
    if normalized in _PERSONAL_SHORT_ANSWER_TEXTS:
        return allow_uncertainty
    if normalized in _NON_ANSWER_TEXTS:
        return False
    if _looks_like_question(normalized):
        return False
    compact = re.sub(r"[^0-9a-z가-힣]", "", normalized)
    if not compact:
        return False
    if re.fullmatch(r"(?:ㅋ+|ㅎ+|ha(?:ha)*|heh(?:e)*)", compact):
        return False
    return True


def _merge_interview_events(clip: Clip, events: list[_InterviewEvent]) -> list[_InterviewEvent]:
    groups: list[list[_InterviewEvent]] = []
    for event in sorted(events, key=lambda item: (item.start, item.end, item.event_id)):
        if (
            not groups
            or event.start - max(item.end for item in groups[-1]) > 1.25
            or event.end - min(item.start for item in groups[-1])
            > INTERVIEW_EVENT_MAX_SPAN_SECONDS
        ):
            groups.append([event])
        else:
            groups[-1].append(event)

    merged: list[_InterviewEvent] = []
    for group in groups:
        if len(group) == 1:
            merged.append(group[0])
            continue
        start = min(event.start for event in group)
        end = max(event.end for event in group)
        event_id = "interview_" + stable_hash(
            {
                "policy": INTERVIEW_DETECTION_POLICY_VERSION,
                "clip_id": clip.clip_id,
                "fingerprint": clip.fingerprint,
                "start": round(start, 2),
                "end": round(end, 2),
            },
            length=18,
        )
        merged.append(
            _InterviewEvent(
                event_id=event_id,
                clip_id=clip.clip_id,
                start=start,
                end=end,
                confidence=max(event.confidence for event in group),
                signals=tuple(
                    unique_preserving_order(
                        signal
                        for event in group
                        for signal in event.signals
                    )
                ),
            )
        )
    return merged


def _expand_explicit_interview_clip(
    clip: Clip,
    cues: list[TranscriptCue],
    events: list[_InterviewEvent],
) -> list[_InterviewEvent]:
    if not events:
        return events
    clip_text = " ".join(cue.text.casefold() for cue in cues)
    if _INTERVIEW_CONTEXT_PATTERN.search(clip_text) is None:
        return events
    if clip.duration > 90.0:
        components: list[list[TranscriptCue]] = []
        for cue in cues:
            if (
                not components
                or cue.start - components[-1][-1].end
                > INTERVIEW_CONTINUATION_GAP_SECONDS
            ):
                components.append([cue])
            else:
                components[-1].append(cue)

        expanded: list[_InterviewEvent] = []
        for event in events:
            component = next(
                (
                    group
                    for group in components
                    if any(
                        _ranges_overlap(event.start, event.end, cue.start, cue.end)
                        for cue in group
                    )
                ),
                None,
            )
            if component is None:
                expanded.append(event)
                continue
            context_cues = [
                cue
                for cue in component
                if _INTERVIEW_CONTEXT_PATTERN.search(cue.text.casefold()) is not None
                and max(0.0, event.start - cue.end, cue.start - event.end)
                <= INTERVIEW_CONTEXT_EVENT_MAX_DISTANCE_SECONDS
                and max(event.end, cue.end) - min(event.start, cue.start)
                <= INTERVIEW_EVENT_MAX_SPAN_SECONDS
            ]
            if not context_cues:
                expanded.append(event)
                continue
            context_cue = min(
                context_cues,
                key=lambda cue: (
                    max(0.0, event.start - cue.end, cue.start - event.end),
                    abs(((cue.start + cue.end) / 2.0) - ((event.start + event.end) / 2.0)),
                    cue.start,
                ),
            )
            component_start = max(0.0, component[0].start - 0.35)
            component_end = min(clip.duration, component[-1].end + 0.75)
            seed_start = min(event.start, context_cue.start)
            seed_end = max(event.end, context_cue.end)
            remaining = max(
                0.0,
                INTERVIEW_EVENT_MAX_SPAN_SECONDS - (seed_end - seed_start),
            )
            start = max(component_start, seed_start - (remaining / 2.0))
            end = min(component_end, start + INTERVIEW_EVENT_MAX_SPAN_SECONDS)
            if end < seed_end:
                end = seed_end
                start = max(
                    component_start,
                    end - INTERVIEW_EVENT_MAX_SPAN_SECONDS,
                )
            if end - start < INTERVIEW_EVENT_MAX_SPAN_SECONDS:
                start = max(
                    component_start,
                    end - INTERVIEW_EVENT_MAX_SPAN_SECONDS,
                )
                end = min(
                    component_end,
                    start + INTERVIEW_EVENT_MAX_SPAN_SECONDS,
                )
            event_id = "interview_" + stable_hash(
                {
                    "policy": INTERVIEW_DETECTION_POLICY_VERSION,
                    "clip_id": clip.clip_id,
                    "fingerprint": clip.fingerprint,
                    "start": round(start, 2),
                    "end": round(end, 2),
                    "scope": "explicit_contiguous_run",
                },
                length=18,
            )
            expanded.append(
                _InterviewEvent(
                    event_id=event_id,
                    clip_id=clip.clip_id,
                    start=round(start, 3),
                    end=round(end, 3),
                    confidence=max(0.96, event.confidence),
                    signals=tuple(
                        unique_preserving_order(
                            [
                                "explicit_interview_context",
                                "contiguous_interview_run",
                                *event.signals,
                            ]
                        )
                    ),
                    anchor_event_id=event.anchor_event_id,
                )
            )
        return _merge_interview_events(clip, expanded)

    event_id = "interview_" + stable_hash(
        {
            "policy": INTERVIEW_DETECTION_POLICY_VERSION,
            "clip_id": clip.clip_id,
            "fingerprint": clip.fingerprint,
            "start": 0.0,
            "end": round(clip.duration, 2),
            "scope": "explicit_full_clip",
        },
        length=18,
    )
    return [
        _InterviewEvent(
            event_id=event_id,
            clip_id=clip.clip_id,
            start=0.0,
            end=round(clip.duration, 3),
            confidence=max(0.96, *(event.confidence for event in events)),
            signals=tuple(
                unique_preserving_order(
                    [
                        "explicit_interview_context",
                        "full_clip_interview",
                        *(signal for event in events for signal in event.signals),
                    ]
                )
            ),
        )
    ]


def _ranges_overlap(start: float, end: float, other_start: float, other_end: float) -> bool:
    return min(end, other_end) - max(start, other_start) > 0.001


def _required_events_payload(
    events: list[tuple[Clip, _InterviewEvent]],
    candidates: list[Candidate],
) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for clip, event in events:
        candidate_ids = [
            candidate.candidate_id
            for candidate in candidates
            if event.event_id in candidate.required_event_ids
        ]
        if not candidate_ids:
            raise VideoSummaryError(
                f"필수 가족 인터뷰 후보를 만들지 못했습니다: {event.event_id}"
            )
        payload.append(
            {
                "event_id": event.event_id,
                "kind": "family_interview",
                "clip_id": clip.clip_id,
                "day_key": clip.day_key,
                "travel_day": clip.travel_day,
                "start": event.start,
                "end": event.end,
                "confidence": event.confidence,
                "signals": list(event.signals),
                "candidate_ids": candidate_ids,
                **(
                    {"anchor_event_id": event.anchor_event_id}
                    if event.anchor_event_id is not None
                    else {}
                ),
            }
        )
    return payload


def _candidate_windows(
    clip: Clip,
    cues: list[TranscriptCue],
    signals: list[dict[str, float]],
    max_per_clip: int,
    *,
    required_events: list[_InterviewEvent] | None = None,
) -> list[tuple[float, float, str]]:
    windows: list[tuple[float, float, str]] = []
    for group in _group_cues(cues):
        start = max(0.0, group[0].start - 0.35)
        end = min(clip.duration, group[-1].end + 0.75)
        start, end = _ensure_duration(
            start,
            end,
            clip.duration,
            minimum=2.5,
            maximum=MAX_CANDIDATE_DURATION_SECONDS,
        )
        _append_window(windows, (start, end, "speech"))

    boundary_duration = min(6.0, clip.duration)
    if boundary_duration >= 0.75:
        windows.append((0.0, boundary_duration, "opener"))
    if clip.duration > 7:
        windows.append((max(0.0, clip.duration - boundary_duration), clip.duration, "closer"))

    ranked_signals = sorted(
        signals,
        key=lambda sample: (
            float(sample.get("motion", 0.0)) * 0.60
            + float(sample.get("contrast", 0.0)) * 0.25
            + _brightness_quality(float(sample.get("brightness", 0.5))) * 0.15
        ),
        reverse=True,
    )
    for sample in ranked_signals[: max(6, max_per_clip * 3)]:
        center = float(sample.get("time", 0.0))
        duration = min(7.0, clip.duration)
        start = max(0.0, min(clip.duration - duration, center - duration / 2.0))
        _append_window(windows, (start, min(clip.duration, start + duration), "visual"))
    if not windows:
        windows.append((0.0, min(clip.duration, 6.0), "visual"))
    selected = _select_windows(windows, clip, cues, signals, max(1, max_per_clip))
    selected.extend(
        (event.start, event.end, "interview")
        for event in required_events or []
    )
    return _merge_overlapping_windows(selected)


def _merge_overlapping_windows(
    windows: list[tuple[float, float, str]],
    *,
    tolerance: float = 0.001,
    maximum: float = MAX_CANDIDATE_DURATION_SECONDS,
) -> list[tuple[float, float, str]]:
    """Preserve the selected union as disjoint candidates without creating long takes."""
    components: list[list[tuple[float, float, str]]] = []
    for start, end, origin in sorted(windows, key=lambda item: (item[0], item[1])):
        if not components or start > max(item[1] for item in components[-1]) + tolerance:
            components.append([(start, end, origin)])
            continue
        components[-1].append((start, end, origin))

    merged: list[tuple[float, float, str]] = []
    for component in components:
        component_start = min(item[0] for item in component)
        component_end = max(item[1] for item in component)
        duration = component_end - component_start
        part_count = max(1, math.ceil(duration / maximum))
        for part_index in range(part_count):
            start = component_start + duration * part_index / part_count
            end = component_start + duration * (part_index + 1) / part_count
            origin = _merged_window_origin(component, start, end, part_index, part_count)
            merged.append((start, end, origin))
    return merged


def _merged_window_origin(
    component: list[tuple[float, float, str]],
    start: float,
    end: float,
    part_index: int,
    part_count: int,
) -> str:
    if part_index == 0 and any(origin == "opener" for _, _, origin in component):
        return "opener"
    if part_index == part_count - 1 and any(origin == "closer" for _, _, origin in component):
        return "closer"
    priority = {"interview": 4, "speech": 3, "visual": 2, "opener": 1, "closer": 1}
    return max(
        component,
        key=lambda item: (
            max(0.0, min(end, item[1]) - max(start, item[0])),
            priority.get(item[2], 0),
        ),
    )[2]


def _select_windows(
    windows: list[tuple[float, float, str]],
    clip: Clip,
    cues: list[TranscriptCue],
    signals: list[dict[str, float]],
    limit: int,
) -> list[tuple[float, float, str]]:
    if len(windows) <= limit:
        return sorted(windows, key=lambda item: (item[0], item[1]))

    def rank(window: tuple[float, float, str]) -> float:
        start, end, origin = window
        text = _window_transcript(cues, start, end)
        roles = _roles(text, clip, start, end, origin)
        motion, quality = _window_signals(signals, start, end)
        speech = sum(
            max(0.0, min(cue.end, end) - max(cue.start, start))
            for cue in cues
            if cue.end > start and cue.start < end
        ) / max(0.1, end - start)
        return _score_candidate(roles, min(1.0, speech), motion, quality, start, end, clip.duration)

    selected: list[tuple[float, float, str]] = []
    if limit >= 2:
        opener = next((item for item in windows if item[2] == "opener"), None)
        closer = next((item for item in windows if item[2] == "closer"), None)
        if opener:
            selected.append(opener)
        if closer and closer not in selected:
            selected.append(closer)
    remaining = [item for item in windows if item not in selected]
    while len(selected) < limit and remaining:
        def coverage_rank(window: tuple[float, float, str]) -> tuple[float, float]:
            center = (window[0] + window[1]) / 2.0
            distance = min((abs(center - (item[0] + item[1]) / 2.0) for item in selected), default=clip.duration)
            coverage = min(0.18, distance / max(1.0, clip.duration) * 0.36)
            return rank(window) + coverage, -window[0]

        chosen = max(remaining, key=coverage_rank)
        selected.append(chosen)
        remaining.remove(chosen)
    return sorted(selected, key=lambda item: (item[0], item[1]))


def _group_cues(cues: list[TranscriptCue]) -> list[list[TranscriptCue]]:
    groups: list[list[TranscriptCue]] = []
    for cue in sorted(cues, key=lambda item: (item.start, item.end)):
        if cue.end <= cue.start or not cue.text.strip():
            continue
        if not groups:
            groups.append([cue])
            continue
        current = groups[-1]
        if cue.start - current[-1].end <= 1.8 and cue.end - current[0].start <= 17.0:
            current.append(cue)
        else:
            groups.append([cue])
    return groups


def _ensure_duration(start: float, end: float, total: float, minimum: float, maximum: float) -> tuple[float, float]:
    if end - start > maximum:
        end = start + maximum
    if end - start < minimum:
        missing = minimum - (end - start)
        start = max(0.0, start - missing / 2.0)
        end = min(total, end + missing / 2.0)
        if end - start < minimum:
            start = max(0.0, end - minimum)
    return start, end


def _append_window(windows: list[tuple[float, float, str]], incoming: tuple[float, float, str]) -> None:
    start, end, origin = incoming
    if end - start < 0.75:
        return
    for current_start, current_end, _ in windows:
        overlap = max(0.0, min(end, current_end) - max(start, current_start))
        shorter = min(end - start, current_end - current_start)
        if shorter > 0 and overlap / shorter >= 0.72:
            return
    windows.append((start, end, origin))


def _window_transcript(cues: list[TranscriptCue], start: float, end: float) -> str:
    texts = [cue.text.strip() for cue in cues if cue.end > start and cue.start < end and cue.text.strip()]
    return " ".join(texts)


def _window_signals(samples: list[dict[str, float]], start: float, end: float) -> tuple[float, float]:
    selected = [sample for sample in samples if start <= float(sample.get("time", 0.0)) <= end]
    if not selected and samples:
        center = (start + end) / 2.0
        selected = [min(samples, key=lambda sample: abs(float(sample.get("time", 0.0)) - center))]
    if not selected:
        return 0.0, 0.5
    motion = sum(float(sample.get("motion", 0.0)) for sample in selected) / len(selected)
    quality = sum(
        0.50 * float(sample.get("contrast", 0.0))
        + 0.50 * _brightness_quality(float(sample.get("brightness", 0.5)))
        for sample in selected
    ) / len(selected)
    return min(1.0, motion), min(1.0, quality)


def _brightness_quality(brightness: float) -> float:
    return max(0.0, 1.0 - abs(brightness - 0.52) * 2.2)


def _roles(text: str, clip: Clip, start: float, end: float, origin: str) -> list[str]:
    normalized = text.casefold()
    roles: list[str] = []
    if any(word in normalized for word in JOURNEY_WORDS) or origin in {"opener", "closer"}:
        roles.append("journey")
    if any(word in normalized for word in FUN_WORDS) or text.count("!") >= 1:
        roles.append("fun")
    if any(word in normalized for word in FOOD_WORDS):
        roles.append("food")
    if any(word in normalized for word in SCENERY_WORDS) or (not text and origin == "visual"):
        roles.append("scenery")
    if text:
        roles.append("dialogue")
    if start <= 0.5:
        roles.append("opener")
    if clip.duration - end <= 0.75:
        roles.append("closer")
    return unique_preserving_order(roles or ["moment"])


def _score_candidate(
    roles: list[str],
    speech_ratio: float,
    motion: float,
    quality: float,
    start: float,
    end: float,
    clip_duration: float,
) -> float:
    role_bonus = {
        "interview": 0.30,
        "fun": 0.22,
        "food": 0.15,
        "journey": 0.12,
        "dialogue": 0.09,
        "scenery": 0.10,
        "opener": 0.04,
        "closer": 0.04,
    }
    value = 0.18 + 0.22 * quality + 0.20 * motion + 0.18 * min(1.0, speech_ratio * 1.7)
    value += sum(role_bonus.get(role, 0.0) for role in set(roles))
    if end - start < 1.5:
        value -= 0.2
    if clip_duration > 0 and (start <= 0.25 or clip_duration - end <= 0.25):
        value += 0.03
    return max(0.0, min(1.0, value))


def _candidate_location(clip: Clip, transcript: str, rules: Any) -> str | None:
    return resolve_location(
        Path(clip.relative_path),
        clip.day_key,
        rules,
        transcript=transcript,
    )


def _day_summary(candidates: list[Candidate]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[candidate.day_key].append(candidate)
    return [
        {
            "day_key": day_key,
            "travel_day": values[0].travel_day,
            "candidate_count": len(values),
            "source_duration": round(sum(value.duration for value in values), 2),
        }
        for day_key, values in sorted(grouped.items())
    ]


def _candidate_timestamp(candidate: Candidate) -> float:
    return datetime.fromisoformat(candidate.captured_at).astimezone(timezone.utc).timestamp()


def _clip_start_timestamp(clip: Clip) -> float:
    return datetime.fromisoformat(clip.captured_at).astimezone(timezone.utc).timestamp()
