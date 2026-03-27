from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Dict, List


@dataclass
class ClipInfo:
    filename: str
    path: str
    start_time: datetime
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    size_bytes: int
    bitrate_mbps: float
    date_key: str
    timezone_name: str = ""
    location_id: str = ""
    route_phase: str = ""
    route_label: str = ""
    travel_day: int = 0
    date_group_label: str = ""
    score: float = 1.0
    llm_score_bias: float = 0.0

    @property
    def is_vertical(self) -> bool:
        return self.height > self.width

    def to_dict(self) -> Dict[str, object]:
        data = asdict(self)
        data["start_time"] = self.start_time.isoformat()
        data["is_vertical"] = self.is_vertical
        return data


@dataclass
class TimelinePlan:
    title: str
    fps: float
    target_duration: float
    items: List[Dict[str, object]]
    days: List[Dict[str, object]]
    chapters: List[Dict[str, object]]
    notes: List[str]

    def to_dict(self) -> Dict[str, object]:
        return {
            "title": self.title,
            "fps": self.fps,
            "target_duration": self.target_duration,
            "items": self.items,
            "days": self.days,
            "chapters": self.chapters,
            "notes": self.notes,
        }
