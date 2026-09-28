from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from companion_gateway import api
from companion_gateway.config import (
    AppConfig,
    EvergreenConfig,
    MemoryConfig,
    MemoryPluginConfig,
    StorageConfig,
    load_config,
)

BOUNDARY_PLUGIN_SOURCE = '''\
import json
import sqlite3


def _connect(options):
    db = sqlite3.connect(options["database_path"], timeout=10)
    db.execute(
        "CREATE TABLE IF NOT EXISTS boundary_batches "
        "(seq INTEGER PRIMARY KEY AUTOINCREMENT, ids TEXT NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS boundary_outbox "
        "(message_id INTEGER PRIMARY KEY, occurred_at TEXT NOT NULL, "
        "role TEXT NOT NULL, text TEXT NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS boundary_inject "
        "(query TEXT NOT NULL, scope TEXT NOT NULL, max_chars INTEGER NOT NULL)"
    )
    return db


def ingest_messages(request, options):
    db = _connect(options)
    if options.get("fail"):
        db.close()
        raise RuntimeError("boundary plugin transient failure")
    messages = getattr(request, "messages", None) or ()
    ids = []
    try:
        for message in messages:
            ident = int(message["id"])
            ids.append(ident)
            db.execute(
                "INSERT OR IGNORE INTO boundary_outbox"
                "(message_id, occurred_at, role, text) VALUES (?, ?, ?, ?)",
                (
                    ident,
                    str(message.get("occurred_at", "")),
                    str(message.get("role", "")),
                    str(message.get("text", "")),
                ),
            )
        if ids:
            db.execute("INSERT INTO boundary_batches(ids) VALUES (?)", (json.dumps(ids),))
        db.commit()
    finally:
        db.close()
    return {"highest_id": max(ids) if ids else 0}


def inject_context(request, options):
    db = _connect(options)
    try:
        db.execute(
            "INSERT INTO boundary_inject(query, scope, max_chars) VALUES (?, ?, ?)",
            (
                str(getattr(request, "query", "")),
                str(getattr(request, "scope", "")),
                int(getattr(request, "max_chars", 0) or 0),
            ),
        )
        db.commit()
    finally:
        db.close()
    return {
        "text": "BOUNDARY_PLUGIN_CONTEXT" + ("x" * 4000),
        "records": [{"source": "boundary", "text": "BOUNDARY_PLUGIN_RECORD"}],
    }


def status(request, options):
    return {"status": {"source": "boundary", "healthy": True}}


def ensure_message_chunks(request, options):
    return {"chunks": 0}


def rebuild_chunks(request, options):
    return {"status": {"source": "boundary", "chunks": 0}}


def rebuild_index(request, options):
    return {"status": {"source": "boundary", "rebuilt": True}}


def backfill_once(request, options):
    return {"status": {"source": "boundary", "backfilled": True}}


def match_phrase(request, options):
    return {"deltas": None}


def close(request, options):
    return None
'''


@pytest.fixture(scope="session")
def boundary_plugin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    mods = tmp_path_factory.mktemp("boundary-mods")
    suite = mods / "boundary_memory"
    suite.mkdir()
    (suite / "README.md").write_text("boundary e2e plugin\n", encoding="utf-8")
    (suite / "main.py").write_text(BOUNDARY_PLUGIN_SOURCE, encoding="utf-8")
    (suite / "pyproject.toml").write_text(
        '[project]\nname = "boundary-memory"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n',
        encoding="utf-8",
    )
    subprocess.run(["uv", "--no-config", "lock", "--directory", str(suite)], check=True)
    return mods


def _write_json(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _config(
    tmp_path: Path,
    *,
    storage: bool = True,
    mods: Path | None = None,
    fail: bool = False,
    injection_max_chars: int = 20_000,
    plugin_context_max_chars: int = 3_000,
    batch_size: int = 50,
) -> AppConfig:
    plugin = (
        MemoryPluginConfig(
            module="boundary_memory",
            mods_dir=str(mods),
            options={"fail": fail},
            ingest_batch_size=batch_size,
            ingest_backfill_interval_seconds=1,
        )
        if mods is not None
        else MemoryPluginConfig()
    )
    return AppConfig(
        data_dir=tmp_path / "data",
        memory=MemoryConfig(
            recent_messages=8,
            search_hits=8,
            context_messages=1,
            injection_max_chars=injection_max_chars,
            plugin_context_max_chars=plugin_context_max_chars,
        ),
        evergreen=EvergreenConfig(enabled=True, max_items=32, max_chars=4_000),
        storage=StorageConfig(enabled=storage),
        memory_plugin=plugin,
    )


@asynccontextmanager
async def _client(config: AppConfig):
    app = api.create_app(config)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, app


async def _ingest(
    client: httpx.AsyncClient,
    conversation: str,
    content: str,
    occurred_at: str | None = None,
) -> dict:
    body = {"harness": "e2e", "conversation_id": conversation, "role": "user", "content": content}
    if occurred_at is not None:
        body["occurred_at"] = occurred_at
    response = await client.post("/state/v1/messages", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _assert_unavailable(response: httpx.Response) -> None:
    assert response.status_code in (503, 409), response.text
    detail = str(response.json().get("detail", "")).lower()
    assert "storage" in detail, response.text
    assert "disabled" in detail or "unavailable" in detail, response.text


def _cli(config: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    return subprocess.run(
        [sys.executable, "-m", "companion_gateway.cli", "--config", str(config), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _assert_cli_unavailable(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode != 0, result.stdout
    output = (result.stdout + result.stderr).lower()
    assert "storage" in output, result.stderr
    assert "disabled" in output or "unavailable" in output, result.stderr


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


def _outbox_ids(database: Path) -> list[int]:
    return [row[0] for row in _rows(database, "SELECT message_id FROM boundary_outbox ORDER BY message_id")]


def _batches(database: Path) -> list[list[int]]:
    return [json.loads(row[0]) for row in _rows(database, "SELECT ids FROM boundary_batches ORDER BY seq")]


async def _wait_for(predicate, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.1)
    return predicate()


async def test_default_storage_round_trips_messages_and_facts_across_restart(tmp_path: Path):
    config_path = _write_json(tmp_path / "config.json", {"data_dir": str(tmp_path / "data")})
    config = load_config(config_path)
    assert config.storage.enabled is True

    async with _client(config) as (client, _):
        created = await _ingest(client, "builtin", "BUILTIN_MESSAGE_MARKER")
        message_id = created["id"]
        fact = await client.post(
            "/state/v1/evergreen/facts",
            json={"key": "builtin.key", "text": "BUILTIN_FACT_MARKER"},
        )
        assert fact.status_code == 200, fact.text

    async with _client(config) as (client, _):
        fetched = await client.get(f"/state/v1/messages/{message_id}")
        assert fetched.status_code == 200
        assert fetched.json()["text"] == "BUILTIN_MESSAGE_MARKER"
        search = await client.post("/state/v1/memory/search", json={"query": "BUILTIN_MESSAGE_MARKER"})
        assert search.status_code == 200
        assert any(
            message["text"] == "BUILTIN_MESSAGE_MARKER"
            for hit in search.json()["results"]
            for message in hit["messages"]
        )
        facts = await client.get("/state/v1/evergreen/facts")
        assert facts.status_code == 200
        assert any(fact["text"] == "BUILTIN_FACT_MARKER" for fact in facts.json()["facts"])


async def test_storage_disabled_reports_unavailable_and_preserves_data_through_restart(tmp_path: Path):
    enabled_path = _write_json(
        tmp_path / "enabled.json",
        {"data_dir": str(tmp_path / "data"), "storage": {"enabled": True}},
    )
    disabled_path = _write_json(
        tmp_path / "disabled.json",
        {"data_dir": str(tmp_path / "data"), "storage": {"enabled": False}},
    )

    async with _client(load_config(enabled_path)) as (client, _):
        created = await _ingest(client, "preserve", "PRESERVED_MESSAGE_MARKER")
        message_id = created["id"]
        fact = await client.post(
            "/state/v1/evergreen/facts",
            json={"key": "preserve.key", "text": "PRESERVED_FACT_MARKER"},
        )
        assert fact.status_code == 200, fact.text

    async with _client(load_config(disabled_path)) as (client, _):
        _assert_unavailable(await client.post(
            "/state/v1/messages",
            json={"harness": "e2e", "conversation_id": "preserve", "role": "user", "content": "blocked"},
        ))
        _assert_unavailable(await client.get(f"/state/v1/messages/{message_id}"))
        _assert_unavailable(await client.post("/state/v1/memory/search", json={"query": "PRESERVED"}))
        _assert_unavailable(await client.get("/state/v1/evergreen/facts"))
        _assert_unavailable(await client.post(
            "/state/v1/evergreen/facts",
            json={"key": "blocked.key", "text": "blocked"},
        ))

    async with _client(load_config(disabled_path)) as (client, _):
        _assert_unavailable(await client.get(f"/state/v1/messages/{message_id}"))

    async with _client(load_config(enabled_path)) as (client, _):
        fetched = await client.get(f"/state/v1/messages/{message_id}")
        assert fetched.status_code == 200
        assert fetched.json()["text"] == "PRESERVED_MESSAGE_MARKER"
        facts = await client.get("/state/v1/evergreen/facts")
        assert facts.status_code == 200
        assert any(fact["text"] == "PRESERVED_FACT_MARKER" for fact in facts.json()["facts"])


def test_configured_plugin_with_storage_disabled_fails_config(tmp_path: Path, boundary_plugin: Path):
    with pytest.raises(ValueError, match="storage"):
        AppConfig(
            data_dir=tmp_path / "data",
            storage=StorageConfig(enabled=False),
            memory_plugin=MemoryPluginConfig(module="boundary_memory", mods_dir=str(boundary_plugin)),
        )

    config_path = _write_json(
        tmp_path / "config.json",
        {
            "data_dir": str(tmp_path / "data"),
            "storage": {"enabled": False},
            "memory_plugin": {"module": "boundary_memory", "mods_dir": str(boundary_plugin)},
        },
    )
    with pytest.raises(ValueError, match="storage"):
        load_config(config_path)


def test_cli_reports_storage_unavailable(tmp_path: Path):
    config_path = _write_json(
        tmp_path / "config.json",
        {"data_dir": str(tmp_path / "data"), "storage": {"enabled": False}},
    )

    _assert_cli_unavailable(_cli(config_path, "memory", "recent"))
    _assert_cli_unavailable(_cli(config_path, "memory", "show", "1"))
    _assert_cli_unavailable(_cli(config_path, "memory", "search", "anything"))
    _assert_cli_unavailable(_cli(config_path, "evergreen", "list"))
    _assert_cli_unavailable(_cli(config_path, "evergreen", "remember", "key", "value"))


async def test_plugin_ingest_batches_replay_in_id_order_with_retry_and_restart(
    tmp_path: Path, boundary_plugin: Path
):
    data_dir = tmp_path / "data"
    database = data_dir / "state.sqlite3"
    failed = _config(tmp_path, mods=boundary_plugin, fail=True, batch_size=2)
    healthy = _config(tmp_path, mods=boundary_plugin, fail=False, batch_size=2)

    async with _client(failed) as (client, _):
        first = await _ingest(client, "feed", "FIRST_MARKER", occurred_at="2026-01-03T00:00:00+00:00")
        second = await _ingest(client, "feed", "SECOND_MARKER", occurred_at="2026-01-01T00:00:00+00:00")
        third = await _ingest(client, "feed", "THIRD_MARKER", occurred_at="2026-01-02T00:00:00+00:00")

    assert [first["id"], second["id"], third["id"]] == [1, 2, 3]
    assert _outbox_ids(database) == []
    assert _rows(database, "SELECT last_acknowledged_id FROM plugin_ingest_state") == []

    batch_count = 0
    async with _client(healthy) as (client, _):
        await _wait_for(lambda: _outbox_ids(database) == [1, 2, 3])
        batch_count = len(_batches(database))

    assert _outbox_ids(database) == [1, 2, 3]
    batches = _batches(database)
    assert batches
    assert all(len(batch) <= 2 for batch in batches)
    assert any(len(batch) == 2 for batch in batches)
    assert [ident for batch in batches for ident in batch] == [1, 2, 3]
    acknowledged = _rows(database, "SELECT last_acknowledged_id FROM plugin_ingest_state")
    assert acknowledged and acknowledged[0][0] == 3
    rows = _rows(database, "SELECT message_id, occurred_at FROM boundary_outbox ORDER BY message_id")
    assert [row[1] for row in rows] == [
        "2026-01-03T00:00:00+00:00",
        "2026-01-01T00:00:00+00:00",
        "2026-01-02T00:00:00+00:00",
    ]

    db = sqlite3.connect(database)
    db.execute("DELETE FROM plugin_ingest_state")
    db.commit()
    db.close()

    async with _client(healthy) as (client, _):
        await _wait_for(lambda: len(_batches(database)) > batch_count)

    assert _outbox_ids(database) == [1, 2, 3]


async def test_plugin_injects_context_within_budget_without_displacing_builtin_facts(
    tmp_path: Path, boundary_plugin: Path
):
    config = _config(
        tmp_path,
        mods=boundary_plugin,
        fail=False,
        injection_max_chars=3_000,
        plugin_context_max_chars=500,
    )

    async with _client(config) as (client, _):
        await _ingest(client, "vault", "VAULT_RECENT_MARKER")
        fact = await client.post(
            "/state/v1/evergreen/facts",
            json={"key": "vault.key", "text": "BOUNDARY_FACT_MARKER"},
        )
        assert fact.status_code == 200, fact.text

        response = await client.post(
            "/state/v1/context",
            json={"harness": "e2e", "conversation_id": "vault", "query": "vault"},
        )
        assert response.status_code == 200, response.text
        injection = response.json()["injection"]
        assert "BOUNDARY_FACT_MARKER" in injection
        assert "BOUNDARY_PLUGIN_CONTEXT" in injection
        assert "VAULT_RECENT_MARKER" in injection
        assert len(injection) <= 3_000

    database = tmp_path / "data" / "state.sqlite3"
    requests = _rows(database, "SELECT query, scope, max_chars FROM boundary_inject")
    assert requests, "plugin inject_context was not called"
    query, _scope, max_chars = requests[-1]
    assert query == "vault"
    assert 0 < max_chars <= 500


async def test_health_and_index_report_lexical_only_without_plugin(tmp_path: Path):
    config_path = _write_json(
        tmp_path / "config.json",
        {"data_dir": str(tmp_path / "data"), "storage": {"enabled": True}},
    )
    config = load_config(config_path)

    async with _client(config) as (client, _):
        health = await client.get("/health")
        assert health.status_code == 200, health.text
        health_index = health.json()["memory_index"]
        assert health_index["mode"] == "lexical"
        assert health_index["retrieval"] == "lexical"
        assert health_index["plugin_enabled"] is False

        index = await client.get("/state/v1/memory/index")
        assert index.status_code == 200, index.text
        assert index.json()["mode"] == "lexical"
        assert index.json()["retrieval"] == "lexical"
        assert index.json()["plugin_enabled"] is False

    status = _cli(config_path, "memory", "index", "status")
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["retrieval"] == "lexical"

    backfill = _cli(config_path, "memory", "index", "backfill")
    assert backfill.returncode == 0, backfill.stderr
    assert json.loads(backfill.stdout)["enabled"] is False

    rebuild = _cli(config_path, "memory", "index", "rebuild")
    assert rebuild.returncode == 0, rebuild.stderr
    assert json.loads(rebuild.stdout)["status"] == "rebuilt"

    reindex = _cli(config_path, "memory", "reindex")
    assert reindex.returncode == 0, reindex.stderr
    assert json.loads(reindex.stdout) == {"status": "rebuilt"}


async def test_health_and_index_report_plugin_status_with_configured_plugin(
    tmp_path: Path, boundary_plugin: Path
):
    config = _config(tmp_path, mods=boundary_plugin, fail=False)
    config_path = _write_json(
        tmp_path / "config.json",
        {
            "data_dir": str(tmp_path / "data"),
            "memory_plugin": {"module": "boundary_memory", "mods_dir": str(boundary_plugin)},
        },
    )

    async with _client(config) as (client, _):
        health = await client.get("/health")
        assert health.status_code == 200, health.text
        assert health.json()["memory_index"] == {"source": "boundary", "healthy": True}

        index = await client.get("/state/v1/memory/index")
        assert index.status_code == 200, index.text
        assert index.json() == {"source": "boundary", "healthy": True}

    status = _cli(config_path, "memory", "index", "status")
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout) == {"source": "boundary", "healthy": True}

    backfill = _cli(config_path, "memory", "index", "backfill")
    assert backfill.returncode == 0, backfill.stderr
    assert json.loads(backfill.stdout) == {"source": "boundary", "backfilled": True}

    rebuild = _cli(config_path, "memory", "index", "rebuild")
    assert rebuild.returncode == 0, rebuild.stderr
    assert json.loads(rebuild.stdout) == {"source": "boundary", "chunks": 0}

    reindex = _cli(config_path, "memory", "reindex")
    assert reindex.returncode == 0, reindex.stderr
    assert json.loads(reindex.stdout) == {"status": "rebuilt"}


async def test_health_survives_storage_disabled_and_index_reports_unavailable(tmp_path: Path):
    config_path = _write_json(
        tmp_path / "config.json",
        {"data_dir": str(tmp_path / "data"), "storage": {"enabled": False}},
    )
    config = load_config(config_path)

    async with _client(config) as (client, _):
        health = await client.get("/health")
        assert health.status_code == 200, health.text
        assert isinstance(health.json()["memory_index"], dict)

        index = await client.get("/state/v1/memory/index")
        _assert_unavailable(index)

    _assert_cli_unavailable(_cli(config_path, "memory", "index", "status"))
    _assert_cli_unavailable(_cli(config_path, "memory", "index", "backfill"))
    _assert_cli_unavailable(_cli(config_path, "memory", "index", "rebuild"))
    _assert_cli_unavailable(_cli(config_path, "memory", "reindex"))


async def test_version_four_database_recreates_missing_plugin_ingest_state(tmp_path: Path):
    config_path = _write_json(tmp_path / "config.json", {"data_dir": str(tmp_path / "data")})
    config = load_config(config_path)
    database = tmp_path / "data" / "state.sqlite3"

    async with _client(config) as (client, _):
        created = await _ingest(client, "v4", "V4_MESSAGE_MARKER")
        message_id = created["id"]

    assert _rows(database, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='plugin_ingest_state'")
    assert _rows(database, "PRAGMA user_version")[0][0] == 4

    db = sqlite3.connect(database)
    db.execute("DROP TABLE plugin_ingest_state")
    db.commit()
    db.close()
    missing = _rows(
        database,
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='plugin_ingest_state'",
    )
    assert missing == []

    async with _client(config) as (client, _):
        health = await client.get("/health")
        assert health.status_code == 200, health.text
        fetched = await client.get(f"/state/v1/messages/{message_id}")
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["text"] == "V4_MESSAGE_MARKER"

    assert _rows(database, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='plugin_ingest_state'")
    assert _rows(database, "PRAGMA user_version")[0][0] == 4
