from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 3

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    harness TEXT NOT NULL,
    external_id TEXT NOT NULL,
    route TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(harness, external_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
CREATE INDEX IF NOT EXISTS messages_time
ON messages(occurred_at, id);

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

CREATE TABLE IF NOT EXISTS memory_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    chunker_key TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    start_char INTEGER NOT NULL CHECK(start_char >= 0),
    end_char INTEGER NOT NULL CHECK(end_char > start_char),
    created_at TEXT NOT NULL,
    UNIQUE(message_id, chunker_key, ordinal)
);
CREATE INDEX IF NOT EXISTS memory_chunks_message
ON memory_chunks(message_id, chunker_key, ordinal);

CREATE TABLE IF NOT EXISTS memory_chunk_embeddings (
    chunk_id INTEGER NOT NULL REFERENCES memory_chunks(id) ON DELETE CASCADE,
    embedding_key TEXT NOT NULL,
    dimensions INTEGER NOT NULL CHECK(dimensions > 0),
    embedding BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(chunk_id, embedding_key)
);
CREATE INDEX IF NOT EXISTS memory_chunk_embeddings_key
ON memory_chunk_embeddings(embedding_key, dimensions, chunk_id);

CREATE TABLE IF NOT EXISTS evergreen_fact_revisions (
    revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision > 0),
    fact_key TEXT NOT NULL,
    text TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('active', 'forgotten')),
    priority INTEGER NOT NULL DEFAULT 50 CHECK(priority BETWEEN 0 AND 100),
    source_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    reason TEXT NOT NULL DEFAULT '',
    review_after TEXT,
    expires_at TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL CHECK(created_by IN ('agent', 'operator', 'api')),
    UNIQUE(fact_id, revision)
);
CREATE INDEX IF NOT EXISTS evergreen_fact_key
ON evergreen_fact_revisions(fact_key, revision DESC);
CREATE INDEX IF NOT EXISTS evergreen_fact_history
ON evergreen_fact_revisions(fact_id, revision);

CREATE TABLE IF NOT EXISTS affect_state (
    id INTEGER PRIMARY KEY CHECK(id = 1),
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
ON affect_events(follow_up_at, follow_up_consumed_at);

CREATE TABLE IF NOT EXISTS affect_classifications (
    source_message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    automatic_label TEXT NOT NULL,
    agent_label TEXT,
    chosen_label TEXT,
    decision_source TEXT CHECK(decision_source IN ('agent', 'automatic', 'provided')),
    status TEXT NOT NULL CHECK(status IN ('pending', 'applied')),
    occurred_at TEXT NOT NULL,
    finalize_after TEXT NOT NULL,
    resolved_at TEXT,
    affect_event_id INTEGER REFERENCES affect_events(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS affect_classifications_pending
ON affect_classifications(status, finalize_after, source_message_id);

CREATE TABLE IF NOT EXISTS proactive_events (
    id TEXT PRIMARY KEY,
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

PRAGMA user_version = 3;
"""

MIGRATE_2_TO_3 = """
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS affect_classifications (
    source_message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    automatic_label TEXT NOT NULL,
    agent_label TEXT,
    chosen_label TEXT,
    decision_source TEXT CHECK(decision_source IN ('agent', 'automatic', 'provided')),
    status TEXT NOT NULL CHECK(status IN ('pending', 'applied')),
    occurred_at TEXT NOT NULL,
    finalize_after TEXT NOT NULL,
    resolved_at TEXT,
    affect_event_id INTEGER REFERENCES affect_events(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS affect_classifications_pending
ON affect_classifications(status, finalize_after, source_message_id);

PRAGMA user_version = 3;
COMMIT;
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prepare_schema()
        with self.connect() as db:
            db.executescript(SCHEMA)

    def _prepare_schema(self) -> None:
        if not self.path.exists():
            return
        with sqlite3.connect(self.path) as db:
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            initialized = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
            ).fetchone()
        if not initialized or version == SCHEMA_VERSION:
            return
        if version == 2:
            with self.connect() as db:
                db.executescript(MIGRATE_2_TO_3)
            return
        if initialized:
            raise RuntimeError(
                f"database schema version {version} is incompatible; "
                "use an empty data directory or restore a schema version 2 or 3 backup"
            )

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
