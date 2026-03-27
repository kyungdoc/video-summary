from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha1
from pathlib import Path
from typing import Dict, List


PARTIALS_DIRNAME = "clip_results"


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def clip_result_key(clip_path: str) -> str:
    path = Path(clip_path)
    digest = sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:10]
    return f"{path.stem}-{digest}"


def clip_results_dir(build_dir: Path) -> Path:
    return build_dir / PARTIALS_DIRNAME


def clip_result_path(build_dir: Path, clip_path: str) -> Path:
    return clip_results_dir(build_dir) / f"{clip_result_key(clip_path)}.json"


def load_clip_result(build_dir: Path, clip_path: str) -> Dict[str, object]:
    path = clip_result_path(build_dir, clip_path)
    if not path.exists():
        return {
            "clip_path": clip_path,
            "filename": Path(clip_path).name,
        }
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {"clip_path": clip_path, "filename": Path(clip_path).name}


def update_clip_result(build_dir: Path, clip_path: str, patch: Dict[str, object]) -> Path:
    data = load_clip_result(build_dir, clip_path)
    data.update(patch)
    data["clip_path"] = clip_path
    data["filename"] = Path(clip_path).name
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    path = clip_result_path(build_dir, clip_path)
    _write_json(path, data)
    return path


def load_all_clip_results(build_dir: Path) -> List[Dict[str, object]]:
    results = []
    for path in sorted(clip_results_dir(build_dir).glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            results.append(data)
    results.sort(key=lambda item: (str(item.get("first_source_time", "")), str(item.get("filename", ""))))
    return results
