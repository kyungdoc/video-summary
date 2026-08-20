from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable


class VideoSummaryError(RuntimeError):
    """A user-facing pipeline error."""


def slugify(value: str) -> str:
    normalized = re.sub(r"[^\w\-]+", "-", value.strip().lower(), flags=re.UNICODE)
    normalized = re.sub(r"[-_]{2,}", "-", normalized).strip("-_")
    return normalized or "trip"


def stable_hash(value: Any, length: int = 20) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def file_fingerprint(path: Path) -> str:
    stat = path.stat()
    digest = hashlib.sha256()
    chunk_size = 64 * 1024
    with path.open("rb") as handle:
        if stat.st_size <= chunk_size * 4:
            while chunk := handle.read(chunk_size):
                digest.update(chunk)
        else:
            for offset in (0, max(0, stat.st_size // 2 - chunk_size // 2), max(0, stat.st_size - chunk_size)):
                handle.seek(offset)
                digest.update(handle.read(chunk_size))
    return stable_hash(
        {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sample_sha256": digest.hexdigest()},
        length=24,
    )


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".partial", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def run_command(
    args: list[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    capture: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            cwd=cwd,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            check=True,
            env=env,
        )
    except FileNotFoundError as exc:
        raise VideoSummaryError(f"필요한 명령을 찾지 못했습니다: {args[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        if len(detail) > 3000:
            detail = detail[-3000:]
        raise VideoSummaryError(f"명령 실행 실패 ({args[0]}): {detail or f'exit {exc.returncode}'}") from exc


def print_status(message: str) -> None:
    print(f"[video-summary] {message}", file=sys.stderr, flush=True)


def unique_preserving_order(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
