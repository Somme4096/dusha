from __future__ import annotations

import sqlite3
from pathlib import Path

_EXTENSION_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS everos_sessions (
    conversation_id INTEGER PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL UNIQUE,
    app_id TEXT NOT NULL,
    project_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS everos_session_map (
    harness TEXT NOT NULL,
    external_conversation_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    app_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    PRIMARY KEY(harness, external_conversation_id)
);
CREATE INDEX IF NOT EXISTS everos_session_map_session
ON everos_session_map(session_id);
CREATE INDEX IF NOT EXISTS everos_session_map_external
ON everos_session_map(external_conversation_id);

CREATE TABLE IF NOT EXISTS everos_outbox (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL UNIQUE REFERENCES messages(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    app_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    delivered_at TEXT,
    flush_generation INTEGER NOT NULL DEFAULT 0,
    processed_at TEXT
);
CREATE INDEX IF NOT EXISTS everos_outbox_pending
ON everos_outbox(delivered_at, id);
CREATE UNIQUE INDEX IF NOT EXISTS everos_outbox_session_timestamp
ON everos_outbox(session_id, timestamp);

CREATE TABLE IF NOT EXISTS everos_flush_state (
    app_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    requested_generation INTEGER NOT NULL DEFAULT 0,
    processed_generation INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    last_result TEXT,
    due_at TEXT,
    PRIMARY KEY(app_id, project_id, session_id)
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_extension_tables()

    def _ensure_extension_tables(self) -> None:
        with self.connect() as db:
            db.executescript(_EXTENSION_SCHEMA)
            columns = {row[1] for row in db.execute("PRAGMA table_info(everos_outbox)")}
            if "flush_generation" not in columns:
                db.execute("ALTER TABLE everos_outbox ADD COLUMN flush_generation INTEGER NOT NULL DEFAULT 0")
            if "processed_at" not in columns:
                db.execute("ALTER TABLE everos_outbox ADD COLUMN processed_at TEXT")

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def integrity_check(self) -> str:
        with self.connect() as db:
            return str(db.execute("PRAGMA integrity_check").fetchone()[0])

    def backup(self, destination: str | Path) -> Path:
        target = Path(destination).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source, sqlite3.connect(target) as output:
            source.backup(output)
        return target
