from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS companions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    companion_id TEXT NOT NULL REFERENCES companions(id) ON DELETE CASCADE,
    harness TEXT NOT NULL,
    external_id TEXT NOT NULL,
    route TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(companion_id, harness, external_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    companion_id TEXT NOT NULL REFERENCES companions(id) ON DELETE CASCADE,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system', 'tool', 'event')),
    text TEXT NOT NULL,
    content_json TEXT NOT NULL,
    external_id TEXT,
    occurred_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    sha256 TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS messages_external_id
ON messages(conversation_id, external_id)
WHERE external_id IS NOT NULL AND external_id != '';
CREATE INDEX IF NOT EXISTS messages_conversation_time
ON messages(conversation_id, occurred_at, id);
CREATE INDEX IF NOT EXISTS messages_companion_time
ON messages(companion_id, occurred_at, id);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text,
    content='messages',
    content_rowid='id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text)
    VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF text ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text)
    VALUES ('delete', old.id, old.text);
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE IF NOT EXISTS affect_state (
    companion_id TEXT PRIMARY KEY REFERENCES companions(id) ON DELETE CASCADE,
    state_json TEXT NOT NULL,
    last_updated_at TEXT NOT NULL,
    last_user_message_at TEXT,
    last_interaction_at TEXT,
    last_proactive_sent_at TEXT,
    unanswered_proactive INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS affect_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    companion_id TEXT NOT NULL REFERENCES companions(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    source_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    deltas_json TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL,
    follow_up_at TEXT,
    follow_up_expires_at TEXT,
    follow_up_consumed_at TEXT
);
CREATE INDEX IF NOT EXISTS affect_events_follow_up
ON affect_events(companion_id, follow_up_at, follow_up_consumed_at);

CREATE TABLE IF NOT EXISTS proactive_events (
    id TEXT PRIMARY KEY,
    companion_id TEXT NOT NULL REFERENCES companions(id) ON DELETE CASCADE,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
    route TEXT NOT NULL,
    reason TEXT NOT NULL,
    dedup_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    consumer TEXT,
    available_at TEXT NOT NULL,
    lease_until TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sent_at TEXT,
    last_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS proactive_events_poll
ON proactive_events(status, available_at, created_at);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

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
