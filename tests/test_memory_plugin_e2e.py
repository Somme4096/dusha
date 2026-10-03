from __future__ import annotations

import json
import shutil
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from companion_gateway import api
from companion_gateway.config import AppConfig, DecisionConfig, EvergreenConfig, MemoryConfig, MemoryPluginConfig

GENERIC_PLUGIN_SOURCE = '''\
import sqlite3


def _connect(options):
    db = sqlite3.connect(options["database_path"], timeout=10)
    db.execute(
        "CREATE TABLE IF NOT EXISTS generic_received "
        "(message_id INTEGER PRIMARY KEY, text TEXT NOT NULL)"
    )
    return db


def ingest_messages(request, options):
    db = _connect(options)
    messages = getattr(request, "messages", None) or ()
    ids = []
    try:
        for message in messages:
            ident = int(message["id"])
            ids.append(ident)
            db.execute(
                "INSERT OR IGNORE INTO generic_received(message_id, text) VALUES (?, ?)",
                (ident, str(message.get("text", ""))),
            )
        db.commit()
    finally:
        db.close()
    return {"highest_id": max(ids) if ids else 0}


def inject_context(request, options):
    if options.get("context_fail"):
        raise RuntimeError("configured context failure")
    db = _connect(options)
    try:
        row = db.execute(
            "SELECT text FROM generic_received ORDER BY message_id DESC LIMIT 1"
        ).fetchone()
    finally:
        db.close()
    latest = str(row[0]) if row else ""
    return {
        "text": "GENERIC_PLUGIN_CONTEXT",
        "records": [{"source": "generic", "text": "GENERIC_PLUGIN_RECORD " + latest}],
    }


def match_phrase(request, options):
    message = str(getattr(request, "message", "")).casefold()
    for phrase in options.get("phrase_patterns", []):
        if str(phrase["pattern"]).casefold() in message:
            return {"deltas": phrase["deltas"]}
    return {"deltas": None}


def status(request, options):
    return {"status": {"source": "generic", "healthy": True}}


def ensure_message_chunks(request, options):
    return {"chunks": 0}


def rebuild_chunks(request, options):
    return {"status": {"source": "generic", "chunks": 0}}


def rebuild_index(request, options):
    return {"status": {"source": "generic", "rebuilt": True}}


def backfill_once(request, options):
    return {"status": {"source": "generic", "backfilled": True}}


def close(request, options):
    return None
'''


@pytest.fixture(scope="session")
def generic_plugin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    mods = tmp_path_factory.mktemp("generic-mods")
    suite = mods / "generic_memory"
    suite.mkdir()
    (suite / "README.md").write_text("generic e2e plugin\n", encoding="utf-8")
    (suite / "main.py").write_text(GENERIC_PLUGIN_SOURCE, encoding="utf-8")
    (suite / "pyproject.toml").write_text(
        '[project]\nname = "generic-memory"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n',
        encoding="utf-8",
    )
    subprocess.run(["uv", "--no-config", "lock", "--directory", str(suite)], check=True)
    return mods


@pytest.fixture(scope="session")
def decision_plugin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    mods = tmp_path_factory.mktemp("decision-mods")
    source = Path(__file__).parents[1] / "examples" / "mods" / "system_one"
    destination = mods / "state_decider"
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(".venv", "__pycache__", "*.pyc"))
    (destination / "main.py").write_text(
        "def decide(request, options):\n"
        "    if request.state['base']['elation'] < 0.21:\n"
        "        return {'emotion': 'intimacy'}\n"
        "    return {'emotion': 'dejection'}\n",
        encoding="utf-8",
    )
    return mods


@pytest.fixture
def configured_app(tmp_path: Path, generic_plugin: Path, decision_plugin: Path):
    def make(
        *,
        options: dict | None = None,
        data_dir: Path | None = None,
        decision_enabled: bool = False,
        context_fail: bool = False,
    ) -> AppConfig:
        merged = dict(options or {})
        merged["context_fail"] = context_fail
        return AppConfig(
            data_dir=data_dir or tmp_path / "data",
            memory=MemoryConfig(recent_messages=8, search_hits=8, context_messages=1, injection_max_chars=20_000),
            evergreen=EvergreenConfig(enabled=True, max_items=32, max_chars=4_000),
            decision=DecisionConfig(
                module="state_decider" if decision_enabled else "",
                mods_dir=str(decision_plugin),
            ),
            memory_plugin=MemoryPluginConfig(
                module="generic_memory",
                mods_dir=str(generic_plugin),
                ingest_backfill_interval_seconds=1,
                options=merged,
            ),
        )

    return make


@asynccontextmanager
async def _client(config: AppConfig):
    app = api.create_app(config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client


async def _ingest(client: httpx.AsyncClient, conversation: str, content: str, role: str = "user") -> dict:
    response = await client.post(
        "/state/v1/messages",
        json={"harness": "e2e", "conversation_id": conversation, "role": role, "content": content},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_ingestion_context_isolation_search_and_phrase_match(configured_app):
    options = {
        "phrase_patterns": [{"pattern": "I Love Lanterns", "deltas": {"elation": 0.3}}],
    }
    async with _client(configured_app(options=options)) as client:
        first = await _ingest(client, "one", "Conversation one remembers the amber lantern.")
        before_no_match = (await client.get("/state/v1/affect")).json()["base"]
        await _ingest(client, "two", "Conversation two remembers the violet telescope.")
        after_no_match = (await client.get("/state/v1/affect")).json()["base"]
        assert after_no_match == pytest.approx(before_no_match)
        phrase = await _ingest(client, "one", "I LOVE LANTERNS")

        context = await client.post(
            "/state/v1/context",
            json={"harness": "e2e", "conversation_id": "one", "query": ""},
        )
        assert context.status_code == 200
        context_text = [record["text"] for record in context.json()["records"]]
        assert "Conversation one remembers the amber lantern." in context_text
        assert "Conversation two remembers the violet telescope." not in context_text

        search = await client.post("/state/v1/memory/search", json={"query": "AMBER LANTERN"})
        assert search.status_code == 200
        search_text = [message["text"] for hit in search.json()["results"] for message in hit["messages"]]
        assert "Conversation one remembers the amber lantern." in search_text

        no_match = await client.post("/state/v1/memory/search", json={"query": "unrecorded silver comet"})
        assert no_match.status_code == 200
        assert no_match.json()["results"] == []

        assert phrase["affect"] is None
        affect = await client.get("/state/v1/affect")
        assert affect.status_code == 200
        assert affect.json()["base"]["elation"] > after_no_match["elation"]

        fetched = await client.get(f"/state/v1/messages/{first['id']}")
        assert fetched.status_code == 200
        assert fetched.json()["text"] == "Conversation one remembers the amber lantern."


async def test_evergreen_lifecycle_errors_and_phrase_only_update(configured_app):
    options = {"phrase_patterns": [{"pattern": "bright harbor", "deltas": {"intimacy": 0.2}}]}
    async with _client(configured_app(options=options)) as client:
        message = await _ingest(client, "facts", "My home is the bright harbor.")
        before_second_match = (await client.get("/state/v1/affect")).json()["base"]
        created = await client.post(
            "/state/v1/evergreen/facts",
            json={"key": "user.home", "text": "The user's home is the bright harbor.", "source_message_id": message["id"]},
        )
        assert created.status_code == 200
        fact = created.json()["fact"]
        fact_id = fact["fact_id"]

        invalid = await client.post(
            "/state/v1/evergreen/facts", json={"key": "INVALID KEY", "text": "bad"}
        )
        assert invalid.status_code == 422
        duplicate = await client.post(
            "/state/v1/evergreen/facts", json={"key": "user.home", "text": "another home"}
        )
        assert duplicate.status_code == 409

        missing = await client.get("/state/v1/evergreen/facts/missing/history")
        assert missing.status_code == 404
        wrong_revision = await client.post(
            f"/state/v1/evergreen/facts/{fact_id}/revisions",
            json={"expected_revision": 9, "text": "wrong"},
        )
        assert wrong_revision.status_code == 409
        revised = await client.post(
            f"/state/v1/evergreen/facts/{fact_id}/revisions",
            json={"expected_revision": 1, "text": "The user's home is usually the bright harbor."},
        )
        assert revised.status_code == 200
        assert revised.json()["fact"]["revision"] == 2
        history = await client.get(f"/state/v1/evergreen/facts/{fact_id}/history")
        assert history.status_code == 200
        assert len(history.json()["revisions"]) == 2

        forgotten = await client.post(
            f"/state/v1/evergreen/facts/{fact_id}/forget",
            json={"expected_revision": 2, "reason": "No longer current."},
        )
        assert forgotten.status_code == 200
        assert forgotten.json()["fact"]["effective_state"] == "forgotten"
        missing_forget = await client.post(
            "/state/v1/evergreen/facts/missing/forget", json={"expected_revision": 1, "reason": "missing"}
        )
        assert missing_forget.status_code == 404

        phrase = await _ingest(client, "facts", "A BRIGHT HARBOR is comforting.")
        assert phrase["affect"] is None
        affect = await client.get("/state/v1/affect")
        assert affect.json()["base"]["intimacy"] > before_second_match["intimacy"]


async def test_restart_preserves_builtin_messages_and_evergreen(configured_app):
    data_dir = configured_app().data_dir
    async with _client(configured_app(data_dir=data_dir)) as client:
        message = await _ingest(client, "persistent", "Persist this memory across restart.")
        created = await client.post(
            "/state/v1/evergreen/facts",
            json={"key": "user.persistence", "text": "Persistence is enabled."},
        )
        assert created.status_code == 200
        fact_id = created.json()["fact"]["fact_id"]

    async with _client(configured_app(data_dir=data_dir)) as client:
        fetched = await client.get(f"/state/v1/messages/{message['id']}")
        assert fetched.status_code == 200
        assert fetched.json()["text"] == "Persist this memory across restart."
        search = await client.post("/state/v1/memory/search", json={"query": "persist restart"})
        assert search.status_code == 200
        assert any(
            item["text"] == "Persist this memory across restart."
            for hit in search.json()["results"]
            for item in hit["messages"]
        )
        facts = await client.get("/state/v1/evergreen/facts")
        assert any(fact["fact_id"] == fact_id for fact in facts.json()["facts"])


async def test_malformed_phrase_pattern_does_not_break_ingest_or_restart(configured_app):
    data_dir = configured_app().data_dir
    options = {"phrase_patterns": [{"deltas": {"elation": 0.3}}]}
    async with _client(configured_app(options=options, data_dir=data_dir)) as client:
        message = await _ingest(client, "malformed", "Keep this message despite the matcher failure.")
        affect = await client.get("/state/v1/affect")
        assert affect.status_code == 200
        assert set(affect.json()) >= {"base", "mood"}

    async with _client(configured_app(options=options, data_dir=data_dir)) as client:
        fetched = await client.get(f"/state/v1/messages/{message['id']}")
        assert fetched.status_code == 200
        assert fetched.json()["text"] == "Keep this message despite the matcher failure."


async def test_decision_plugin_sees_state_before_phrase_delta(configured_app):
    options = {
        "phrase_patterns": [{"pattern": "pre phrase", "deltas": {"elation": 0.3}}],
    }
    async with _client(configured_app(options=options, decision_enabled=True)) as client:
        message = await _ingest(client, "ordering", "The pre phrase is present.")
        assert message["affect"] is not None
        affect = message["affect"]["state"]
        assert affect["base"]["intimacy"] > 0.2
        assert affect["base"]["dejection"] == pytest.approx(0.15)
        assert affect["base"]["elation"] > 0.2


async def test_plugin_context_is_source_labeled_across_restart_and_falls_back(configured_app):
    data_dir = configured_app().data_dir
    async with _client(configured_app(data_dir=data_dir)) as client:
        message = await _ingest(client, "transition", "Core lexical retrieval keeps the transition marker.")
        search = await client.post("/state/v1/memory/search", json={"query": "transition marker"})
        assert search.status_code == 200, search.text
        assert any(
            "transition marker" in item["text"]
            for hit in search.json()["results"]
            for item in hit["messages"]
        )
        context = await client.post(
            "/state/v1/context",
            json={"harness": "e2e", "conversation_id": "transition", "query": "transition"},
        )
        assert context.status_code == 200, context.text
        plugin_context = context.json()["context"]["memory"].get("plugin_context")
        assert plugin_context is not None, context.text
        assert plugin_context["source"]
        blob = json.dumps(plugin_context)
        assert "GENERIC_PLUGIN_RECORD" in blob, blob
        assert "Core lexical retrieval keeps the transition marker." in blob, blob
        assert "memory_id" not in blob, blob

    async with _client(configured_app(data_dir=data_dir)) as client:
        fetched = await client.get(f"/state/v1/messages/{message['id']}")
        assert fetched.status_code == 200
        search = await client.post("/state/v1/memory/search", json={"query": "transition marker"})
        assert search.status_code == 200, search.text
        assert any(
            "transition marker" in item["text"]
            for hit in search.json()["results"]
            for item in hit["messages"]
        )
        context = await client.post(
            "/state/v1/context",
            json={"harness": "e2e", "conversation_id": "transition", "query": "transition"},
        )
        assert context.status_code == 200, context.text
        plugin_context = context.json()["context"]["memory"].get("plugin_context")
        assert plugin_context is not None, context.text
        assert "GENERIC_PLUGIN_RECORD" in json.dumps(plugin_context)

    async with _client(configured_app(data_dir=data_dir, context_fail=True)) as client:
        context = await client.post(
            "/state/v1/context",
            json={"harness": "e2e", "conversation_id": "transition", "query": "transition"},
        )
        assert context.status_code == 200, context.text
        body = context.json()
        assert "Core lexical retrieval keeps the transition marker." in body["injection"]
        assert any(
            "Core lexical retrieval keeps the transition marker." in item["text"]
            for item in body["records"]
        )
        assert body["context"]["memory"].get("plugin_context") is None
