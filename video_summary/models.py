from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


SCHEMA_VERSION = 1


@dataclass(slots=True)
class Clip:
    clip_id: str
    path: str
    relative_path: str
    fingerprint: str
    size_bytes: int
    duration: float
    captured_at: str
    capture_source: str
    day_key: str
    travel_day: int
    width: int
    height: int
    fps: float
    codec: str
    rotation: int
    has_audio: bool
    audio_sample_rate: int | None = None
    location: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Clip":
        fields = cls.__dataclass_fields__
        return cls(**{key: value[key] for key in fields if key in value})


@dataclass(slots=True)
class TranscriptCue:
    start: float
    end: float
    text: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TranscriptCue":
        return cls(start=float(value["start"]), end=float(value["end"]), text=str(value["text"]).strip())


@dataclass(slots=True)
class Candidate:
    candidate_id: str
    clip_id: str
    day_key: str
    travel_day: int
    start: float
    end: float
    captured_at: str
    transcript: str
    roles: list[str]
    score: float
    speech_ratio: float
    motion_score: float
    visual_quality: float
    location: str | None
    frame_path: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["duration"] = round(self.duration, 3)
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Candidate":
        fields = cls.__dataclass_fields__
        return cls(**{key: value[key] for key in fields if key in value})


@dataclass(slots=True)
class PlanSegment:
    candidate_id: str
    role: str
    reason: str
    location: str | None = None
    caption: str | None = None
    speed: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Episode:
    day_key: str
    travel_day: int
    title: str
    subtitle: str
    summary: str
    target_duration: float
    segments: list[PlanSegment]

    def to_dict(self) -> dict[str, Any]:
        return {
            **{key: value for key, value in asdict(self).items() if key != "segments"},
            "segments": [segment.to_dict() for segment in self.segments],
        }


@dataclass(slots=True)
class EditPlan:
    project: str
    prompt: str
    planner: str
    candidate_set_hash: str
    episodes: list[Episode]
    version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "project": self.project,
            "prompt": self.prompt,
            "planner": self.planner,
            "candidate_set_hash": self.candidate_set_hash,
            "episodes": [episode.to_dict() for episode in self.episodes],
        }
