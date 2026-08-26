from __future__ import annotations

import math
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from PIL import Image

from .media import VISUAL_SIGNAL_POLICY_VERSION, analyze_visual_signals, extract_frame, load_clips, resolve_location
from .models import Candidate, Clip, TranscriptCue
from .project import ProjectPaths
from .state import StateStore
from .transcribe import load_transcript
from .utils import VideoSummaryError, file_fingerprint, print_status, read_json, stable_hash, unique_preserving_order, write_json


JOURNEY_WORDS = {
    "출발", "도착", "공항", "비행기", "기차", "버스", "택시", "렌터카", "이동", "체크인", "체크아웃",
    "숙소", "호텔", "리조트", "주차", "귀가", "집으로", "departure", "arrival", "airport", "train", "bus",
    "hotel", "parking",
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
JOURNEY_TRANSITION_POLICY_VERSION = 1
JOURNEY_TRANSITION_DEDUPE_SECONDS = 60.0
PARTY_TRANSITION_CONTEXT_POLICY_VERSION = 1
PARTY_TRANSITION_CONTEXT_MAX_SECONDS = 12.0
PARTY_TRANSITION_CONTEXT_MAX_CUES = 3
MEAL_EVENT_POLICY_VERSION = 6
MEAL_OPTION_MAX_DURATION_SECONDS = 12.0
MEAL_SETUP_HORIZON_SECONDS = 3.0 * 60.0 * 60.0
MEAL_DIRECT_CLUSTER_SECONDS = 30.0 * 60.0
MEAL_INFER_AFTER_SETUP_SECONDS = 15.0 * 60.0
MEAL_INFER_BEFORE_CLOSURE_SECONDS = 15.0 * 60.0
INTERVIEW_DETECTION_POLICY_VERSION = 3
INTERVIEW_ANSWER_WAIT_SECONDS = 15.0
INTERVIEW_CONTINUATION_GAP_SECONDS = 12.0
INTERVIEW_EVENT_MAX_SPAN_SECONDS = 180.0
INTERVIEW_CONTEXT_EVENT_MAX_DISTANCE_SECONDS = 90.0
FULL_COVERAGE_PARTITION_POLICY_VERSION = 1
STORY_EVENT_CATALOG_POLICY_VERSION = 3
MULTICAMERA_ANGLE_POLICY_VERSION = 1
STORY_EVENT_GAP_SECONDS = 5.0 * 60.0
STORY_EVENT_MAX_SPAN_SECONDS = 45.0 * 60.0


_KO_TRAVEL_PARTY = (
    r"(?:\ud560\uba38\ub2c8|\ud560\uc544\ubc84\uc9c0|\uc870\ubd80\ubaa8\ub2d8|\ubd80\ubaa8\ub2d8|\uc5c4\ub9c8|\uc544\ube60|\uc5b4\uba38\ub2c8|\uc544\ubc84\uc9c0|\uac00\uc871|\uc2dd\uad6c|\uc77c\ud589|"
    r"\uce5c\uad6c\ub4e4?|\uc544\uc774\ub4e4?|\uc560\ub4e4|\ub3d9\uc0dd|\ud615|\ub204\ub098|\uc5b8\ub2c8|\uc624\ube60|\uc0bc\ucd0c|\uc774\ubaa8|\uace0\ubaa8|\uc678\uc0bc\ucd0c)"
)
_EN_TRAVEL_PARTY = (
    r"(?:family|parents?|grandparents?|grandm(?:a|other)|grandp(?:a|father)|mom|mum|mother|"
    r"dad|father|friends?|kids?|children|brother|sister|party|group)"
)
_KO_TRANSIT_PLACE = (
    r"(?:\uacf5\ud56d|\uae30\ucc28\uc5ed|\uc804\ucca0\uc5ed|\uc9c0\ud558\ucca0\uc5ed|"
    r"(?:[^\s,，.]{1,12})(?<![\uc9c0\uad6c\uc601])\uc5ed|(?<![\uac00-\ud7a3])\uc5ed|\ubc84\uc2a4\s*\ud130\ubbf8\ub110|\ud130\ubbf8\ub110|"
    r"\ud638\ud154|\uc219\uc18c|\ub9ac\uc870\ud2b8|\ud39c\uc158|\uac8c\uc2a4\ud2b8\ud558\uc6b0\uc2a4|\uc5d0\uc5b4\ube44\uc564\ube44)"
)
_EN_TRANSIT_PLACE = (
    r"(?:airport|(?:train|railway|subway|bus)\s+station|station|terminal|hotel|resort|hostel|"
    r"guest\s*house|airbnb|lodging|accommodation)"
)
_KO_TRANSPORT = r"(?:\ube44\ud589\uae30|\ud56d\uacf5\uae30|\uae30\ucc28|\uc5f4\ucc28|\ubc84\uc2a4|\ud0dd\uc2dc|\ud398\ub9ac|\ubc30|\uc9c0\ud558\ucca0|\uc804\ucca0|\ubaa8\ub178\ub808\uc77c|\ud2b8\ub7a8)"
_EN_TRANSPORT = r"(?:flight|plane|aircraft|train|bus|taxi|ferry|boat|subway|metro|monorail|tram)"

_JOURNEY_TRANSITION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ko_party_pickup_dropoff_join",
        re.compile(
            rf"{_KO_TRAVEL_PARTY}.{{0,36}}?"
            r"(?:\ud0dc\uc6b0(?:\uace0|\ub7ec|\ub824\uace0|\uba74\uc11c|\uc5c8|\uc558|\uaca0)|\ud53d\uc5c5(?:\ud558|\ud574|\ud588)|\ub370\ub9ac(?:\uace0|\ub7ec)|\ubaa8\uc2dc(?:\uace0|\ub7ec)|"
            r"\ud569\ub958(?:\ud588|\ud574\uc11c|\ud558\uace0|\ud558\ub7ec|\ud569\ub2c8\ub2e4)|\ub9cc\ub098\uc11c\s*(?:\ud568\uaed8|\uac19\uc774)|"
            r"\ub0b4\ub824\s*\ub4dc\ub9ac|\ubaa8\uc154\ub2e4\s*\ub4dc\ub9ac|\ubc14\ub798\ub2e4\s*\ub4dc\ub9ac|\ub370\ub824\ub2e4\s*\uc8fc|\ub4dc\ub86d\s*\uc624\ud504)"
            rf"|(?:\ud53d\uc5c5(?:\ud558|\ud574|\ud588)|\ub370\ub9ac(?:\uace0|\ub7ec)|\ubaa8\uc2dc(?:\uace0|\ub7ec)|\ud569\ub958(?:\ud588|\ud574\uc11c|\ud558\uace0))"
            rf".{{0,28}}?{_KO_TRAVEL_PARTY}"
        ),
    ),
    (
        "en_party_pickup_dropoff_join",
        re.compile(
            rf"(?:pick(?:ed|ing)?\s+up|drop(?:ped|ping)?\s+off|collect(?:ed|ing)?|"
            rf"meet(?:ing)?\s+up\s+with|met\s+up\s+with|join(?:ed|ing)?(?:\s+up)?\s+with)"
            rf"\s+(?:(?:my|our|the)\s+)?{_EN_TRAVEL_PARTY}"
            rf"|(?:drop(?:ped|ping)?|pick(?:ed|ing)?)\s+(?:(?:my|our|the)\s+)?"
            rf"{_EN_TRAVEL_PARTY}\s+(?:off|up)"
            rf"|join(?:ed|ing)\s+(?:(?:my|our|the)\s+)?{_EN_TRAVEL_PARTY}"
            rf"|{_EN_TRAVEL_PARTY}.{{0,20}}?(?:joined|met)\s+(?:us|me)"
        ),
    ),
    (
        "ko_transfer_stopover",
        re.compile(
            r"(?:\ud658\uc2b9(?:\ud588|\ud569\ub2c8\ub2e4|\ud558\uace0|\ud574\uc11c|\ud558\ub7ec|\s*\uc911\uc785\ub2c8\ub2e4)|"
            r"\uacbd\uc720(?:\ud588|\ud569\ub2c8\ub2e4|\ud558\uace0|\ud574\uc11c|\s*\uc911\uc785\ub2c8\ub2e4)|"
            r"\uae30\ucc29(?:\ud588|\ud569\ub2c8\ub2e4)|\uac08\uc544\ud0d4|\uac08\uc544\ud0c0\uace0|\uac08\uc544\ud0c0\ub7ec|\uac08\uc544\ud0d1\ub2c8\ub2e4)"
        ),
    ),
    (
        "en_transfer_stopover",
        re.compile(
            rf"\b(?:we(?:'re|\s+are|\s+just)?\s+)?transferr(?:ed|ing)\s+"
            rf"(?:(?:to|between|from)\s+(?:(?:another|the|our|a)\s+)?{_EN_TRANSPORT}"
            rf"|(?:at|in|through)\s+(?:(?:the|our)\s+)?{_EN_TRANSIT_PLACE})\b"
            r"|\b(?:we(?:'re|\s+are|\s+just)?\s+)?chang(?:ed|ing)\s+(?:trains?|flights?)\b"
            r"|\b(?:we(?:'re|\s+are|\s+just)?\s+)?stopp(?:ed|ing)\s+over\b"
            rf"|\bmak(?:e|ing)\s+(?:a\s+)?connection.{{0,24}}?{_EN_TRANSIT_PLACE}\b"
            rf"|\bconnect(?:ed|ing)\s+through\s+(?:(?:the|our)\s+)?{_EN_TRANSIT_PLACE}\b"
            r"|\b(?:we\s+(?:have|had)|we've|our|the)\s+(?:a\s+)?(?:layover|stopover)\b"
        ),
    ),
    (
        "ko_rental_pickup_return",
        re.compile(
            r"(?:\ub80c\ud130\uce74|\ub80c\ud2b8\uce74|\ub300\uc5ec\ucc28).{0,28}?"
            r"(?:\uc778\uc218(?:\ud588|\ud569\ub2c8\ub2e4|\ud558\ub7ec)|\ud53d\uc5c5(?:\ud588|\ud569\ub2c8\ub2e4|\ud558\ub7ec)|\ucc3e\uc73c\ub7ec|\ubc1b\uc558|"
            r"\ubc18\ub0a9(?:\ud588|\ud569\ub2c8\ub2e4|\ud558\ub7ec)|\ub3cc\ub824\uc8fc\ub7ec|\ub3cc\ub824\uc92c)"
            r"|(?:\uc778\uc218(?:\ud588|\ud558\ub7ec)|\ud53d\uc5c5(?:\ud588|\ud558\ub7ec)|\ubc18\ub0a9(?:\ud588|\ud558\ub7ec)|\ub3cc\ub824\uc8fc\ub7ec)"
            r".{0,24}?(?:\ub80c\ud130\uce74|\ub80c\ud2b8\uce74|\ub300\uc5ec\ucc28)"
            r"|(?:\ub80c\ud130\uce74|\ub80c\ud2b8\uce74)\ub7ec\s*\uac11\ub2c8\ub2e4"
        ),
    ),
    (
        "en_rental_pickup_return",
        re.compile(
            r"\b(?:pick(?:ed|ing)?\s+up|collect(?:ed|ing)?|return(?:ed|ing)?)\s+"
            r"(?:(?:the|our|a)\s+)?rental\s+car\b"
            r"|\brental\s+car.{0,20}\b(?:pick(?:ed|ing)?\s+up|collect(?:ed|ing)?|return(?:ed|ing)?)\b"
        ),
    ),
    (
        "ko_lodging_reveal",
        re.compile(
            r"(?:\uc5ec\uae30\uac00|\uc5ec\uae30\ub294|\uc774\uacf3\uc774)\s*(?:\ubc14\ub85c\s*)?(?:\uc6b0\ub9ac(?:\uc758)?\s*)?"
            r"(?:\ud638\ud154\s*\ubc29|\uc219\uc18c|\ub9ac\uc870\ud2b8|\ud39c\uc158|\uac8c\uc2a4\ud2b8\ud558\uc6b0\uc2a4)(?:\uc785\ub2c8\ub2e4|\uc774\uc5d0\uc694|\uc608\uc694)"
            r"|(?:\uc6b0\ub9ac(?:\uc758)?\s+)(?:\ud638\ud154\s*\ubc29|\uc219\uc18c)(?:\uc785\ub2c8\ub2e4|\uc774\uc5d0\uc694|\uc608\uc694)"
            r"|(?:[0-9a-z\uac00-\ud7a3'’\-]{2,20}\s+){1,3}"
            r"(?:\ud638\ud154|\ub9ac\uc870\ud2b8|\ud39c\uc158|\uac8c\uc2a4\ud2b8\ud558\uc6b0\uc2a4)(?:\uc744|\ub97c)\s*\ucc3e\uc558\uc2b5\ub2c8\ub2e4"
        ),
    ),
    (
        "ko_lodging_checkin_checkout",
        re.compile(
            r"(?:\ud638\ud154|\uc219\uc18c|\ub9ac\uc870\ud2b8|\ud39c\uc158|\uac8c\uc2a4\ud2b8\ud558\uc6b0\uc2a4|\uc5d0\uc5b4\ube44\uc564\ube44).{0,28}?"
            r"\uccb4\ud06c\s*(?:\uc778|\uc544\uc6c3)(?:\ud588|\ud569\ub2c8\ub2e4|\ud558\uace0|\ud574\uc11c|\ud558\ub7ec|\s*\uc911\uc785\ub2c8\ub2e4|\uc744?\s*\ub9c8\uce58|\uc744?\s*\ub9c8\ucce4)"
            r"|\uccb4\ud06c\s*(?:\uc778|\uc544\uc6c3)(?:\ud588|\ud558\uace0|\ud574\uc11c|\ud558\ub7ec|\uc744?\s*\ub9c8\uce58|\uc744?\s*\ub9c8\ucce4).{0,24}?"
            r"(?:\ud638\ud154|\uc219\uc18c|\ub9ac\uc870\ud2b8|\ud39c\uc158|\uac8c\uc2a4\ud2b8\ud558\uc6b0\uc2a4|\uc5d0\uc5b4\ube44\uc564\ube44)"
        ),
    ),
    (
        "en_lodging_checkin_checkout",
        re.compile(
            rf"\b(?:checked|checking)\s+(?:in(?:to|\s+at)?|out(?:\s+of)?)\b.{{0,28}}?{_EN_TRANSIT_PLACE}\b"
            rf"|\b{_EN_TRANSIT_PLACE}\b.{{0,28}}?\b(?:checked|checking)\s+(?:in|out)\b"
        ),
    ),
    (
        "ko_customs_destination_arrival",
        re.compile(
            r"(?:[0-9a-z\uac00-\ud7a3'’\-]{2,24}(?:\s+[0-9a-z\uac00-\ud7a3'’\-]{2,24}){0,3})"
            r"(?:\uc5d0|\uc73c\ub85c)?\s*\ub3c4\ucc29(?:\ud588|\ud569\ub2c8\ub2e4|\ud588\uc5b4\uc694|\ud569\ub2c8\ub2e4)"
            r".{0,60}?(?:\uc138\uad00|\uc785\uad6d\s*\uc2ec\uc0ac).{0,14}?(?:\ud1b5\uacfc(?:\ud588|\ud569\ub2c8\ub2e4|\ud558\uace0)|\ub9c8\ucce4\uc2b5\ub2c8\ub2e4)"
            r"|(?:\uc138\uad00|\uc785\uad6d\s*\uc2ec\uc0ac).{0,14}?(?:\ud1b5\uacfc(?:\ud588|\ud569\ub2c8\ub2e4|\ud558\uace0)|\ub9c8\ucce4\uc2b5\ub2c8\ub2e4)"
            r".{0,60}?(?:[0-9a-z\uac00-\ud7a3'’\-]{2,24}(?:\s+[0-9a-z\uac00-\ud7a3'’\-]{2,24}){0,3})"
            r"(?:\uc5d0|\uc73c\ub85c)?\s*\ub3c4\ucc29(?:\ud588|\ud569\ub2c8\ub2e4|\ud588\uc5b4\uc694|\ud569\ub2c8\ub2e4)"
        ),
    ),
    (
        "en_customs_destination_arrival",
        re.compile(
            r"\barrived\s+(?:in|at)\s+(?:[a-z][a-z'’\-]{1,24})(?:\s+[a-z][a-z'’\-]{1,24}){0,3}"
            r".{0,60}?\b(?:cleared|passed|went\s+through|finished)\s+(?:customs|immigration)\b"
            r"|\b(?:cleared|passed|went\s+through|finished)\s+(?:customs|immigration)\b"
            r".{0,60}?\barrived\s+(?:in|at)\s+(?:[a-z][a-z'’\-]{1,24})(?:\s+[a-z][a-z'’\-]{1,24}){0,3}"
        ),
    ),
    (
        "ko_transit_arrival_departure",
        re.compile(
            rf"{_KO_TRANSIT_PLACE}(?:\uc5d0|\uc73c\ub85c|\uc5d0\uc11c|\uc744|\ub97c)?\s*.{{0,12}}?"
            r"(?:\ub3c4\ucc29(?:\ud588|\ud569\ub2c8\ub2e4|\ud588\uc5b4\uc694|\ud569\ub2c8\ub2e4)|\uc654\uc2b5\ub2c8\ub2e4|\uc654\uc5b4\uc694|"
            r"\ucd9c\ubc1c(?:\ud588|\ud569\ub2c8\ub2e4|\ud588\uc5b4\uc694)|\ub5a0\ub0ac|\ub5a0\ub0a9\ub2c8\ub2e4|\ub098\uc654|\ub098\uc635\ub2c8\ub2e4|"
            r"\ub0b4\ub838\uc2b5\ub2c8\ub2e4|\ub0b4\ub838\uc5b4\uc694)"
        ),
    ),
    (
        "en_transit_arrival_departure",
        re.compile(
            rf"\b(?:we(?:'ve|\s+have|\s+just)?|i(?:'ve|\s+have|\s+just)?)?\s*"
            rf"(?:arrived|made\s+it|got)\s+(?:at|to)\s+(?:(?:the|our)\s+)?{_EN_TRANSIT_PLACE}\b"
            rf"|\b(?:we(?:'re|\s+are|\s+just)?|i(?:'m|\s+am|\s+just)?)?\s*"
            rf"(?:left|leaving|departed\s+from|departing\s+from)\s+(?:(?:the|our)\s+)?{_EN_TRANSIT_PLACE}\b"
        ),
    ),
    (
        "ko_transport_boarding_alighting",
        re.compile(
            rf"{_KO_TRANSPORT}(?:\uc744|\ub97c|\uc5d0|\uc5d0\uc11c)?\s*.{{0,12}}?"
            r"(?:\ud0d4\uc2b5\ub2c8\ub2e4|\ud0d4\uc5b4\uc694|\ud0d1\ub2c8\ub2e4|\ud0d1\uc2b9\ud588\uc2b5\ub2c8\ub2e4|\ud0d1\uc2b9\ud569\ub2c8\ub2e4|\uc62c\ub77c\ud0d4|"
            r"\ud0c0\ub7ec\s*\uac11\ub2c8\ub2e4|\ub0b4\ub838\uc2b5\ub2c8\ub2e4|\ub0b4\ub838\uc5b4\uc694|\ub0b4\ub824\uc11c|\ub0b4\ub9ac\uace0|\ud558\ucc28\ud588\uc2b5\ub2c8\ub2e4|\ud558\ucc28\ud569\ub2c8\ub2e4)"
        ),
    ),
    (
        "en_transport_boarding_alighting",
        re.compile(
            rf"\b(?:we(?:'re|\s+are|\s+just|\s+have)?|i(?:'m|\s+am|\s+just|\s+have)?)?\s*"
            rf"(?:boarded|boarding|got\s+on|getting\s+on|caught|took|got\s+off|getting\s+off|"
            rf"got\s+out\s+of|getting\s+out\s+of|stepped\s+off)"
            rf"\s+(?:(?:the|a|our)\s+)?{_EN_TRANSPORT}\b"
        ),
    ),
)

_JOURNEY_TRANSITION_QUESTION_PATTERN = re.compile(
    r"[?？]|(?:\uc5b4\ub514|\uc5b8\uc81c|\ub204\uad6c|\ubb50|\ubb34\uc5c7|\uc5b4\ub290).{0,30}?(?:\uac00|\uc624|\ub3c4\ucc29|\ucd9c\ubc1c|\ud0c0|\ub0b4\ub9ac)"
    r"|^\s*(?:where|when|who|what|which|how|are|is|do|did|should|shall|can|could|will|would)\b"
)
_JOURNEY_TRANSITION_INSTRUCTION_PATTERN = re.compile(
    r"(?:\uc548\ub0b4|\ubc29\uc1a1|\uc2b9\uac1d|\uace0\uac1d|\ud0d1\uc2b9\uac1d|\uc8fc\uc758\s*\ubc14\ub78d\ub2c8\ub2e4|\ud558\uc2dc\uae30\s*\ubc14\ub78d\ub2c8\ub2e4|"
    r"\ud574\s*\uc8fc\uc138\uc694|\ud574\uc8fc\uc138\uc694|\ud558\uc138\uc694|\ud558\uc2ed\uc2dc\uc624|\ud0c0\uc138\uc694|\ub0b4\ub9ac\uc138\uc694|\uac00\uc138\uc694|\uc624\uc138\uc694)"
    r"|\b(?:please|attention|passengers?|customers?|announcement)\b"
)
_GENERIC_JOURNEY_COMMAND_PATTERN = re.compile(
    r"^(?:\uc790\s*[,，]?\s*|\uc774\uc81c\s*)?(?:\uac00\uc790|\uac11\uc2dc\ub2e4|\ucd9c\ubc1c|\ucd9c\ubc1c\ud558\uc790)[\s.!~]*$"
    r"|^(?:let['’]?s\s+go|time\s+to\s+go)[\s.!]*$"
    r"|^(?:please\s+)?(?:board|take|get\s+on|get\s+off|check\s+(?:in|out)|return|pick\s+up|drop\s+off|join)\b"
)
_NON_LODGING_ASR_PATTERN = re.compile(
    r"(?:\ub3d9\ud0a4\s*\ud638\ud154|\ub3d9\ud0a4\ud638\ud14c|\ub3c8\ud0a4\s*\ud638\ud154|\ub3c8\ud0a4\s*\ud638\ud14c|\ub3c8\ud0a4\ud638\ud14c).{0,24}?"
    r"(?:\uc654|\ub3c4\ucc29|\ucc3e\uc558|\ubc29\uc785\ub2c8\ub2e4|\uc219\uc18c\uc785\ub2c8\ub2e4)"
)
_GENERIC_LODGING_SEARCH_PATTERN = re.compile(
    r"(?:\uc88b\uc740|\uad1c\ucc2e\uc740|\uc608\uc05c|\uc800\ub834\ud55c|\uc0c8\ub85c\uc6b4|\ub2e4\ub978|\ubb35\uc744|\uc608\uc57d\ud560|\uac80\uc0c9\ud55c)\s*"
    r"(?:\ud638\ud154|\ub9ac\uc870\ud2b8|\ud39c\uc158|\uac8c\uc2a4\ud2b8\ud558\uc6b0\uc2a4)(?:\uc744|\ub97c)\s*\ucc3e\uc558"
    r"|(?:\uc778\ud130\ub137|\uac80\uc0c9|\uc608\uc57d|\ud6c4\uae30).{0,24}?"
    r"(?:\ud638\ud154|\ub9ac\uc870\ud2b8|\ud39c\uc158|\uac8c\uc2a4\ud2b8\ud558\uc6b0\uc2a4).{0,14}?\ucc3e\uc558"
)

_KO_TRANSIT_DIRECTION_PATTERN = re.compile(
    rf"{_KO_TRANSIT_PLACE}(?:으로|로|에|까지)?\s*"
    r"(?:갑니다|가요|향합니다|향해\s*갑니다|이동합니다|"
    r"가는\s*길(?:입니다|이에요)|가고\s*있습니다)\s*[.!~]*$"
)
_KO_NAMED_DIRECTION_PATTERN = re.compile(
    r"(?P<destination>[0-9a-z가-힣'’\-]{2,24}(?:\s+[0-9a-z가-힣'’\-]{2,24}){0,2}?)"
    r"(?:으로|로|에|까지)\s*"
    r"(?:갑니다|가요|향합니다|향해\s*갑니다|이동합니다|"
    r"가는\s*길(?:입니다|이에요)|가고\s*있습니다)\s*[.!~]*$"
)
_KO_NON_WAYPOINT_DIRECTION_PATTERN = re.compile(
    r"(?:거기|저기|여기|어디|어딘가|다음|곳|장소|목적지|여행지|밖|안|앞|뒤|"
    r"식당|맛집|카페|공원|박물관|"
    r"시장|해변|관광지|동물원|쇼핑몰|마트|병원|화장실|주차장|식사|아침|점심|저녁|"
    r"맛있는\s*곳|집|댓|회사|학교|놀이터)"
)
_EN_DIRECTION_PREFIX = (
    r"(?:(?:we|i)(?:'re|'m|\s+are|\s+am)?\s+)?"
    r"(?:going|heading|headed|traveling|travelling|driving|moving|on\s+our\s+way)"
    r"\s+(?:to|towards?)\s+(?:(?:the|our)\s+)?"
)
_EN_TRANSIT_DIRECTION_PATTERN = re.compile(
    rf"\b{_EN_DIRECTION_PREFIX}{_EN_TRANSIT_PLACE}"
    r"(?:\s+(?:now|today))?\s*[.!~]*$"
)
_EN_NAMED_DIRECTION_PATTERN = re.compile(
    rf"\b{_EN_DIRECTION_PREFIX}"
    r"(?P<destination>[a-z][a-z'’\-]{1,24}(?:\s+[a-z][a-z'’\-]{1,24}){0,3})"
    r"\s*[.!~]*$"
)
_EN_NON_WAYPOINT_DIRECTION_PATTERN = re.compile(
    r"\b(?:there|here|somewhere|anywhere|next|place|destination|outside|inside|bed|work|"
    r"dinner|breakfast|lunch|"
    r"restaurant|diner|cafe|park|museum|beach|market|mall|zoo|bathroom|parking|"
    r"home|house|office|school|store|shop|attraction|playground)\b"
)

_MEAL_SETUP_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "meal_setup_departure",
        re.compile(
            r"(?:밥|아침|점심|저녁|음식|라멘|라면|초밥|스시|고기|피자).{0,18}?"
            r"(?:먹으러|먹으로).{0,18}?(?:갑니다|가요|가자|갑시다|갈\s*거예요)"
            r"|\b(?:going|heading)\s+(?:out\s+)?(?:to\s+eat|for\s+(?:breakfast|lunch|dinner|food))\b"
        ),
    ),
    (
        "restaurant_arrival",
        re.compile(
            r"(?:식당|밥\s*집|밥집|맛집|레스토랑).{0,20}?(?:왔습니다|왔어요|도착했습니다|도착했어요)"
            r"|(?:왔습니다|왔어요|도착했습니다|도착했어요).{0,20}?(?:식당|밥\s*집|밥집|맛집|레스토랑)"
            r"|\b(?:arrived|made\s+it|we(?:'re|\s+are)\s+here)\b.{0,24}\b(?:restaurant|diner|cafe)\b"
        ),
    ),
    (
        "meal_setup_order",
        re.compile(
            r"(?:메뉴|음식|요리|밥|식사|아침|점심|저녁|피자|라멘|라면|초밥|스시|고기|"
            r"아이스크림|디저트|케이크|커피|주스)(?:이|가|은|는|도|을|를)?\s*"
            r"(?:주문(?:했습니다|했어요|했어|했|하는\s*중)|시켰습니다|시켰어요|시켰어)"
            r"|(?:주문(?:했습니다|했어요|했어|했|하는\s*중)|시켰습니다|시켰어요|시켰어)"
            r".{0,18}?(?:메뉴|음식|요리|밥|식사|아침|점심|저녁|피자|라멘|라면|초밥|스시|고기|"
            r"아이스크림|디저트|케이크|커피|주스)"
            r"|\b(?:ordered|placed\s+(?:our|the|an?)\s+order)\b.{0,24}"
            r"\b(?:food|meal|breakfast|lunch|dinner|pizza|ramen|sushi|dessert|coffee)\b"
        ),
    ),
)

_MEAL_CLOSURE_REACTION_PATTERN = re.compile(
    r"(?:밥|식사|아침|점심|저녁|음식|요리|메뉴|피자|라멘|라면|초밥|스시|고기|빵|"
    r"아이스크림|디저트|케이크|커피|주스).{0,20}?"
    r"(?:정말|진짜|너무|엄청|아주)?\s*(?:맛있었어요|맛있었습니다|맛있었어|좋았어요|좋았습니다|"
    r"최고였어요|최고였습니다)"
    r"|(?:맛있었어요|맛있었습니다|맛있었어|좋았어요|좋았습니다|최고였어요|최고였습니다)"
    r".{0,20}?(?:밥|식사|아침|점심|저녁|음식|요리|메뉴|피자|라멘|라면|초밥|스시|고기|빵|"
    r"아이스크림|디저트|케이크|커피|주스)"
    r"|\b(?:food|meal|breakfast|lunch|dinner|pizza|ramen|sushi|dessert|cake|coffee)\b"
    r".{0,24}?\b(?:was|were)\s+(?:really\s+|so\s+|very\s+)?(?:delicious|tasty|good|great|amazing)\b"
)

_MEAL_CLOSURE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "meal_closure_exit",
        re.compile(
            r"(?:밥|아침|점심|저녁|음식)?\s*먹고\s*(?:왔|나왔|돌아왔)"
            r"|(?:맛있게|다)\s*먹고.{0,24}?(?:돌아|갑시다|나왔|왔)"
            r"|\b(?:finished|done)\s+(?:eating|with\s+(?:breakfast|lunch|dinner))\b"
            r"|\b(?:just\s+ate|came\s+out\s+of\s+the\s+restaurant)\b"
        ),
    ),
    (
        "meal_closure_thanks",
        re.compile(r"잘\s*먹었습니다|잘\s*먹었어요|\bthanks?\s+for\s+the\s+(?:meal|food)\b"),
    ),
    (
        "meal_closure_reaction",
        _MEAL_CLOSURE_REACTION_PATTERN,
    ),
)

_VIRTUAL_MEAL_CONTEXT_PATTERN = re.compile(
    r"(?:책|동화|그림책|만화|이야기).{0,36}?(?:요리|음식|밥|먹|주먹밥|볶음밥)"
    r"|(?:요리|음식|밥|먹|주먹밥|볶음밥).{0,36}?(?:책|동화|그림책|만화|이야기)"
    r"|(?:고양이|친구들|가족들).{0,30}?(?:밥을\s*주|볶음밥을\s*먹네|다\s*같이\s*먹)"
    r"|\b(?:storybook|cookbook|picture\s+book|cartoon).{0,40}\b(?:cook|food|eat)\b"
)
_MEAL_RETROSPECTIVE_OR_PLAN_PATTERN = re.compile(
    r"(?:먹으러|먹으로|먹을\s*(?:거|예정)|먹고\s*(?:왔|나왔|돌아왔)|잘\s*먹었습니다|잘\s*먹었어요)"
    r"|(?:어제|지난번|그때|예전에).{0,30}?(?:먹|맛있)"
    r"|(?:먹었던|먹었었던|맛있었던)"
    r"|\b(?:going\s+to\s+eat|plan(?:ning)?\s+to\s+eat|ate\s+yesterday|used\s+to\s+eat)\b"
)
_MEAL_SERVED_FOOD_PATTERN = re.compile(
    r"(?:음식|요리|메뉴|피자|라멘|라면|초밥|스시|고기|빵|아이스크림|디저트|케이크|커피|주스)"
    r"(?:이|가|은|는|도|을|를)?\s*(?:(?:드디어|이제|방금|다|먼저|막)\s*)?"
    r"(?:나왔습니다|나왔어요|나왔네요|나왔어|받았습니다|받았어요|받았네요|"
    r"차려졌습니다|차려졌어요|도착했습니다)"
    r"|(?:나왔습니다|나왔어요|나왔네요|받았습니다|받았어요|차려졌습니다|"
    r"차려졌어요).{0,12}?"
    r"(?:음식|요리|메뉴|피자|라멘|라면|초밥|스시|고기|빵|아이스크림|디저트|케이크|커피|주스)"
    r"|\b(?:food|meal|dish|pizza|ramen|sushi|ice\s*cream|dessert|cake|coffee)\b"
    r".{0,24}?\b(?:arrived|was\s+served|is\s+here)\b"
)
_MEAL_APPROACH_OR_QUEUE_PATTERN = re.compile(
    r"(?:가\s*보시죠|들어가\s*보(?:자|시죠|겠습니다)|"
    r"줄.{0,24}?(?:서서|서|사서)?\s*기다리|대기\s*중|웨이팅\s*중)"
    r"|\b(?:let['’]?s\s+go\s+(?:in|inside|there)|waiting\s+in\s+line|queueing|queuing)\b"
)
_MEAL_ACTUAL_EATING_PATTERN = re.compile(
    r"(?:먹고\s*있|먹는\s*중|먹어\s*볼|먹어\s*보|먹어봤|한\s*입|입에\s*넣|냠냠)"
    r"|\b(?:we(?:'re|\s+are)|i(?:'m|\s+am))\s+(?:eating|having\s+(?:breakfast|lunch|dinner))\b"
    r"|\b(?:take|taking|took)\s+(?:a\s+)?bite\b"
)
_MEAL_TASTING_PATTERN = re.compile(
    r"(?:맛있어요|맛있네요|맛있습니다|진짜\s*맛있|엄청\s*맛있|너무\s*맛있|맛을\s*보)"
    r"|\b(?:tastes?|is)\s+(?:really\s+|so\s+|very\s+)?(?:delicious|tasty|good)\b"
)
_MEAL_FOOD_REVEAL_PATTERN = re.compile(
    r"(?:이거|이건|이게).{0,18}?(?:피자|라멘|라면|초밥|스시|고기|빵|아이스크림|디저트|케이크)(?:야|예요|이에요|입니다)"
)
_MEAL_FOOD_NOUN_PATTERN = re.compile(
    r"(?:밥|식사|아침|점심|저녁|음식|요리|메뉴|피자|라멘|라면|초밥|스시|고기|빵|"
    r"아이스크림|디저트|케이크|커피|주스)"
    r"|\b(?:breakfast|lunch|dinner|food|meal|dish|pizza|ramen|sushi|ice\s*cream|dessert|cake|coffee|juice)\b"
)
_MEAL_PRESENT_CONTEXT_PATTERN = re.compile(
    r"(?:이거|이건|이게|지금|여기|와|우와)"
    r"|\b(?:this|these|here|right\s+now|wow)\b"
)
_MEAL_DESSERT_PATTERN = re.compile(
    r"(?:아이스크림|디저트|케이크|빙수|도넛|과자)"
    r"|\b(?:ice\s*cream|dessert|cake|donut|doughnut)\b"
)
_MEAL_DRINK_PATTERN = re.compile(
    r"(?:커피|주스|음료|차를?\s*(?:마시|먹))"
    r"|\b(?:coffee|juice|drink|tea)\b"
)
_OPAQUE_MEAL_LOW_INFORMATION_PATTERN = re.compile(
    r"(?:(?:안녕(?:하세요)?|고맙습니다|감사합니다|빠+파|파+파|짠|건배|"
    r"네|예|응|와|우와|음|어|아|대성공)(?:\s+|[.!~]*)?)+"
    r"|(?:\d{1,2}시에\s+와서\s+)?대성공"
    r"|맛있게\s+(?:드세요|먹어|먹어요|먹자)"
    r"|(?:(?:hello|hi|thanks|thank\s+you|cheers|yes|yeah|okay|ok|wow|yay)"
    r"(?:\s+|[.!~]*)?)+|enjoy\s+your\s+meal"
)


@dataclass(frozen=True, slots=True)
class _InterviewEvent:
    event_id: str
    clip_id: str
    start: float
    end: float
    confidence: float
    signals: tuple[str, ...]
    anchor_event_id: str | None = None


@dataclass(frozen=True, slots=True)
class _MealMarker:
    signal: str
    start: float
    end: float


@dataclass(frozen=True, slots=True)
class _MealOption:
    clip_id: str
    start: float
    end: float
    confidence: float
    signals: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _MealEvent:
    event_id: str
    day_key: str
    travel_day: int
    subtype: str
    confidence: float
    signals: tuple[str, ...]
    options: tuple[_MealOption, ...]
    setup_options: tuple[_MealOption, ...] = ()
    closure_options: tuple[_MealOption, ...] = ()


@dataclass(frozen=True, slots=True)
class _MealEvidence:
    clip: Clip
    setup_markers: tuple[_MealMarker, ...]
    closure_markers: tuple[_MealMarker, ...]
    direct_options: tuple[_MealOption, ...]
    subtype: str
    virtual_context: bool
    inferred_body_eligible: bool


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
        preserve_meal_events = _preserve_meal_events(config)
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
        meal_events = (
            _detect_meal_events(clips, cues_by_clip)
            if preserve_meal_events
            else []
        )
        meal_options_by_clip: dict[str, list[tuple[_MealEvent, _MealOption]]] = defaultdict(list)
        meal_context_by_clip: dict[
            str,
            list[tuple[_MealEvent, str, _MealOption]],
        ] = defaultdict(list)
        for meal_event in meal_events:
            for option in meal_event.options:
                meal_options_by_clip[option.clip_id].append((meal_event, option))
            for stage, options in (
                ("setup", meal_event.setup_options),
                ("closure", meal_event.closure_options),
            ):
                for option in options:
                    meal_context_by_clip[option.clip_id].append(
                        (meal_event, stage, option)
                    )
        for index, clip in enumerate(clips, start=1):
            if clip.duration <= 0:
                continue
            print_status(f"candidates {index}/{len(clips)}: {Path(clip.path).name}")
            cues = cues_by_clip[clip.clip_id]
            signals = analyze_visual_signals(paths, clip, interval, force=force)
            interview_events = interview_events_by_clip.get(clip.clip_id, [])
            required_interview_events.extend((clip, event) for event in interview_events)
            transition_windows = _detect_journey_transition_windows(clip, cues)
            clip_meal_options = meal_options_by_clip.get(clip.clip_id, [])
            clip_meal_context = meal_context_by_clip.get(clip.clip_id, [])
            windows = _candidate_windows(
                clip,
                cues,
                signals,
                max_per_clip,
                required_events=interview_events,
                transition_windows=transition_windows,
                meal_windows=[
                    (option.start, option.end, "meal")
                    for _, option in clip_meal_options
                ]
                + [
                    (option.start, option.end, f"meal_{stage}")
                    for _, stage, option in clip_meal_context
                ],
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
                required_meal_event_ids = unique_preserving_order(
                    event.event_id
                    for event, option in clip_meal_options
                    if _ranges_overlap(start, end, option.start, option.end)
                )
                if required_meal_event_ids:
                    roles = unique_preserving_order(["food", *roles])
                required_meal_context_ids = unique_preserving_order(
                    _meal_context_id(event.event_id, stage)
                    for event, stage, option in clip_meal_context
                    if _ranges_overlap(start, end, option.start, option.end)
                )
                if required_meal_context_ids:
                    roles = unique_preserving_order(["food", *roles])
                motion, quality = _window_signals(signals, start, end)
                speech_duration = sum(
                    max(0.0, min(cue.end, end) - max(cue.start, start))
                    for cue in cues
                    if cue.end > start and cue.start < end
                )
                speech_ratio = min(1.0, speech_duration / max(0.1, end - start))
                location = _candidate_location(clip, text, config.get("locations", []))
                exclusion_reason = _candidate_exclusion_reason(
                    clip,
                    start,
                    end,
                    config["editing"].get("exclude_ranges", []),
                )
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
                if _candidate_frame_needs_extraction(frame_path, force=force):
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
                        required_meal_event_ids=required_meal_event_ids,
                        required_meal_context_ids=required_meal_context_ids,
                        origin=origin,
                        exclusion_reason=exclusion_reason,
                        source_kind=clip.source_kind,
                        source_stream_id=clip.source_stream_id,
                        capture_time_confidence=clip.capture_time_confidence,
                    )
                )

        candidates.sort(key=lambda item: (_candidate_timestamp(item), item.candidate_id))
        _assign_story_event_metadata(candidates)
        _assign_multicamera_angle_groups(candidates, paths.root)
        if not candidates:
            raise VideoSummaryError("편집 후보를 만들지 못했습니다.")
        required_events = [
            *_required_events_payload(required_interview_events, candidates),
            *_required_meal_events_payload(meal_events, candidates),
        ]
        payload = {
            "version": 6,
            "project": config["project"]["name"],
            "cache_key": cache_key,
            "policy_versions": {
                "journey_transition": JOURNEY_TRANSITION_POLICY_VERSION,
                "meal_event": MEAL_EVENT_POLICY_VERSION,
                "full_coverage_partition": FULL_COVERAGE_PARTITION_POLICY_VERSION,
                "story_event_catalog": STORY_EVENT_CATALOG_POLICY_VERSION,
                "multicamera_angle": MULTICAMERA_ANGLE_POLICY_VERSION,
            },
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


def _candidate_frame_needs_extraction(frame_path: Path, *, force: bool) -> bool:
    return force or not frame_path.exists() or frame_path.stat().st_size == 0


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
            "version": 18,
            "visual_signal_policy": VISUAL_SIGNAL_POLICY_VERSION,
            "journey_transition_detection": {
                "policy": JOURNEY_TRANSITION_POLICY_VERSION,
                "party_direction_context_policy": PARTY_TRANSITION_CONTEXT_POLICY_VERSION,
            },
            "meal_event_detection": {
                "policy": MEAL_EVENT_POLICY_VERSION,
                "preserve": _preserve_meal_events(config),
            },
            "family_interview_detection": {
                "policy": INTERVIEW_DETECTION_POLICY_VERSION,
                "preserve": _preserve_family_interviews(config),
            },
            "full_coverage_partition": FULL_COVERAGE_PARTITION_POLICY_VERSION,
            "story_event_catalog": STORY_EVENT_CATALOG_POLICY_VERSION,
            "multicamera_angle": MULTICAMERA_ANGLE_POLICY_VERSION,
            "project": config["project"]["name"],
            "clips": [
                (
                    clip.clip_id,
                    clip.fingerprint,
                    clip.captured_at,
                    clip.day_key,
                    clip.travel_day,
                    clip.location,
                    clip.source_kind,
                    clip.source_stream_id,
                    clip.capture_time_confidence,
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
            "exclude_ranges": config.get("editing", {}).get("exclude_ranges", []),
        }
    )


def _candidate_exclusion_reason(
    clip: Clip,
    start: float,
    end: float,
    rules: list[dict[str, Any]],
) -> str | None:
    relative = clip.relative_path
    basename = Path(relative).name
    for rule in rules:
        pattern = str(rule.get("match", "")).strip()
        root_pattern = pattern[3:] if pattern.startswith("**/") else pattern
        if not pattern or not (
            fnmatch(relative, pattern)
            or fnmatch(basename, pattern)
            or (root_pattern != pattern and fnmatch(relative, root_pattern))
        ):
            continue
        rule_start = float(rule.get("start", 0.0))
        rule_end = float(rule["end"]) if rule.get("end") is not None else clip.duration
        if min(end, rule_end) - max(start, rule_start) > 0.001:
            return str(rule.get("reason", "사용자 제외 구간")).strip()
    return None


def _assign_story_event_metadata(candidates: list[Candidate]) -> None:
    """Attach a stable activity block and auditable compression contract."""
    by_day: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in sorted(candidates, key=lambda item: (_candidate_timestamp(item), item.candidate_id)):
        by_day[candidate.day_key].append(candidate)

    for day_key, ordered in by_day.items():
        explicit_event_ids = _effective_story_event_ids(ordered)
        clusters: list[list[Candidate]] = []
        cluster_event_ids: set[str] = set()
        cluster_activity_kinds: set[str] = set()
        for candidate_index, candidate in enumerate(ordered):
            candidate_event_ids = explicit_event_ids[candidate_index]
            candidate_activity_kinds = _candidate_strong_activity_kinds(candidate)
            if not clusters:
                clusters.append([candidate])
                cluster_event_ids.update(candidate_event_ids)
                cluster_activity_kinds.update(candidate_activity_kinds)
                continue
            current = clusters[-1]
            previous = current[-1]
            previous_end = _candidate_timestamp(previous) + previous.duration
            current_start = _candidate_timestamp(candidate)
            cluster_start = _candidate_timestamp(current[0])
            location_changed = bool(
                previous.location
                and candidate.location
                and previous.location != candidate.location
            )
            transition_boundary = (
                candidate.clip_id != previous.clip_id
                and "transition" in candidate.roles
            )
            same_explicit_event = bool(cluster_event_ids & candidate_event_ids)
            explicit_event_boundary = bool(
                candidate_event_ids and not same_explicit_event
            )
            activity_boundary = bool(
                cluster_activity_kinds
                and candidate_activity_kinds
                and cluster_activity_kinds.isdisjoint(candidate_activity_kinds)
            )
            ordinary_boundary = (
                current_start - previous_end > STORY_EVENT_GAP_SECONDS
                or current_start - cluster_start > STORY_EVENT_MAX_SPAN_SECONDS
                or location_changed
                or transition_boundary
                or activity_boundary
            )
            if explicit_event_boundary or (ordinary_boundary and not same_explicit_event):
                clusters.append([candidate])
                cluster_event_ids = set(candidate_event_ids)
                cluster_activity_kinds = set(candidate_activity_kinds)
            else:
                current.append(candidate)
                cluster_event_ids.update(candidate_event_ids)
                cluster_activity_kinds.update(candidate_activity_kinds)

        for cluster in clusters:
            event_id = "story_" + stable_hash(
                {
                    "policy": STORY_EVENT_CATALOG_POLICY_VERSION,
                    "day_key": day_key,
                    "clip_id": cluster[0].clip_id,
                    "captured_at": cluster[0].captured_at,
                },
                length=18,
            )
            event_is_core = any(
                item.required_event_ids
                or item.required_meal_event_ids
                or item.required_meal_context_ids
                or "transition" in item.roles
                for item in cluster
            )
            for index, item in enumerate(cluster):
                item.story_event_id = event_id
                item.story_stage = _candidate_story_stage(item, index, len(cluster))
                item.importance = _candidate_importance(item, event_is_core)
                item.speed_policy = _candidate_speed_policy(item)


def _assign_multicamera_angle_groups(
    candidates: list[Candidate],
    project_root: Path,
) -> None:
    """Mark reliable simultaneous views of the same story beat.

    The group is deliberately conservative: two candidates must come from
    different source streams, share an inferred event, overlap in real capture
    time, and contain matching audio text or a near-identical representative
    frame. Ordinal stage labels may differ only when the content match is very
    strong. Low-confidence timestamps are never grouped, so a manually
    estimated messenger export cannot suppress an unrelated native clip.
    """
    by_event: dict[tuple[str, str], list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        candidate.angle_group_id = None
        if (
            candidate.story_event_id
            and candidate.source_stream_id
            and candidate.capture_time_confidence != "low"
        ):
            by_event[(candidate.day_key, candidate.story_event_id)].append(candidate)

    visual_hashes: dict[str, int | None] = {}
    for event_candidates in by_event.values():
        if len({item.source_stream_id for item in event_candidates}) < 2:
            continue
        ordered = sorted(event_candidates, key=lambda item: (_candidate_timestamp(item), item.candidate_id))
        components: list[list[Candidate]] = []
        for candidate in ordered:
            matching = next(
                (
                    component
                    for component in components
                    if all(
                        _same_multicamera_angle(
                            existing,
                            candidate,
                            project_root,
                            visual_hashes,
                        )
                        for existing in component
                    )
                ),
                None,
            )
            if matching is None:
                components.append([candidate])
            else:
                matching.append(candidate)
        for component in components:
            if len(component) < 2:
                continue
            group_id = "angle_" + stable_hash(
                {
                    "policy": MULTICAMERA_ANGLE_POLICY_VERSION,
                    "candidate_ids": sorted(item.candidate_id for item in component),
                },
                length=16,
            )
            for candidate in component:
                candidate.angle_group_id = group_id


def _same_multicamera_angle(
    left: Candidate,
    right: Candidate,
    project_root: Path,
    visual_hashes: dict[str, int | None],
) -> bool:
    if (
        left.clip_id == right.clip_id
        or left.source_stream_id == right.source_stream_id
        or left.capture_time_confidence == "low"
        or right.capture_time_confidence == "low"
    ):
        return False
    broad_roles = {"food", "fun", "scenery", "journey", "dialogue", "interview", "transition"}
    left_roles = set(left.roles) & broad_roles
    right_roles = set(right.roles) & broad_roles
    if left_roles and right_roles and left_roles.isdisjoint(right_roles):
        return False
    left_start = _candidate_timestamp(left)
    right_start = _candidate_timestamp(right)
    overlap = max(
        0.0,
        min(left_start + left.duration, right_start + right.duration)
        - max(left_start, right_start),
    )
    if overlap / max(0.001, min(left.duration, right.duration)) < 0.45:
        return False
    stage_matches = (
        _story_stage_bucket(left.story_stage)
        == _story_stage_bucket(right.story_stage)
    )
    left_hash = _candidate_frame_hash(left, project_root, visual_hashes)
    right_hash = _candidate_frame_hash(right, project_root, visual_hashes)
    if left_hash is None or right_hash is None:
        return False
    visual_similarity = 1.0 - ((left_hash ^ right_hash).bit_count() / 64.0)
    transcript_similarity = _multicamera_transcript_similarity(
        left.transcript,
        right.transcript,
    )
    transcript_matches = transcript_similarity >= (
        0.62 if stage_matches else 0.82
    )
    threshold = (
        0.86
        if stage_matches and transcript_matches
        else 0.92
        if transcript_matches
        else 0.90
        if stage_matches
        else 0.95
    )
    return visual_similarity >= threshold


def _story_stage_bucket(stage: str) -> str:
    if stage in {"body", "action"}:
        return "activity"
    if stage in {"reaction", "outcome"}:
        return "reaction"
    return stage


def _multicamera_transcript_similarity(left: str, right: str) -> float:
    normalize = lambda value: re.sub(r"[^0-9a-z가-힣]+", " ", value.casefold()).strip()
    normalized_left = normalize(left)
    normalized_right = normalize(right)
    if len(normalized_left) < 4 or len(normalized_right) < 4:
        return 0.0
    return SequenceMatcher(None, normalized_left, normalized_right).ratio()


def _candidate_frame_hash(
    candidate: Candidate,
    project_root: Path,
    cache: dict[str, int | None],
) -> int | None:
    if candidate.candidate_id in cache:
        return cache[candidate.candidate_id]
    path = project_root / candidate.frame_path
    try:
        with Image.open(path) as image:
            pixels = list(image.convert("L").resize((9, 8)).tobytes())
    except (OSError, ValueError):
        cache[candidate.candidate_id] = None
        return None
    value = 0
    for row in range(8):
        offset = row * 9
        for column in range(8):
            value = (value << 1) | int(
                pixels[offset + column] > pixels[offset + column + 1]
            )
    cache[candidate.candidate_id] = value
    return value


def _candidate_explicit_story_event_ids(candidate: Candidate) -> frozenset[str]:
    event_ids = {
        f"interview:{str(value).strip()}"
        for value in candidate.required_event_ids
        if str(value).strip()
    }
    event_ids.update(
        f"meal:{str(value).strip()}"
        for value in candidate.required_meal_event_ids
        if str(value).strip()
    )
    for context_id in candidate.required_meal_context_ids:
        normalized = str(context_id).strip()
        if not normalized:
            continue
        event_id, separator, stage = normalized.rpartition(":")
        if separator and event_id and stage in {"setup", "closure"}:
            normalized = event_id
        event_ids.add(f"meal:{normalized}")
    return frozenset(event_ids)


def _candidate_strong_activity_kinds(candidate: Candidate) -> frozenset[str]:
    roles = set(candidate.roles)
    return frozenset(role for role in ("food", "fun", "scenery") if role in roles)


def _effective_story_event_ids(candidates: list[Candidate]) -> list[frozenset[str]]:
    """Attach silent coverage only when it is bracketed by the same explicit event."""
    explicit = [_candidate_explicit_story_event_ids(candidate) for candidate in candidates]
    previous_ids: list[frozenset[str]] = []
    previous = frozenset()
    for event_ids in explicit:
        previous_ids.append(previous)
        if event_ids:
            previous = event_ids

    following_ids: list[frozenset[str]] = [frozenset() for _ in candidates]
    following = frozenset()
    for index in range(len(candidates) - 1, -1, -1):
        following_ids[index] = following
        if explicit[index]:
            following = explicit[index]

    effective = list(explicit)
    for index, candidate in enumerate(candidates):
        if explicit[index] or not _is_silent_story_bridge(candidate):
            continue
        shared = previous_ids[index] & following_ids[index]
        if shared:
            effective[index] = frozenset(shared)
    return effective


def _is_silent_story_bridge(candidate: Candidate) -> bool:
    return (
        candidate.origin == "coverage"
        and candidate.speech_ratio <= 0.08
        and not (
            set(candidate.roles)
            & {"dialogue", "food", "fun", "scenery", "interview", "transition"}
        )
    )


def _candidate_story_stage(candidate: Candidate, index: int, count: int) -> str:
    context_stages = {
        value.rsplit(":", 1)[-1]
        for value in candidate.required_meal_context_ids
    }
    if "setup" in context_stages:
        return "setup"
    if candidate.required_meal_event_ids:
        return "body"
    if "closure" in context_stages:
        return "closure"
    if candidate.required_event_ids:
        return "outcome"
    if "transition" in candidate.roles:
        return "bridge"
    if "food" in candidate.roles:
        return "body"
    if "fun" in candidate.roles:
        return "action"
    if candidate.origin == "coverage":
        return "bridge"
    if index == 0:
        return "setup"
    if index == count - 1:
        return "closure"
    return "body"


def _candidate_importance(candidate: Candidate, event_is_core: bool) -> str:
    if candidate.exclusion_reason:
        return "discard"
    if event_is_core or candidate.required_event_ids:
        return "core"
    if set(candidate.roles) & {"food", "fun", "dialogue", "scenery"}:
        return "supporting"
    return "bridge"


def _candidate_speed_policy(candidate: Candidate) -> str:
    if candidate.exclusion_reason:
        return "omit"
    protected_roles = {
        "interview",
        "transition",
        "food",
        "fun",
        "dialogue",
        "scenery",
    }
    if (
        candidate.required_event_ids
        or candidate.required_meal_event_ids
        or candidate.required_meal_context_ids
        or set(candidate.roles) & protected_roles
        or candidate.speech_ratio > 0.08
        or candidate.story_stage == "outcome"
    ):
        return "protected_1x"
    return "allow_fast"


def _preserve_family_interviews(config: dict[str, Any]) -> bool:
    return config.get("editing", {}).get("preserve_family_interviews", True) is True


def _preserve_meal_events(config: dict[str, Any]) -> bool:
    return config.get("editing", {}).get("preserve_meal_events", True) is True


def _detect_meal_events(
    clips: list[Clip],
    cues_by_clip: dict[str, list[TranscriptCue]],
) -> list[_MealEvent]:
    """Detect filmed meal bodies and compact setup/closure narrative beats."""
    evidences = [
        _meal_evidence(clip, cues_by_clip.get(clip.clip_id, []))
        for clip in clips
        if clip.duration > 0
    ]
    grouped: dict[str, list[_MealEvidence]] = defaultdict(list)
    for evidence in evidences:
        grouped[evidence.clip.day_key].append(evidence)

    events: list[_MealEvent] = []
    for day_evidences in grouped.values():
        day_evidences.sort(key=lambda item: (_clip_start_timestamp(item.clip), item.clip.clip_id))
        claimed_setup: set[tuple[int, _MealMarker]] = set()
        claimed_closure: set[tuple[int, _MealMarker]] = set()
        direct_runs = _direct_meal_runs(day_evidences)

        # Explicit filmed food always wins over an opaque adjacent clip. Build
        # each nearby same-subtype run once, then attach the nearest compatible
        # setup and closure without crossing another direct meal run.
        for run_index, run in enumerate(direct_runs):
            run_start, run_end = _direct_run_bounds(run)
            previous_run_end = (
                _direct_run_bounds(direct_runs[run_index - 1])[1]
                if run_index > 0
                else float("-inf")
            )
            next_run_start = (
                _direct_run_bounds(direct_runs[run_index + 1])[0]
                if run_index + 1 < len(direct_runs)
                else float("inf")
            )
            prior_closures = [
                _meal_marker_timestamp(evidence, marker, use_end=False)
                for evidence in day_evidences
                for marker in evidence.closure_markers
                if previous_run_end
                < _meal_marker_timestamp(evidence, marker, use_end=False)
                < run_start
            ]
            setup_floor = max(previous_run_end, max(prior_closures, default=float("-inf")))
            setup_candidates = [
                (evidence_index, evidence, marker)
                for evidence_index, evidence in enumerate(day_evidences)
                for marker in evidence.setup_markers
                if setup_floor
                < _meal_marker_timestamp(evidence, marker, use_end=True)
                <= run_start
                and run_start - _meal_marker_timestamp(evidence, marker, use_end=True)
                <= MEAL_SETUP_HORIZON_SECONDS
                and _setup_matches_direct_run(evidence, marker, run)
            ]
            setup_match = (
                max(
                    setup_candidates,
                    key=lambda item: _meal_marker_timestamp(item[1], item[2], use_end=True),
                )
                if setup_candidates
                else None
            )
            claimed_setup.update((index, marker) for index, _, marker in setup_candidates)

            future_setups = [
                _meal_marker_timestamp(evidence, marker, use_end=False)
                for evidence in day_evidences
                for marker in evidence.setup_markers
                if run_end
                < _meal_marker_timestamp(evidence, marker, use_end=False)
                < next_run_start
            ]
            closure_ceiling = min(next_run_start, min(future_setups, default=float("inf")))
            closure_horizon = (
                MEAL_SETUP_HORIZON_SECONDS
                if setup_match is not None
                else MEAL_INFER_BEFORE_CLOSURE_SECONDS
            )
            closure_candidates = [
                (evidence_index, evidence, marker)
                for evidence_index, evidence in enumerate(day_evidences)
                for marker in evidence.closure_markers
                if run_end
                <= _meal_marker_timestamp(evidence, marker, use_end=False)
                < closure_ceiling
                and _meal_marker_timestamp(evidence, marker, use_end=False) - run_end
                <= closure_horizon
                and _closure_matches_direct_run(evidence, run)
            ]
            closure_match = (
                min(
                    closure_candidates,
                    key=lambda item: _meal_marker_timestamp(item[1], item[2], use_end=False),
                )
                if closure_candidates
                else None
            )
            claimed_closure.update((index, marker) for index, _, marker in closure_candidates)

            options = [option for _, option in run]
            events.append(
                _make_meal_event(
                    run[0][0].clip,
                    options,
                    subtype=run[0][0].subtype,
                    signals=[
                        "direct_actual",
                        *([setup_match[2].signal] if setup_match is not None else []),
                        *(signal for option in options for signal in option.signals),
                        *([closure_match[2].signal] if closure_match is not None else []),
                    ],
                    setup_options=(
                        [_meal_context_option(setup_match[1], setup_match[2], stage="setup")]
                        if setup_match is not None
                        else []
                    ),
                    closure_options=(
                        [_meal_context_option(closure_match[1], closure_match[2], stage="closure")]
                        if closure_match is not None
                        else []
                    ),
                )
            )

        direct_evidence_indexes = {
            evidence_index
            for evidence_index, evidence in enumerate(day_evidences)
            if evidence.direct_options
        }

        # If no compatible explicit body exists, a setup can still recover the
        # immediately following silent/allowlisted visual clip. A later setup
        # before any body supersedes an earlier approach narration.
        for index, evidence in enumerate(day_evidences):
            available_setup = [
                marker
                for marker in evidence.setup_markers
                if (index, marker) not in claimed_setup
            ]
            if not available_setup or index + 1 >= len(day_evidences):
                continue
            horizon_end = _clip_start_timestamp(evidence.clip) + MEAL_SETUP_HORIZON_SECONDS
            later_setup_index = next(
                (
                    candidate_index
                    for candidate_index in range(index + 1, len(day_evidences))
                    if _clip_start_timestamp(day_evidences[candidate_index].clip) <= horizon_end
                    and day_evidences[candidate_index].setup_markers
                    and not any(
                        boundary in direct_evidence_indexes
                        or day_evidences[boundary].closure_markers
                        for boundary in range(index + 1, candidate_index)
                    )
                ),
                None,
            )
            if later_setup_index is not None:
                claimed_setup.update((index, marker) for marker in available_setup)
                continue

            closure_index: int | None = None
            for candidate_index in range(index + 1, len(day_evidences)):
                candidate = day_evidences[candidate_index]
                if _clip_start_timestamp(candidate.clip) > horizon_end:
                    break
                if candidate_index in direct_evidence_indexes or candidate.setup_markers:
                    break
                available_closure = [
                    marker
                    for marker in candidate.closure_markers
                    if (candidate_index, marker) not in claimed_closure
                ]
                if available_closure:
                    closure_index = candidate_index
                    break

            next_evidence = day_evidences[index + 1]
            if index + 1 in direct_evidence_indexes or next_evidence.setup_markers:
                continue
            has_strong_setup = any(
                marker.signal in {"restaurant_arrival", "meal_setup_order"}
                for marker in available_setup
            )
            if closure_index is None and not has_strong_setup:
                continue
            inferred = _inferred_meal_body_option(
                evidence,
                next_evidence,
                signal="inferred_body_after_setup",
                maximum_gap=MEAL_INFER_AFTER_SETUP_SECONDS,
                confidence=0.91 if closure_index is not None else 0.88,
            )
            if inferred is None:
                continue
            setup_marker = max(available_setup, key=lambda marker: marker.end)
            closure_marker = (
                min(
                    (
                        marker
                        for marker in day_evidences[closure_index].closure_markers
                        if (closure_index, marker) not in claimed_closure
                    ),
                    key=lambda marker: marker.start,
                )
                if closure_index is not None
                else None
            )
            claimed_setup.update((index, marker) for marker in available_setup)
            if closure_marker is not None and closure_index is not None:
                claimed_closure.update(
                    (closure_index, marker)
                    for marker in day_evidences[closure_index].closure_markers
                )
            events.append(
                _make_meal_event(
                    evidence.clip,
                    [inferred],
                    subtype=_meal_event_subtype(evidence.subtype, next_evidence.subtype),
                    signals=[
                        setup_marker.signal,
                        inferred.signals[0],
                        *([closure_marker.signal] if closure_marker is not None else []),
                    ],
                    setup_options=[
                        _meal_context_option(evidence, setup_marker, stage="setup")
                    ],
                    closure_options=(
                        [
                            _meal_context_option(
                                day_evidences[closure_index],
                                closure_marker,
                                stage="closure",
                            )
                        ]
                        if closure_marker is not None and closure_index is not None
                        else []
                    ),
                )
            )

        # A remaining post-meal statement can reveal only the immediately
        # preceding silent/allowlisted body. Direct body runs were already
        # emitted and consumed above, so they can never form duplicate events.
        for index, evidence in enumerate(day_evidences):
            available_closure = [
                marker
                for marker in evidence.closure_markers
                if (index, marker) not in claimed_closure
            ]
            if not available_closure or index == 0 or index - 1 in direct_evidence_indexes:
                continue
            previous = day_evidences[index - 1]
            inferred = _inferred_meal_body_option(
                previous,
                previous,
                signal="inferred_body_before_closure",
                maximum_gap=MEAL_INFER_BEFORE_CLOSURE_SECONDS,
                confidence=0.90,
                following=evidence,
            )
            if inferred is None:
                continue
            closure_marker = min(available_closure, key=lambda marker: marker.start)
            claimed_closure.update((index, marker) for marker in available_closure)
            events.append(
                _make_meal_event(
                    previous.clip,
                    [inferred],
                    subtype=_meal_event_subtype(previous.subtype, evidence.subtype),
                    signals=[closure_marker.signal, inferred.signals[0]],
                    closure_options=[
                        _meal_context_option(evidence, closure_marker, stage="closure")
                    ],
                )
            )

    deduplicated: dict[tuple[Any, ...], _MealEvent] = {}
    for event in events:
        signature = (
            tuple(_meal_option_key(option) for option in event.options),
            tuple(_meal_option_key(option) for option in event.setup_options),
            tuple(_meal_option_key(option) for option in event.closure_options),
        )
        existing = deduplicated.get(signature)
        if existing is None or (event.confidence, len(event.signals)) > (
            existing.confidence,
            len(existing.signals),
        ):
            deduplicated[signature] = event
    return sorted(
        deduplicated.values(),
        key=lambda event: (
            event.day_key,
            min(
                _clip_start_timestamp(next(evidence.clip for evidence in evidences if evidence.clip.clip_id == option.clip_id))
                + option.start
                for option in event.options
            ),
            event.event_id,
        ),
    )


def _meal_evidence(clip: Clip, cues: list[TranscriptCue]) -> _MealEvidence:
    ordered = sorted(
        (cue for cue in cues if cue.end > cue.start and cue.text.strip()),
        key=lambda cue: (cue.start, cue.end),
    )
    combined = " ".join(cue.text.strip() for cue in ordered).casefold()
    virtual_context = _VIRTUAL_MEAL_CONTEXT_PATTERN.search(combined) is not None
    setup_markers: list[_MealMarker] = []
    closure_markers: list[_MealMarker] = []
    direct_options: list[_MealOption] = []
    if not virtual_context:
        for group in _group_cues(ordered):
            setup_marker = _minimal_meal_marker(_MEAL_SETUP_PATTERNS, group)
            closure_marker = _minimal_meal_marker(_MEAL_CLOSURE_PATTERNS, group)
            group_text = " ".join(cue.text.strip() for cue in group).casefold()
            if (
                closure_marker is not None
                and closure_marker.signal == "meal_closure_reaction"
                and _MEAL_RETROSPECTIVE_OR_PLAN_PATTERN.search(group_text)
            ):
                closure_marker = None
            if setup_marker is not None:
                setup_markers.append(setup_marker)
            if closure_marker is not None:
                closure_markers.append(closure_marker)
            direct_match = _minimal_direct_meal_span(group, combined)
            if direct_match is None:
                continue
            direct_signal, direct_start, direct_end = direct_match
            start, end = _ensure_duration(
                max(0.0, direct_start - 0.35),
                min(clip.duration, direct_end + 0.75),
                clip.duration,
                minimum=2.5,
                maximum=MEAL_OPTION_MAX_DURATION_SECONDS,
            )
            _append_unique_meal_option(
                direct_options,
                _MealOption(
                    clip_id=clip.clip_id,
                    start=round(start, 3),
                    end=round(end, 3),
                    confidence={
                        "served_food": 0.97,
                        "actual_eating": 0.96,
                        "food_reveal": 0.94,
                        "tasting_food": 0.92,
                    }[direct_signal],
                    signals=(direct_signal,),
                ),
            )
    return _MealEvidence(
        clip=clip,
        setup_markers=tuple(setup_markers),
        closure_markers=tuple(closure_markers),
        direct_options=tuple(direct_options[:3]),
        subtype=_meal_subtype(combined),
        virtual_context=virtual_context,
        # Opaque body inference is intentionally conservative. Short meal
        # clips often transcribe only greetings, but an explicit travel-state
        # narration (for example, arriving at a hotel) must never stand in for
        # filmed food merely because a "잘 먹었습니다" clip follows it.
        inferred_body_eligible=_opaque_meal_body_eligible(combined),
    )


def _opaque_meal_body_eligible(text: str) -> bool:
    """Allow only silent or tightly allowlisted low-information clips."""
    normalized = " ".join(text.casefold().split())
    if not normalized:
        return True
    if _MEAL_RETROSPECTIVE_OR_PLAN_PATTERN.search(normalized):
        return False
    if any(word in normalized for word in JOURNEY_WORDS | SCENERY_WORDS):
        return False
    # Greetings, thanks, and tiny interjections are common on otherwise
    # visual meal clips. Other semantics are not safe evidence merely because
    # they happen to sit between meal setup and closure narration.
    return _OPAQUE_MEAL_LOW_INFORMATION_PATTERN.fullmatch(normalized) is not None


def _matching_meal_signal(
    patterns: tuple[tuple[str, re.Pattern[str]], ...],
    text: str,
) -> str | None:
    return next((signal for signal, pattern in patterns if pattern.search(text)), None)


def _minimal_meal_marker(
    patterns: tuple[tuple[str, re.Pattern[str]], ...],
    cues: list[TranscriptCue],
) -> _MealMarker | None:
    matches: list[tuple[float, int, float, _MealMarker]] = []
    for start_index in range(len(cues)):
        for end_index in range(start_index, len(cues)):
            window = cues[start_index : end_index + 1]
            text = " ".join(cue.text.strip() for cue in window).casefold()
            signal = _matching_meal_signal(patterns, text)
            if signal is None:
                continue
            marker = _MealMarker(signal, window[0].start, window[-1].end)
            matches.append(
                (
                    marker.end - marker.start,
                    len(window),
                    marker.start,
                    marker,
                )
            )
    return min(matches, key=lambda item: item[:3])[3] if matches else None


def _minimal_direct_meal_span(
    cues: list[TranscriptCue],
    clip_context: str,
) -> tuple[str, float, float] | None:
    matches: list[tuple[float, int, float, str, float, float]] = []
    grouped_context = " ".join(cue.text.strip() for cue in cues).casefold()
    group_signal = _meal_direct_signal(grouped_context, clip_context)
    if group_signal is None:
        return None
    for start_index in range(len(cues)):
        for end_index in range(start_index, len(cues)):
            window = cues[start_index : end_index + 1]
            # A demonstrative food reveal is high confidence only within one
            # ASR cue. Joining a distant "이거 뭐야?" to a later misheard food
            # noun promoted Sapporo outdoor footage as an explicit meal body.
            if group_signal == "food_reveal" and len(window) > 1:
                continue
            text = " ".join(cue.text.strip() for cue in window).casefold()
            signal = _meal_direct_signal(text, clip_context)
            if signal != group_signal:
                continue
            start = window[0].start
            end = window[-1].end
            matches.append((end - start, len(window), start, signal, start, end))
    if not matches:
        if group_signal == "food_reveal":
            return None
        return group_signal, cues[0].start, cues[-1].end
    _, _, _, signal, start, end = min(matches, key=lambda item: item[:3])
    for cue in cues:
        normalized = " ".join(cue.text.casefold().split())
        if cue.start < start or cue.end - start > MEAL_OPTION_MAX_DURATION_SECONDS:
            continue
        if (
            _MEAL_ACTUAL_EATING_PATTERN.search(normalized)
            or _MEAL_FOOD_REVEAL_PATTERN.search(normalized)
            or (
                _MEAL_TASTING_PATTERN.search(normalized)
                and not _MEAL_CLOSURE_REACTION_PATTERN.search(normalized)
                and _MEAL_FOOD_NOUN_PATTERN.search(clip_context)
            )
        ):
            end = max(end, cue.end)
    return signal, start, end


def _meal_direct_signal(text: str, clip_context: str = "") -> str | None:
    normalized = " ".join(text.casefold().split())
    if not normalized or _VIRTUAL_MEAL_CONTEXT_PATTERN.search(normalized):
        return None
    if _MEAL_RETROSPECTIVE_OR_PLAN_PATTERN.search(normalized):
        return None
    if (
        _MEAL_SERVED_FOOD_PATTERN.search(normalized)
        and not _MEAL_APPROACH_OR_QUEUE_PATTERN.search(normalized)
    ):
        return "served_food"
    if _MEAL_FOOD_REVEAL_PATTERN.search(normalized):
        return "food_reveal"
    if _MEAL_ACTUAL_EATING_PATTERN.search(normalized):
        return "actual_eating"
    if (
        _MEAL_TASTING_PATTERN.search(normalized)
        and not _MEAL_CLOSURE_REACTION_PATTERN.search(normalized)
        and (
            _MEAL_FOOD_NOUN_PATTERN.search(normalized)
            or (
                _MEAL_PRESENT_CONTEXT_PATTERN.search(normalized)
                and _MEAL_FOOD_NOUN_PATTERN.search(clip_context)
            )
        )
    ):
        return "tasting_food"
    return None


def _meal_subtype(text: str) -> str:
    if _MEAL_DESSERT_PATTERN.search(text):
        return "dessert"
    if _MEAL_DRINK_PATTERN.search(text):
        return "drink"
    return "meal"


def _meal_event_subtype(*subtypes: str) -> str:
    values = [value for value in subtypes if value]
    if "meal" in values:
        return "meal"
    return values[0] if values else "meal"


def _clip_gap_seconds(earlier: Clip, later: Clip) -> float:
    return max(
        0.0,
        _clip_start_timestamp(later) - (_clip_start_timestamp(earlier) + earlier.duration),
    )


def _inferred_meal_body_option(
    anchor: _MealEvidence,
    body: _MealEvidence,
    *,
    signal: str,
    maximum_gap: float,
    confidence: float,
    following: _MealEvidence | None = None,
) -> _MealOption | None:
    if body.virtual_context:
        return None
    gap = (
        _clip_gap_seconds(anchor.clip, body.clip)
        if following is None
        else _clip_gap_seconds(body.clip, following.clip)
    )
    if gap > maximum_gap:
        return None
    if body.direct_options:
        return body.direct_options[0]
    if not body.inferred_body_eligible:
        return None
    if body.setup_markers or body.closure_markers or body.clip.duration < 0.75:
        return None
    duration = min(body.clip.duration, MAX_CANDIDATE_DURATION_SECONDS)
    return _MealOption(
        clip_id=body.clip.clip_id,
        start=0.0,
        end=round(duration, 3),
        confidence=confidence,
        signals=(signal,),
    )


def _meal_option_key(option: _MealOption) -> tuple[str, float, float]:
    return option.clip_id, round(option.start, 3), round(option.end, 3)


def _meal_context_option(
    evidence: _MealEvidence,
    marker: _MealMarker,
    *,
    stage: str,
) -> _MealOption:
    start, end = _ensure_duration(
        max(0.0, marker.start - 0.35),
        min(evidence.clip.duration, marker.end + 0.75),
        evidence.clip.duration,
        minimum=2.5,
        maximum=MEAL_OPTION_MAX_DURATION_SECONDS,
    )
    return _MealOption(
        clip_id=evidence.clip.clip_id,
        start=round(start, 3),
        end=round(end, 3),
        confidence=0.95,
        signals=(f"meal_{stage}", marker.signal),
    )


def _meal_context_id(event_id: str, stage: str) -> str:
    return f"{event_id}:{stage}"


def _meal_option_timestamp(evidence: _MealEvidence, option: _MealOption) -> float:
    return _clip_start_timestamp(evidence.clip) + option.start


def _meal_option_center_timestamp(evidence: _MealEvidence, option: _MealOption) -> float:
    return _clip_start_timestamp(evidence.clip) + (option.start + option.end) / 2.0


def _meal_marker_timestamp(
    evidence: _MealEvidence,
    marker: _MealMarker,
    *,
    use_end: bool,
) -> float:
    return _clip_start_timestamp(evidence.clip) + (marker.end if use_end else marker.start)


def _direct_meal_runs(
    evidences: list[_MealEvidence],
) -> list[list[tuple[_MealEvidence, _MealOption]]]:
    """Group explicit nearby views without crossing a meal context boundary."""
    entries = [
        (evidence, option)
        for evidence in evidences
        for option in evidence.direct_options
    ]
    entries.sort(key=lambda item: (_meal_option_timestamp(*item), _meal_option_key(item[1])))
    marker_times = sorted(
        _meal_marker_timestamp(evidence, marker, use_end=False)
        for evidence in evidences
        for marker in (*evidence.setup_markers, *evidence.closure_markers)
    )
    runs: list[list[tuple[_MealEvidence, _MealOption]]] = []
    for entry in entries:
        if runs:
            previous = runs[-1][-1]
            previous_time = _meal_option_center_timestamp(*previous)
            incoming_time = _meal_option_center_timestamp(*entry)
            crosses_context = any(
                previous_time < marker_time < incoming_time
                for marker_time in marker_times
            )
            if (
                entry[0].subtype != previous[0].subtype
                or _meal_option_timestamp(*entry)
                - _meal_option_timestamp(*previous)
                > MEAL_DIRECT_CLUSTER_SECONDS
                or crosses_context
            ):
                runs.append([])
        if not runs:
            runs.append([])
        runs[-1].append(entry)
    return runs


def _direct_run_bounds(
    run: list[tuple[_MealEvidence, _MealOption]],
) -> tuple[float, float]:
    centers = [_meal_option_center_timestamp(*entry) for entry in run]
    return min(centers), max(centers)


def _setup_matches_direct_run(
    evidence: _MealEvidence,
    marker: _MealMarker,
    run: list[tuple[_MealEvidence, _MealOption]],
) -> bool:
    return (
        evidence.subtype == run[0][0].subtype
        or marker.signal in {"restaurant_arrival", "meal_setup_order"}
    )


def _closure_matches_direct_run(
    evidence: _MealEvidence,
    run: list[tuple[_MealEvidence, _MealOption]],
) -> bool:
    return evidence.subtype in {"meal", run[0][0].subtype}


def _append_unique_meal_option(options: list[_MealOption], incoming: _MealOption) -> None:
    if any(_meal_option_key(option) == _meal_option_key(incoming) for option in options):
        return
    options.append(incoming)


def _make_meal_event(
    clip: Clip,
    options: list[_MealOption],
    *,
    subtype: str,
    signals: list[str],
    setup_options: list[_MealOption] | None = None,
    closure_options: list[_MealOption] | None = None,
) -> _MealEvent:
    bounded = tuple(options[:3])
    bounded_setup = tuple((setup_options or [])[:1])
    bounded_closure = tuple((closure_options or [])[:1])
    event_id = "meal_" + stable_hash(
        {
            "policy": MEAL_EVENT_POLICY_VERSION,
            "day_key": clip.day_key,
            "subtype": subtype,
            "options": [_meal_option_key(option) for option in bounded],
            "setup": [_meal_option_key(option) for option in bounded_setup],
            "closure": [_meal_option_key(option) for option in bounded_closure],
        },
        length=18,
    )
    return _MealEvent(
        event_id=event_id,
        day_key=clip.day_key,
        travel_day=clip.travel_day,
        subtype=subtype,
        confidence=round(max(option.confidence for option in bounded), 3),
        signals=tuple(unique_preserving_order(signals)),
        options=bounded,
        setup_options=bounded_setup,
        closure_options=bounded_closure,
    )


def _detect_journey_transition_windows(
    clip: Clip,
    cues: list[TranscriptCue],
) -> list[tuple[float, float, str]]:
    """Return compact, high-confidence waypoints without project-specific names."""
    ordered = sorted(
        (cue for cue in cues if cue.end > cue.start and cue.text.strip()),
        key=lambda cue: (cue.start, cue.end),
    )
    windows: list[tuple[float, float, str]] = []
    matched_cues: set[int] = set()
    recent_by_subtype: dict[str, tuple[float, float]] = {}

    def append_once(start: float, end: float, text: str, signal: str) -> None:
        subtype = _journey_transition_dedupe_key(signal, text)
        previous = recent_by_subtype.get(subtype)
        if previous is not None and start - previous[1] <= JOURNEY_TRANSITION_DEDUPE_SECONDS:
            return
        recent_by_subtype[subtype] = (start, end)
        padded_start, padded_end = _ensure_duration(
            max(0.0, start - 0.35),
            min(clip.duration, end + 0.75),
            clip.duration,
            minimum=2.5,
            maximum=MAX_CANDIDATE_DURATION_SECONDS,
        )
        _append_window(windows, (padded_start, padded_end, "transition"))

    for index, cue in enumerate(ordered):
        signal = _journey_transition_signal(cue.text)
        if signal is None:
            continue
        matched_cues.add(index)
        start, end, text = _party_transition_context(ordered, index, signal)
        append_once(start, end, text, signal)

    # ASR can split the party/place and movement verb across adjacent cues.
    # Only join one short, continuous speech run and retain the same strict
    # question/instruction filters over the combined text.
    for group in _group_cues(ordered):
        group_indexes = {
            index
            for index, cue in enumerate(ordered)
            if cue in group
        }
        if group_indexes & matched_cues or len(group) < 2:
            continue
        text = " ".join(cue.text.strip() for cue in group)
        signal = _journey_transition_signal(text)
        if signal is None:
            continue
        append_once(group[0].start, group[-1].end, text, signal)
    return _merge_overlapping_windows(windows)


def _party_transition_context(
    cues: list[TranscriptCue],
    anchor_index: int,
    signal: str,
) -> tuple[float, float, str]:
    """Attach one nearby, declarative destination leg to a family handoff."""
    anchor = cues[anchor_index]
    if "party_pickup_dropoff_join" not in signal:
        return anchor.start, anchor.end, anchor.text

    contexts: list[tuple[float, float, str]] = []
    forward_limit = min(len(cues), anchor_index + 1 + PARTY_TRANSITION_CONTEXT_MAX_CUES)
    for end_index in range(anchor_index + 1, forward_limit):
        run = cues[anchor_index + 1 : end_index + 1]
        if not _continuous_cue_run([anchor, *run]):
            break
        if run[-1].end - anchor.start > PARTY_TRANSITION_CONTEXT_MAX_SECONDS:
            break
        direction_text = " ".join(cue.text.strip() for cue in run)
        if _journey_direction_destination(direction_text):
            contexts.append(
                (
                    anchor.start,
                    run[-1].end,
                    " ".join(cue.text.strip() for cue in [anchor, *run]),
                )
            )
            break

    backward_limit = max(-1, anchor_index - PARTY_TRANSITION_CONTEXT_MAX_CUES - 1)
    for start_index in range(anchor_index - 1, backward_limit, -1):
        run = cues[start_index:anchor_index]
        if not _continuous_cue_run([*run, anchor]):
            break
        if anchor.end - run[0].start > PARTY_TRANSITION_CONTEXT_MAX_SECONDS:
            break
        direction_text = " ".join(cue.text.strip() for cue in run)
        if _journey_direction_destination(direction_text):
            contexts.append(
                (
                    run[0].start,
                    anchor.end,
                    " ".join(cue.text.strip() for cue in [*run, anchor]),
                )
            )
            break

    if not contexts:
        return anchor.start, anchor.end, anchor.text
    return min(
        contexts,
        key=lambda item: (
            item[1] - item[0],
            0 if item[0] == anchor.start else 1,
            item[0],
        ),
    )


def _continuous_cue_run(cues: list[TranscriptCue]) -> bool:
    return all(
        right.start - left.end <= 1.8
        for left, right in zip(cues, cues[1:])
    )


def _journey_direction_destination(text: str) -> bool:
    normalized = " ".join(text.casefold().split())
    if not normalized:
        return False
    if (
        _JOURNEY_TRANSITION_QUESTION_PATTERN.search(normalized)
        or _JOURNEY_TRANSITION_INSTRUCTION_PATTERN.search(normalized)
        or _GENERIC_JOURNEY_COMMAND_PATTERN.search(normalized)
    ):
        return False
    if _KO_TRANSIT_DIRECTION_PATTERN.search(normalized):
        return True
    korean = _KO_NAMED_DIRECTION_PATTERN.search(normalized)
    if korean is not None:
        destination = korean.group("destination")
        return _KO_NON_WAYPOINT_DIRECTION_PATTERN.search(destination) is None
    if _EN_TRANSIT_DIRECTION_PATTERN.search(normalized):
        return True
    english = _EN_NAMED_DIRECTION_PATTERN.search(normalized)
    if english is not None:
        destination = english.group("destination")
        return _EN_NON_WAYPOINT_DIRECTION_PATTERN.search(destination) is None
    return False


def _journey_transition_signal(text: str) -> str | None:
    normalized = " ".join(text.casefold().split())
    if not normalized:
        return None
    if (
        _JOURNEY_TRANSITION_QUESTION_PATTERN.search(normalized)
        or _JOURNEY_TRANSITION_INSTRUCTION_PATTERN.search(normalized)
        or _GENERIC_JOURNEY_COMMAND_PATTERN.search(normalized)
        or _NON_LODGING_ASR_PATTERN.search(normalized)
        or _GENERIC_LODGING_SEARCH_PATTERN.search(normalized)
    ):
        return None
    for signal, pattern in _JOURNEY_TRANSITION_PATTERNS:
        if pattern.search(normalized):
            return signal
    return None


def _journey_transition_dedupe_key(signal: str, text: str) -> str:
    normalized = " ".join(text.casefold().split())
    detail = "event"
    if "party_pickup_dropoff_join" in signal:
        if re.search(r"(?:drop|\ub0b4\ub824\s*\ub4dc\ub9ac|\ubaa8\uc154\ub2e4|\ubc14\ub798\ub2e4|\ub370\ub824\ub2e4)", normalized):
            detail = "dropoff"
        elif re.search(r"(?:join|\bmet\b|\ud569\ub958|\ub9cc\ub098\uc11c)", normalized):
            detail = "join"
        else:
            detail = "pickup"
    elif "rental_pickup_return" in signal:
        detail = "return" if re.search(r"(?:return|\ubc18\ub0a9|\ub3cc\ub824)", normalized) else "pickup"
    elif "lodging_checkin_checkout" in signal:
        detail = "checkout" if re.search(r"(?:check\s*out|\uccb4\ud06c\s*\uc544\uc6c3)", normalized) else "checkin"
    elif "transit_arrival_departure" in signal:
        detail = (
            "departure"
            if re.search(r"(?:left|leav|depart|\ucd9c\ubc1c|\ub5a0\ub098|\ub098\uc654|\ub098\uc635)", normalized)
            else "arrival"
        )
    elif "transport_boarding_alighting" in signal:
        action = (
            "alighting"
            if re.search(r"(?:got\s+off|getting\s+off|got\s+out|stepped\s+off|\ub0b4\ub838|\ub0b4\ub824|\ud558\ucc28)", normalized)
            else "boarding"
        )
        mode_match = re.search(rf"{_KO_TRANSPORT}|{_EN_TRANSPORT}", normalized)
        mode = mode_match.group(0) if mode_match is not None else "transport"
        detail = f"{action}:{mode}"
    return f"{signal}:{detail}"


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
                "selection_mode": "all",
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


def _required_meal_events_payload(
    events: list[_MealEvent],
    candidates: list[Candidate],
) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for event in events:
        candidate_ids = [
            candidate.candidate_id
            for candidate in candidates
            if event.event_id in candidate.required_meal_event_ids
        ]
        if not candidate_ids:
            raise VideoSummaryError(
                f"필수 식사 사건 후보를 만들지 못했습니다: {event.event_id}"
            )
        option_ranges: list[dict[str, Any]] = []
        for option in event.options:
            option_candidate_ids = [
                candidate.candidate_id
                for candidate in candidates
                if candidate.clip_id == option.clip_id
                and event.event_id in candidate.required_meal_event_ids
                and _ranges_overlap(candidate.start, candidate.end, option.start, option.end)
            ]
            if not option_candidate_ids:
                raise VideoSummaryError(
                    f"필수 식사 사건 옵션 후보를 만들지 못했습니다: {event.event_id}"
                )
            option_ranges.append(
                {
                    "clip_id": option.clip_id,
                    "start": option.start,
                    "end": option.end,
                    "confidence": option.confidence,
                    "signals": list(option.signals),
                    "candidate_ids": option_candidate_ids,
                }
            )
        context_groups: list[dict[str, Any]] = []
        for stage, options in (
            ("setup", event.setup_options),
            ("closure", event.closure_options),
        ):
            if not options:
                continue
            context_id = _meal_context_id(event.event_id, stage)
            context_candidate_ids = [
                candidate.candidate_id
                for candidate in candidates
                if context_id in candidate.required_meal_context_ids
            ]
            if not context_candidate_ids:
                raise VideoSummaryError(
                    f"필수 식사 {stage} 맥락 후보를 만들지 못했습니다: {event.event_id}"
                )
            context_groups.append(
                {
                    "context_id": context_id,
                    "stage": stage,
                    "selection_mode": "one_of",
                    "candidate_ids": context_candidate_ids,
                    "ranges": [
                        {
                            "clip_id": option.clip_id,
                            "start": option.start,
                            "end": option.end,
                            "confidence": option.confidence,
                            "signals": list(option.signals),
                        }
                        for option in options
                    ],
                }
            )
        payload.append(
            {
                "event_id": event.event_id,
                "kind": "meal",
                "selection_mode": "one_of",
                "subtype": event.subtype,
                "day_key": event.day_key,
                "travel_day": event.travel_day,
                "confidence": event.confidence,
                "signals": list(event.signals),
                "candidate_ids": candidate_ids,
                "option_ranges": option_ranges,
                "context_groups": context_groups,
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
    transition_windows: list[tuple[float, float, str]] | None = None,
    meal_windows: list[tuple[float, float, str]] | None = None,
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
    # Waypoints are added after the ordinary max-per-clip selection so a
    # meaningful mid-clip handoff cannot disappear merely because the clip's
    # opener, closer, or a visually stronger window consumed the limit.
    selected.extend(
        transition_windows
        if transition_windows is not None
        else _detect_journey_transition_windows(clip, cues)
    )
    # Meal bodies and their detected setup/closure beats are semantic options,
    # added after the ordinary per-clip cap so the full micro-story survives.
    selected.extend(meal_windows or [])
    return _partition_full_clip_coverage(
        _merge_overlapping_windows(selected),
        clip.duration,
    )


def _partition_full_clip_coverage(
    selected: list[tuple[float, float, str]],
    duration: float,
) -> list[tuple[float, float, str]]:
    """Keep every source interval auditable without making it mandatory.

    Semantic and scored candidates retain their exact windows.  Any uncovered
    source time is split into bounded ``coverage`` candidates so later planning
    can explicitly keep, speed up, compact, or omit it instead of silently
    losing the interval before event construction.
    """
    if duration <= 0:
        return []
    ordered = sorted(selected, key=lambda item: (item[0], item[1], item[2]))
    result: list[tuple[float, float, str]] = []
    cursor = 0.0

    def append_gap(start: float, end: float) -> None:
        gap = end - start
        if gap <= 0.001:
            return
        part_count = max(1, math.ceil(gap / MAX_CANDIDATE_DURATION_SECONDS))
        for index in range(part_count):
            part_start = start + gap * index / part_count
            part_end = start + gap * (index + 1) / part_count
            result.append((part_start, part_end, "coverage"))

    for start, end, origin in ordered:
        bounded_start = max(0.0, min(duration, start))
        bounded_end = max(bounded_start, min(duration, end))
        append_gap(cursor, bounded_start)
        if bounded_end - bounded_start > 0.001:
            result.append((bounded_start, bounded_end, origin))
            cursor = max(cursor, bounded_end)
    append_gap(cursor, duration)
    return sorted(result, key=lambda item: (item[0], item[1], item[2]))


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
    semantic_origins = {"transition", "meal", "meal_setup", "meal_closure"}
    for component in components:
        component_start = min(item[0] for item in component)
        component_end = max(item[1] for item in component)
        boundaries = {component_start, component_end}
        if any(origin in semantic_origins for _, _, origin in component):
            for start, end, origin in component:
                if origin not in semantic_origins:
                    continue
                boundaries.update(
                    {
                        max(component_start, min(component_end, start)),
                        max(component_start, min(component_end, end)),
                    }
                )
        ordered_boundaries = sorted(boundaries)
        annotated_ranges = [
            (
                start,
                end,
                frozenset(
                    origin
                    for item_start, item_end, origin in component
                    if origin in semantic_origins
                    and min(end, item_end) - max(start, item_start) > tolerance
                ),
            )
            for start, end in zip(ordered_boundaries, ordered_boundaries[1:])
            if end > start
        ]
        coalesced_ranges: list[tuple[float, float, frozenset[str]]] = []
        for start, end, semantic_kinds in annotated_ranges:
            if coalesced_ranges and coalesced_ranges[-1][2] == semantic_kinds:
                previous_start, _, _ = coalesced_ranges[-1]
                coalesced_ranges[-1] = (previous_start, end, semantic_kinds)
            else:
                coalesced_ranges.append((start, end, semantic_kinds))

        # Partitioning at semantic boundaries must not create an unusably short
        # ordinary candidate. Absorb only the sub-0.75s exterior padding into
        # the adjacent mandatory transition, preserving the exact source union.
        range_index = 0
        while range_index < len(coalesced_ranges):
            start, end, semantic_kinds = coalesced_ranges[range_index]
            if semantic_kinds or end - start >= 0.75:
                range_index += 1
                continue
            previous_semantic = (
                coalesced_ranges[range_index - 1][2]
                if range_index > 0
                else frozenset()
            )
            next_semantic = (
                coalesced_ranges[range_index + 1][2]
                if range_index + 1 < len(coalesced_ranges)
                else frozenset()
            )
            if previous_semantic and next_semantic:
                previous_start = coalesced_ranges[range_index - 1][0]
                next_end = coalesced_ranges[range_index + 1][1]
                coalesced_ranges[range_index - 1 : range_index + 2] = [
                    (previous_start, next_end, previous_semantic | next_semantic)
                ]
                range_index = max(0, range_index - 1)
            elif next_semantic:
                _, next_end, _ = coalesced_ranges[range_index + 1]
                coalesced_ranges[range_index : range_index + 2] = [
                    (start, next_end, next_semantic)
                ]
            elif previous_semantic:
                previous_start = coalesced_ranges[range_index - 1][0]
                coalesced_ranges[range_index - 1 : range_index + 1] = [
                    (previous_start, end, previous_semantic)
                ]
                range_index = max(0, range_index - 1)
            else:
                range_index += 1

        semantic_ranges = [
            (start, end) for start, end, _ in coalesced_ranges
        ]
        for range_start, range_end in semantic_ranges:
            duration = range_end - range_start
            part_count = max(1, math.ceil(duration / maximum))
            for part_index in range(part_count):
                start = range_start + duration * part_index / part_count
                end = range_start + duration * (part_index + 1) / part_count
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
    overlapping_origins = {
        origin
        for item_start, item_end, origin in component
        if min(end, item_end) - max(start, item_start) > 0.001
    }
    if "transition" in overlapping_origins:
        return "transition"
    if "interview" in overlapping_origins:
        return "interview"
    if "meal" in overlapping_origins:
        return "meal"
    if "meal_setup" in overlapping_origins:
        return "meal_setup"
    if "meal_closure" in overlapping_origins:
        return "meal_closure"
    if part_index == 0 and "opener" in overlapping_origins:
        return "opener"
    if part_index == part_count - 1 and "closer" in overlapping_origins:
        return "closer"
    priority = {
        "interview": 5,
        "transition": 4,
        "meal": 4,
        "meal_setup": 4,
        "meal_closure": 4,
        "speech": 3,
        "visual": 2,
        "opener": 1,
        "closer": 1,
    }
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
    if origin == "transition":
        roles.extend(["transition", "journey"])
    elif origin in {"meal", "meal_setup", "meal_closure"}:
        roles.append("food")
    elif any(word in normalized for word in JOURNEY_WORDS) or origin in {"opener", "closer"}:
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
        "transition": 0.26,
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
