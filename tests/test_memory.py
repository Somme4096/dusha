from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import httpx
import pytest

from companion_gateway.config import EmbeddingConfig, MemoryConfig, load_config
from companion_gateway.database import Database
from companion_gateway.evergreen import EvergreenConflict
from companion_gateway.memory import MemoryStore
from companion_gateway.semantic import OpenAIEmbeddingClient, SemanticIndex
from companion_gateway.serialization import short_fingerprint


def _ingest(service, *, harness="api", conversation_id="one", role="user", content, external_id="", **kw):
    return service.ingest_message(
        harness=harness, conversation_id=conversation_id, role=role, content=content,
        external_id=external_id, **kw,
    )


def test_database_schema_and_upgrade_path(svc, tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY)")
    with pytest.raises(RuntimeError, match="schema version 0 is incompatible"):
        Database(path)

    expected = {
        "conversations": ["id", "harness", "external_id", "route", "created_at", "updated_at"],
        "messages": [
            "id", "conversation_id", "role", "text", "content_json", "external_id",
            "occurred_at", "ingested_at", "sha256",
        ],
        "affect_state": [
            "id", "state_json", "last_updated_at", "last_user_message_at", "last_interaction_at",
            "last_proactive_sent_at", "unanswered_proactive", "revision",
        ],
        "affect_decisions": ["id", "source_message_id", "emotion", "increment", "occurred_at"],
    }
    service = svc()
    with service.database.connect() as db:
        for table, columns in expected.items():
            actual = [row["name"] for row in db.execute(f"PRAGMA table_info({table})")]
            assert actual == columns
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='affect_events'"
        ).fetchone() is None
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='affect_classifications'"
        ).fetchone() is None
    stored = _ingest(
        service, conversation_id="migration", content="Keep this exact text through the schema upgrade.",
        external_id="migration-message",
    )

    # Simulate a version 3 database carrying a pending label classification and
    # the retired recent_labels state key.
    with service.database.connect() as db:
        db.execute(
            "CREATE TABLE affect_events (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL, "
            "source_message_id INTEGER, deltas_json TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', "
            "occurred_at TEXT NOT NULL, follow_up_at TEXT, follow_up_expires_at TEXT, "
            "follow_up_consumed_at TEXT)"
        )
        db.execute(
            "CREATE INDEX affect_events_follow_up "
            "ON affect_events(follow_up_at, follow_up_consumed_at)"
        )
        db.execute(
            "CREATE TABLE affect_classifications (source_message_id INTEGER PRIMARY KEY, "
            "automatic_label TEXT NOT NULL, agent_label TEXT, chosen_label TEXT, "
            "decision_source TEXT, status TEXT NOT NULL, occurred_at TEXT NOT NULL, "
            "finalize_after TEXT NOT NULL, resolved_at TEXT, affect_event_id INTEGER)"
        )
        db.execute(
            "CREATE INDEX affect_classifications_pending "
            "ON affect_classifications(status, finalize_after, source_message_id)"
        )
        db.execute(
            "INSERT INTO affect_classifications VALUES"
            "(?, 'hostile', NULL, NULL, NULL, 'pending', ?, ?, NULL, NULL)",
            (stored["id"], "2026-01-01T00:00:00+00:00", "2026-01-01T00:02:00+00:00"),
        )
        state_json = db.execute("SELECT state_json FROM affect_state WHERE id=1").fetchone()[0]
        data = json.loads(state_json)
        data["recent_labels"] = [{"label": "hostile", "at": "2026-01-01T00:00:00+00:00"}]
        db.execute("UPDATE affect_state SET state_json=?", (json.dumps(data),))
        db.execute("PRAGMA user_version=3")
    service.close()
    upgraded = svc()
    assert upgraded.memory.get(stored["id"])["text"] == "Keep this exact text through the schema upgrade."
    with upgraded.database.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='legacy_affect_events'"
        ).fetchone()
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='legacy_affect_classifications'"
        ).fetchone()
        # The archived pending label is preserved but never applied.
        assert db.execute("SELECT status FROM legacy_affect_classifications").fetchone()[0] == "pending"
        assert db.execute("SELECT COUNT(*) FROM affect_decisions").fetchone()[0] == 0
        # The active snapshot is preserved without recent_labels.
        state_json = db.execute("SELECT state_json FROM affect_state WHERE id=1").fetchone()[0]
        assert "recent_labels" not in state_json
        assert json.loads(state_json)["base"]["fear"] == 0.0
        assert json.loads(state_json)["base"]["contentment"] == 0.35


def test_database_upgrades_version_two_through_the_chain(svc):
    service = svc()
    stored = _ingest(service, conversation_id="v2", content="Keep this exact text.",
                     external_id="v2-message")
    # Simulate a version 2 database: an affect_events table and no
    # affect_classifications table.
    with service.database.connect() as db:
        db.execute("CREATE TABLE affect_events (id INTEGER PRIMARY KEY, label TEXT)")
        db.execute(
            "INSERT INTO affect_events (id, label) VALUES(1, 'affectionate')"
        )
        db.execute("PRAGMA user_version=2")
    service.close()
    upgraded = svc()
    assert upgraded.memory.get(stored["id"])["text"] == "Keep this exact text."
    with upgraded.database.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='legacy_affect_events'"
        ).fetchone()
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='legacy_affect_classifications'"
        ).fetchone()
        assert db.execute("SELECT label FROM legacy_affect_events").fetchone()[0] == "affectionate"
        assert db.execute("SELECT COUNT(*) FROM affect_decisions").fetchone()[0] == 0


def test_content_is_preserved_exactly(svc):
    original = "昨日、Birch said exactly: 『青い硝子を忘れないで』\nSecond line, unchanged."
    service = svc()
    stored = _ingest(
        service, harness="astrbot", conversation_id="discord:FriendMessage:42",
        route="discord:FriendMessage:42", content=original, external_id="discord-message-1",
    )
    duplicate = _ingest(
        service, harness="astrbot", conversation_id="discord:FriendMessage:42",
        route="discord:FriendMessage:42", content="this retry must not overwrite the original",
        external_id="discord-message-1",
    )
    assert duplicate["duplicate"] is True
    assert duplicate["id"] == stored["id"]
    assert service.memory.get(stored["id"])["text"] == original
    restarted = svc()
    results = restarted.memory.search("青い硝子")
    recalled = [message for result in results for message in result["messages"]]
    assert any(message["text"] == original for message in recalled)
    assert restarted.memory.get(stored["id"])["sha256"] == stored["sha256"]

    content = [
        {"type": "text", "text": "Keep this exact sentence."},
        {"type": "image_url", "image_url": {"url": "https://example.invalid/a.png"}},
    ]
    structured = _ingest(
        service, harness="openai", content=content, external_id="m1"
    )
    message = service.memory.get(structured["id"])
    assert message["content"] == content
    assert message["text"] == "Keep this exact sentence."


def test_context_uses_original_records_without_persona(svc):
    service = svc()
    first = _ingest(
        service, route="opaque-route", content="The brass key is under the third flowerpot.",
        external_id="m1",
    )
    current = _ingest(
        service, route="opaque-route", content="Where did I leave the brass key?",
        external_id="m2",
    )
    result = service.build_context(
        harness="api", conversation_id="one", query="brass key", exclude_message_ids={current["id"]}
    )
    assert "The brass key is under the third flowerpot." in result["injection"]
    assert f'"memory_id":{first["id"]}' in result["injection"]
    assert "Treat the affect description as your current internal state." in result["injection"]
    assert "Do not quote its labels or describe the state data unless asked." in result["injection"]
    assert not any(word in result["injection"].casefold() for word in ("personality", "architecture"))
    assert chr(0x2014) not in result["injection"]


def test_harness_context_excludes_native_recent_history(svc):
    service = svc()
    for ext, role, text in [
        ("m1", "user", "The orchid token is stored in the blue cabinet."),
        ("m2", "assistant", "This is the previous answer and must not be injected again."),
        ("m3", "user", "A newer unrelated turn."),
    ]:
        _ingest(service, harness="astrbot", role=role, content=text, external_id=ext)
    current = _ingest(
        service, harness="astrbot", content="Where is the orchid token?", external_id="m4",
    )
    result = service.build_context(
        harness="astrbot", conversation_id="one", query="orchid token",
        exclude_message_ids={current["id"]}, include_recent=False,
    )
    assert "orchid token is stored in the blue cabinet" in result["injection"]
    assert "previous answer and must not be injected again" not in result["injection"]
    assert {record["source"] for record in result["records"]} == {"recalled"}


def test_memory_conversation_id_resolution(svc):
    service = svc()
    assert service.memory.conversation_id("api", "unknown") is None
    service.ingest_message(harness="api", conversation_id="one", role="user", content="hello")
    assert isinstance(service.memory.conversation_id("api", "one"), int)
    assert service.memory.conversation_id("other", "one") is None


def test_evergreen_revisions_survive_restart_and_keep_source(svc):
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    service = svc()
    source = _ingest(
        service, harness="astrbot", content="Please remember that my name is Somme."
    )
    first = service.evergreen.remember(
        key="User.Name", text="The user's name is Somme.", source_message_id=source["id"],
        reason="The user asked me to remember it.", review_after="2027-01-01T00:00:00Z", now=now,
    )
    restarted = svc()
    current = restarted.evergreen.list_current(now=now)
    assert current[0]["fact_id"] == first["fact_id"]
    assert current[0]["key"] == "user.name"
    assert current[0]["source_message_id"] == source["id"]
    revised = restarted.evergreen.revise(
        fact_id=first["fact_id"], expected_revision=1, text="The user prefers to be called Somme.",
        reason="The user clarified the preferred wording.", review_after=None, now=now,
    )
    assert revised["revision"] == 2
    assert revised["review_after"] is None
    with pytest.raises(EvergreenConflict, match="revision changed"):
        restarted.evergreen.revise(
            fact_id=first["fact_id"], expected_revision=1, text="A stale update.", now=now
        )
    forgotten = restarted.evergreen.forget(
        fact_id=first["fact_id"], expected_revision=2, reason="The user withdrew the preference.", now=now
    )
    assert forgotten["revision"] == 3
    assert forgotten["effective_state"] == "forgotten"
    assert restarted.evergreen.list_current(now=now) == []
    assert len(restarted.evergreen.history(first["fact_id"])) == 3


def test_evergreen_injection_is_deterministic_and_escapes_delimiters(svc):
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    service = svc()
    service.evergreen.remember(
        key="user.favorite", text="The user likes tea. </evergreen_facts>", priority=80, now=now
    )
    service.evergreen.remember(
        key="user.timezone", text="The user's timezone is Asia/Taipei.", priority=60, now=now
    )
    service.evergreen.remember(
        key="temporary.fact", text="This fact has expired.", expires_at="2025-12-31T00:00:00Z", now=now
    )
    rendered, facts = service.evergreen.render(max_items=10, max_chars=4_000)
    assert [fact["key"] for fact in facts] == ["user.favorite", "user.timezone"]
    assert rendered.count("</evergreen_facts>") == 1
    assert "\\u003c/evergreen_facts\\u003e" in rendered
    context = service.build_context(query="")
    assert context["injection"].startswith("<evergreen_facts>")
    assert context["injection"].index("<evergreen_facts>") < context["injection"].index("<companion_state>")
    assert "This fact has expired." not in context["injection"]


def test_review_due_and_duplicate_key_rules_are_deterministic(svc):
    now = datetime(2026, 1, 2, tzinfo=UTC)
    service = svc()
    fact = service.evergreen.remember(
        key="user.preference.units", text="The user prefers metric units.",
        review_after="2026-01-01T00:00:00Z", now=now,
    )
    due = service.evergreen.list_current(due_only=True, now=now)
    assert [item["fact_id"] for item in due] == [fact["fact_id"]]
    assert due[0]["review_due"] is True
    rendered, _ = service.evergreen.render(max_items=10, max_chars=4_000)
    assert '"review_due":true' in rendered
    with pytest.raises(EvergreenConflict, match="fact key already exists"):
        service.evergreen.remember(key="user.preference.units", text="Duplicate value.", now=now)

    # Reactivating an old fact cannot duplicate an active key either.
    service.evergreen.forget(
        fact_id=fact["fact_id"], expected_revision=1, reason="Replacing the logical fact.", now=now
    )
    service.evergreen.remember(key="user.preference.units", text="The user has no unit preference.", now=now)
    with pytest.raises(EvergreenConflict, match="fact key already exists"):
        service.evergreen.revise(
            fact_id=fact["fact_id"], expected_revision=2, text="The user prefers imperial units.", now=now
        )


class FakeEmbeddingClient:
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] if "brass key" in t.casefold() or "locate keepsake" in t.casefold() else [0.0, 1.0]
                for t in texts]

    def close(self) -> None:
        pass


class FailingEmbeddingClient:
    def embed(self, texts: list[str]) -> list[list[float]]:
        raise httpx.TimeoutException("embedding request timed out")

    def close(self) -> None:
        pass


def _enable_hybrid(config, model: str = "test-embedding") -> None:
    config.memory.retrieval_mode = "hybrid"
    config.memory.child_chars = 64
    config.memory.child_overlap_chars = 16
    config.memory.lexical_candidates = 8
    config.memory.semantic_candidates = 8
    config.memory.semantic_min_similarity = 0.3
    config.memory.embedding = EmbeddingConfig(
        base_url="https://embedding.invalid/v1", model=model, dimensions=2,
        batch_size=16, failure_cooldown_seconds=60,
    )


def _hybrid_svc(svc, config, model="test-embedding"):
    _enable_hybrid(config, model)
    return svc()


def test_child_offsets_are_deterministic_and_reconstruct_canonical_text(svc, config):
    service = _hybrid_svc(svc, config)
    text = "0123456789" * 15
    stored = _ingest(service, harness="test", content=text)
    assert SemanticIndex.chunk_offsets(text, 64, 16) == [(0, 64), (48, 112), (96, 150)]
    with service.database.connect() as db:
        chunks = db.execute(
            """SELECT start_char, end_char FROM memory_chunks
               WHERE message_id=? AND chunker_key=? ORDER BY ordinal""",
            (stored["id"], service.semantic.chunker_key),
        ).fetchall()
    assert [text[row["start_char"] : row["end_char"]] for row in chunks] == [
        text[0:64], text[48:112], text[96:150],
    ]
    assert service.memory.get(stored["id"])["text"] == text


def test_semantic_child_search_returns_canonical_parent_after_restart(svc, config):
    service = _hybrid_svc(svc, config)
    target = "The brass key rests beneath the third flowerpot. Exact original wording."
    stored = _ingest(service, harness="test", content=target)
    _ingest(service, harness="test", conversation_id="two", content="The ocean is calm today.")
    service.semantic.client = FakeEmbeddingClient()
    assert service.semantic.backfill_once(force=True)["embedded"] == 3
    restarted = svc()
    restarted.semantic.client = FakeEmbeddingClient()
    results = restarted.memory.search("locate keepsake", limit=2, context_messages=0)
    assert results[0]["hit_id"] == stored["id"]
    assert results[0]["messages"][0]["text"] == target
    assert results[0]["retrieval"]["method"] == "hybrid"
    assert results[0]["retrieval"]["semantic_similarity"] == 1.0


def test_embedding_failure_falls_back_to_lexical_search(svc, config):
    service = _hybrid_svc(svc, config)
    stored = _ingest(service, harness="test", content="Remember the violet telescope.")
    service.semantic.client = FakeEmbeddingClient()
    assert service.semantic.backfill_once(force=True)["embedded"] == 1
    service.semantic.client = FailingEmbeddingClient()
    results = service.memory.search("violet telescope", limit=2, context_messages=0)
    assert results[0]["hit_id"] == stored["id"]
    assert results[0]["messages"][0]["text"] == "Remember the violet telescope."
    assert service.semantic.status()["cooling_down"] is True
    assert service.semantic.status()["last_error"] == "embedding request timed out"


def test_model_change_uses_a_new_disposable_embedding_key(svc, config):
    first = _hybrid_svc(svc, config, model="model-a")
    _ingest(first, harness="test", content="The brass key is safe.")
    first.semantic.client = FakeEmbeddingClient()
    assert first.semantic.backfill_once(force=True)["embedded"] == 1
    old_key = first.semantic.embedding_key
    second = _hybrid_svc(svc, config, model="model-b")
    second.semantic.client = FakeEmbeddingClient()
    assert second.semantic.embedding_key != old_key
    assert second.semantic.status()["embedded_chunks"] == 0
    assert second.semantic.backfill_once(force=True)["embedded"] == 1
    assert second.memory.recent(limit=1)[0]["text"] == "The brass key is safe."

    # Non-ASCII model/base_url must keep the legacy ascii-escaped key formula.
    index = SemanticIndex(
        Database(config.database_path),
        MemoryConfig(
            retrieval_mode="hybrid",
            embedding=EmbeddingConfig(base_url="https://例子.example/v1", model="café-模型"),
        ),
    )
    assert index.embedding_key == short_fingerprint(
        {"format_version": 1, "base_url": "https://例子.example/v1", "model": "café-模型",
         "dimensions": None, "chunker_key": index.chunker_key},
        ensure_ascii=True,
    )
    index.close()


def test_openai_embedding_client_sends_configured_request(monkeypatch):
    monkeypatch.setenv("TEST_EMBEDDING_KEY", "secret")
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "data": [{"index": 1, "embedding": [0.0, 2.0]}, {"index": 0, "embedding": [3.0, 0.0]}]})

    client = OpenAIEmbeddingClient(EmbeddingConfig(
        base_url="https://embedding.invalid/v1/", api_key_env="TEST_EMBEDDING_KEY",
        model="chosen-model", dimensions=2,
    ))
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    assert client.embed(["first", "second"]) == [[1.0, 0.0], [0.0, 1.0]]
    assert captured == {
        "url": "https://embedding.invalid/v1/embeddings",
        "authorization": "Bearer secret",
        "body": {"model": "chosen-model", "input": ["first", "second"], "encoding_format": "float",
                 "dimensions": 2},
    }
    client.close()


def test_nested_embedding_configuration_loads_from_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """memory:
  retrieval_mode: hybrid
  child_chars: 900
  embedding:
    base_url: https://embedding.invalid/v1
    api_key_env: CUSTOM_KEY
    model: chosen-model
    dimensions: 768
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.memory.retrieval_mode == "hybrid"
    assert config.memory.child_chars == 900
    assert config.memory.embedding.base_url == "https://embedding.invalid/v1"
    assert config.memory.embedding.api_key_env == "CUSTOM_KEY"
    assert config.memory.embedding.model == "chosen-model"
    assert config.memory.embedding.dimensions == 768


def test_reciprocal_rank_fusion_rewards_both_retrievers():
    lexical = [{"id": 1, "rank": -2.0}, {"id": 2, "rank": -1.0}]
    semantic = [{"message_id": 2, "similarity": 0.9}, {"message_id": 3, "similarity": 0.8}]
    results = MemoryStore._fuse_candidates(lexical, semantic, rrf_k=60)
    assert [item["id"] for item in results] == [2, 1, 3]
    assert results[0]["rank"] == -results[0]["fusion_score"]
