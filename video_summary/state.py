from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import fcntl

from .utils import VideoSummaryError


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS stages (
                    stage TEXT PRIMARY KEY,
                    cache_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    error TEXT
                )
                """
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def is_complete(self, stage: str, cache_key: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT status, cache_key FROM stages WHERE stage = ?", (stage,)
            ).fetchone()
        return bool(row and row[0] == "complete" and row[1] == cache_key)

    def mark_running(self, stage: str, cache_key: str, metadata: dict[str, Any] | None = None) -> None:
        self._upsert(stage, cache_key, "running", metadata or {}, None)

    def mark_complete(self, stage: str, cache_key: str, metadata: dict[str, Any] | None = None) -> None:
        self._upsert(stage, cache_key, "complete", metadata or {}, None)

    def mark_failed(self, stage: str, cache_key: str, error: str) -> None:
        self._upsert(stage, cache_key, "failed", {}, error)

    def mark_fallback(self, stage: str, cache_key: str, metadata: dict[str, Any], error: str) -> None:
        """Persist a usable fallback without treating it as a cache hit on the next run."""
        self._upsert(stage, cache_key, "fallback", metadata, error)

    def rows(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            records = connection.execute(
                "SELECT stage, cache_key, status, updated_at, metadata, error FROM stages ORDER BY stage"
            ).fetchall()
        return [
            {
                "stage": row[0],
                "cache_key": row[1],
                "status": row[2],
                "updated_at": row[3],
                "metadata": json.loads(row[4]),
                "error": row[5],
            }
            for row in records
        ]

    def _upsert(
        self,
        stage: str,
        cache_key: str,
        status: str,
        metadata: dict[str, Any],
        error: str | None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO stages(stage, cache_key, status, updated_at, metadata, error)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(stage) DO UPDATE SET
                    cache_key=excluded.cache_key,
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    metadata=excluded.metadata,
                    error=excluded.error
                """,
                (stage, cache_key, status, now, json.dumps(metadata, ensure_ascii=False), error),
            )


@contextmanager
def project_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.seek(0)
            owner = handle.read().strip() or "unknown"
            raise VideoSummaryError(f"같은 프로젝트가 이미 실행 중입니다 (PID {owner}).") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
