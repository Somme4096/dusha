from __future__ import annotations

import fcntl
import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("dusha.outbox")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    body_json TEXT NOT NULL,
    queued_at TEXT NOT NULL
);
"""
_RETRY = frozenset({401, 408, 429, 502, 504})


def retryable(error: httpx.HTTPError) -> bool:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in _RETRY
    return isinstance(error, httpx.RequestError)


class Outbox:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "outbox.sqlite3"
        self.lock = data_dir / "outbox.lock"
        with self._connect() as db:
            db.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            yield db
            db.commit()
        finally:
            db.close()

    def put(self, path: str, body: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO pending(path, body_json, queued_at) VALUES(?,?,?)",
                (path, json.dumps(body, ensure_ascii=False), datetime.now(UTC).isoformat(timespec="seconds")),
            )

    def size(self) -> int:
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM pending").fetchone()[0])

    def flush(self, client: httpx.Client) -> int:
        with open(self.lock, "w") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            sent = 0
            while True:
                with self._connect() as db:
                    row = db.execute("SELECT id, path, body_json FROM pending ORDER BY id LIMIT 1").fetchone()
                if row is None:
                    return sent
                try:
                    response = client.post(row[1], json=json.loads(row[2]))
                except httpx.RequestError:
                    return sent
                if response.status_code in _RETRY:
                    return sent
                if response.status_code >= 400:
                    logger.warning("dropped queued %s: %s %s", row[1], response.status_code, response.text)
                with self._connect() as db:
                    db.execute("DELETE FROM pending WHERE id=?", (row[0],))
                sent += 1
