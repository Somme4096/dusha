from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 5

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

CREATE TABLE IF NOT EXISTS transcript_nodes (
    message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    parent_id INTEGER REFERENCES transcript_nodes(message_id) ON DELETE CASCADE,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    key TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS transcript_children ON transcript_nodes(parent_id);
CREATE INDEX IF NOT EXISTS transcript_roots ON transcript_nodes(conversation_id) WHERE parent_id IS NULL;

PRAGMA user_version = 5;
"""

class _ClosingConnection:
    """Commits or rolls back like sqlite3's context manager, then closes the connection."""

    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection

    def __enter__(self) -> sqlite3.Connection:
        return self._connection

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is None:
                self._connection.commit()
            else:
                self._connection.rollback()
        finally:
            self._connection.close()
        return False


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prepare_schema()
        self.migrate(SCHEMA)

    def _prepare_schema(self) -> None:
        if not self.path.exists():
            return
        with _ClosingConnection(sqlite3.connect(self.path)) as db:
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            initialized = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
            ).fetchone()
        if initialized and version != SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema version {version} is incompatible. "
                f"Use an empty data directory or restore a schema version {SCHEMA_VERSION} backup"
            )

    def migrate(self, script: str) -> None:
        with self.connect() as db:
            db.executescript(script)

    def connect(self) -> _ClosingConnection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA foreign_keys=ON")
        return _ClosingConnection(db)

    def integrity_check(self) -> str:
        with self.connect() as db:
            return str(db.execute("PRAGMA integrity_check").fetchone()[0])

    def backup(self, destination: str | Path) -> Path:
        target = Path(destination).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source, _ClosingConnection(sqlite3.connect(target)) as output:
            source.backup(output)
        return target
