from __future__ import annotations

import asyncio
import logging
import sqlite3
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from companion_gateway import api
from companion_gateway.config import AppConfig, MemoryConfig, MemoryPluginConfig


@pytest.fixture(scope="session")
def contract_plugin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    mods = tmp_path_factory.mktemp("contract-mods")
    suite = mods / "contract_memory"
    suite.mkdir()
    (suite / "README.md").write_text("contract test plugin\n", encoding="utf-8")
    (suite / "main.py").write_text(
        "import sqlite3\n"
        "print('plugin imported', flush=True)\n"
        "\n"
        "__all__ = ['ingest', 'ingest_messages', 'get', 'recent', 'search', 'match_phrase', "
        "'ensure_message_chunks', 'status', 'rebuild_index', 'rebuild_chunks', 'close']\n"
        "\n"
        "def _db(options):\n"
        "    database = options['database_path']\n"
        "    db = sqlite3.connect(database)\n"
        "    db.execute('CREATE TABLE IF NOT EXISTS contract_messages "
        "(id INTEGER PRIMARY KEY, text TEXT NOT NULL)')\n"
        "    db.execute('CREATE TABLE IF NOT EXISTS contract_ingest "
        "(message_id INTEGER PRIMARY KEY, text TEXT NOT NULL)')\n"
        "    db.execute('CREATE TABLE IF NOT EXISTS contract_attempts "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT)')\n"
        "    return db\n"
        "\n"
        "def ingest(request, options):\n"
        "    if options.get('fail'):\n"
        "        raise RuntimeError('configured failure')\n"
        "    db = _db(options)\n"
        "    text = request.content\n"
        "    db.execute('INSERT INTO contract_messages(text) VALUES (?)', (text,))\n"
        "    ident = db.execute('SELECT last_insert_rowid()').fetchone()[0]\n"
        "    db.commit()\n"
        "    db.close()\n"
        "    return {'id': ident, 'duplicate': False, 'conversation_id': 1, 'sha256': 'contract'}\n"
        "\n"
        "def ingest_messages(request, options):\n"
        "    db = _db(options)\n"
        "    db.execute('INSERT INTO contract_attempts DEFAULT VALUES')\n"
        "    db.commit()\n"
        "    if options.get('fail'):\n"
        "        db.close()\n"
        "        raise RuntimeError('configured ingestion failure')\n"
        "    messages = getattr(request, 'messages', None) or ()\n"
        "    ids = []\n"
        "    for message in messages:\n"
        "        ident = int(message['id'])\n"
        "        ids.append(ident)\n"
        "        db.execute('INSERT OR IGNORE INTO contract_ingest(message_id, text) "
        "VALUES (?, ?)', (ident, str(message.get('text', ''))))\n"
        "    db.commit()\n"
        "    db.close()\n"
        "    return {'highest_id': max(ids) if ids else 0}\n"
        "\n"
        "def get(request, options):\n"
        "    db = _db(options)\n"
        "    row = db.execute('SELECT id, text FROM contract_messages WHERE id = ?', "
        "(request.message_id,)).fetchone()\n"
        "    db.close()\n"
        "    return {'message': {'id': row[0], 'text': row[1], 'role': 'user', "
        "'occurred_at': '2026-01-01T00:00:00+00:00'} if row else None}\n"
        "\n"
        "def recent(request, options):\n"
        "    return {'messages': []}\n"
        "\n"
        "def search(request, options):\n"
        "    db = _db(options)\n"
        "    rows = db.execute('SELECT id, text FROM contract_messages WHERE text LIKE ?', "
        "('%' + request.query + '%',)).fetchall()\n"
        "    db.close()\n"
        "    return {'results': [{'messages': [{'id': row[0], 'text': row[1], 'role': 'user', "
        "'occurred_at': '2026-01-01T00:00:00+00:00'} for row in rows]}] if rows else []}\n"
        "\n"
        "def match_phrase(request, options):\n"
        "    return {'deltas': None}\n"
        "\n"
        "def ensure_message_chunks(request, options):\n"
        "    return {'chunks': 0}\n"
        "\n"
        "def status(request, options):\n"
        "    return {'status': {'source': 'contract', 'healthy': True}}\n"
        "\n"
        "def rebuild_index(request, options):\n"
        "    return {'status': {'source': 'contract', 'rebuilt': True}}\n"
        "\n"
        "def rebuild_chunks(request, options):\n"
        "    return {'status': {'source': 'contract', 'chunks': 0}}\n"
        "\n"
        "def close(request, options):\n"
        "    return None\n",
        encoding="utf-8",
    )
    (suite / "pyproject.toml").write_text(
        '[project]\nname = "contract-memory"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n',
        encoding="utf-8",
    )
    subprocess.run(["uv", "--no-config", "lock", "--directory", str(suite)], check=True)
    return mods


def _config(tmp_path: Path, mods: Path, *, fail: bool = False, interval_seconds: int = 30) -> AppConfig:
    return AppConfig(
        data_dir=tmp_path / "data",
        memory=MemoryConfig(recent_messages=8, search_hits=8, context_messages=1, injection_max_chars=20_000),
        memory_plugin=MemoryPluginConfig(
            module="contract_memory",
            mods_dir=str(mods),
            options={"fail": fail},
            ingest_backfill_interval_seconds=interval_seconds,
        ),
    )


def _rows(database: Path, query: str) -> list:
    if not database.exists():
        return []
    db = sqlite3.connect(database)
    try:
        return db.execute(query).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        db.close()


async def _wait_for(predicate, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.1)
    return predicate()


@asynccontextmanager
async def _client(config: AppConfig):
    app = api.create_app(config)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, app


@pytest.mark.asyncio
async def test_configured_third_party_function_contract_works_and_restarts(
    tmp_path: Path, contract_plugin: Path
):
    config = _config(tmp_path, contract_plugin)
    async with _client(config) as (client, app):
        response = await client.post(
            "/state/v1/messages",
            json={"harness": "e2e", "conversation_id": "one", "role": "user", "content": "isolated contract"},
        )
        assert response.status_code == 200
        message_id = response.json()["id"]
        search = await client.post("/state/v1/memory/search", json={"query": "isolated contract"})
        assert search.status_code == 200
        assert search.json()["results"]
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json()["memory_index"] == {"source": "contract", "healthy": True}
        rebuilt_index = app.state.service.memory.rebuild_index()
        assert rebuilt_index.status == {"source": "contract", "rebuilt": True}
        rebuilt_chunks = app.state.service.memory.rebuild_chunks()
        assert rebuilt_chunks.status == {"source": "contract", "chunks": 0}

    async with _client(config) as (client, _):
        fetched = await client.get(f"/state/v1/messages/{message_id}")
        assert fetched.status_code == 200
        assert fetched.json()["text"] == "isolated contract"


@pytest.mark.asyncio
async def test_configured_plugin_ingest_failure_keeps_core_message_and_catch_up_succeeds(
    tmp_path: Path, contract_plugin: Path, caplog: pytest.LogCaptureFixture
):
    database = tmp_path / "data" / "state.sqlite3"
    failing = _config(tmp_path, contract_plugin, fail=True, interval_seconds=1)

    with caplog.at_level(logging.WARNING, logger="companion_gateway"):
        async with _client(failing) as (client, _):
            response = await client.post(
                "/state/v1/messages",
                json={"harness": "e2e", "conversation_id": "one", "role": "user", "content": "failure"},
            )
            assert response.status_code == 200
            message_id = response.json()["id"]
            fetched = await client.get(f"/state/v1/messages/{message_id}")
            assert fetched.status_code == 200
            assert fetched.json()["text"] == "failure"

    assert _rows(database, "SELECT message_id FROM contract_ingest") == []
    assert _rows(database, "SELECT COUNT(*) FROM contract_attempts")[0][0] >= 1
    assert _rows(database, "SELECT last_acknowledged_id FROM plugin_ingest_state") == []
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "companion_gateway" and record.levelno >= logging.WARNING
    ]
    assert any("ingestion failed" in message for message in warnings)
    assert all("failure" not in message for message in warnings)

    healthy = _config(tmp_path, contract_plugin, fail=False, interval_seconds=1)
    async with _client(healthy) as (client, _):
        received = await _wait_for(
            lambda: _rows(database, "SELECT message_id FROM contract_ingest") == [(message_id,)]
        )
        assert received
        fetched = await client.get(f"/state/v1/messages/{message_id}")
        assert fetched.status_code == 200
        assert fetched.json()["text"] == "failure"

    assert _rows(database, "SELECT message_id, text FROM contract_ingest") == [(message_id, "failure")]
    acknowledged = _rows(database, "SELECT last_acknowledged_id FROM plugin_ingest_state")
    assert acknowledged and acknowledged[0][0] == message_id
