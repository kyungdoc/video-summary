from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Dict


TAXONOMY_FILENAME = "taxonomy.json"

DEFAULT_TAXONOMY: Dict[str, object] = {
    "version": 1,
    "context": "한국어 여행 브이로그 전사입니다. 자막은 짧고 자연스럽게 유지합니다.",
    "prompt_terms": [
        "공항",
        "숙소",
        "리조트",
        "수영장",
        "해변",
        "아침식사",
        "조식",
        "체크인",
        "체크아웃",
        "출발",
        "도착",
        "지도",
        "짐",
        "여행",
    ],
    "terms": [
        {"canonical": "공항", "category": "travel", "aliases": []},
        {"canonical": "숙소", "category": "lodging", "aliases": []},
        {"canonical": "리조트", "category": "lodging", "aliases": []},
        {"canonical": "수영장", "category": "activity", "aliases": []},
        {"canonical": "해변", "category": "activity", "aliases": []},
        {"canonical": "아침식사", "category": "meal", "aliases": []},
        {"canonical": "조식", "category": "meal", "aliases": []},
        {"canonical": "체크인", "category": "travel", "aliases": []},
        {"canonical": "체크아웃", "category": "travel", "aliases": []},
        {"canonical": "출발", "category": "travel", "aliases": []},
        {"canonical": "도착", "category": "travel", "aliases": []},
        {"canonical": "지도", "category": "travel", "aliases": []},
    ],
    "replacement_rules": [
        {"pattern": "호치민", "replacement": "호찌민"},
        {"pattern": "하고싶", "replacement": "하고 싶"},
        {"pattern": "갔다올게요", "replacement": "갔다 올게요"},
        {"pattern": "어디가지\\?", "replacement": "어디 가지?"},
        {"pattern": "우리집", "replacement": "우리 집"},
    ],
}


def build_project_taxonomy(_project_name: str) -> Dict[str, object]:
    return json.loads(json.dumps(DEFAULT_TAXONOMY, ensure_ascii=False))


def ensure_project_taxonomy(build_dir: Path, project_name: str) -> Path:
    build_dir.mkdir(parents=True, exist_ok=True)
    taxonomy_path = build_dir / TAXONOMY_FILENAME
    if taxonomy_path.exists():
        return taxonomy_path
    taxonomy = build_project_taxonomy(project_name)
    taxonomy_path.write_text(
        json.dumps(taxonomy, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return taxonomy_path


def load_project_taxonomy(build_dir: Path, project_name: str) -> Dict[str, object]:
    taxonomy_path = ensure_project_taxonomy(build_dir, project_name)
    data = json.loads(taxonomy_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Invalid taxonomy file: {taxonomy_path}")
    return data


def taxonomy_signature(taxonomy: Dict[str, object]) -> str:
    payload = json.dumps(taxonomy, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
