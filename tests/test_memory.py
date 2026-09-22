from __future__ import annotations

import sqlite3

import pytest

from companion_gateway.database import Database
from companion_gateway.service import CompanionService


def test_incompatible_database_is_rejected(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY)")

    with pytest.raises(RuntimeError, match="schema version 0 is incompatible"):
        Database(path)


def test_schema_has_one_state_scope(config):
    service = CompanionService(config)
    expected = {
        "conversations": ["id", "harness", "external_id", "route", "created_at", "updated_at"],
        "messages": [
            "id",
            "conversation_id",
            "role",
            "text",
            "content_json",
            "external_id",
            "occurred_at",
            "ingested_at",
            "sha256",
        ],
        "affect_state": [
            "id",
            "state_json",
            "last_updated_at",
            "last_user_message_at",
            "last_interaction_at",
            "last_proactive_sent_at",
            "unanswered_proactive",
            "revision",
        ],
        "affect_classifications": [
            "source_message_id",
            "automatic_label",
            "agent_label",
            "chosen_label",
            "decision_source",
            "status",
            "occurred_at",
            "finalize_after",
            "resolved_at",
            "affect_event_id",
        ],
    }
    with service.database.connect() as db:
        for table, columns in expected.items():
            actual = [row["name"] for row in db.execute(f"PRAGMA table_info({table})")]
            assert actual == columns
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3


def test_schema_two_upgrade_preserves_canonical_messages(config):
    service = CompanionService(config)
    stored = service.ingest_message(
        harness="api",
        conversation_id="migration",
        role="user",
        content="Keep this exact text through the schema upgrade.",
        external_id="migration-message",
        affect_label="neutral",
    )
    with service.database.connect() as db:
        db.execute("DROP TABLE affect_classifications")
        db.execute("PRAGMA user_version=2")
    service.close()

    upgraded = CompanionService(config)

    assert upgraded.memory.get(stored["id"])["text"] == "Keep this exact text through the schema upgrade."
    with upgraded.database.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='affect_classifications'"
        ).fetchone()


def test_raw_text_is_canonical_search_result_and_survives_restart(config):
    service = CompanionService(config)
    original = "昨日、Birch said exactly: 『青い硝子を忘れないで』\nSecond line, unchanged."
    stored = service.ingest_message(
        harness="astrbot",
        conversation_id="discord:FriendMessage:42",
        route="discord:FriendMessage:42",
        role="user",
        content=original,
        external_id="discord-message-1",
        affect_label="neutral",
    )
    duplicate = service.ingest_message(
        harness="astrbot",
        conversation_id="discord:FriendMessage:42",
        route="discord:FriendMessage:42",
        role="user",
        content="this retry must not overwrite the original",
        external_id="discord-message-1",
    )

    assert duplicate["duplicate"] is True
    assert duplicate["id"] == stored["id"]
    assert service.memory.get(stored["id"])["text"] == original

    restarted = CompanionService(config)
    results = restarted.memory.search("青い硝子")
    recalled = [message for result in results for message in result["messages"]]
    assert any(message["text"] == original for message in recalled)
    assert restarted.memory.get(stored["id"])["sha256"] == stored["sha256"]


def test_structured_openai_content_is_preserved(config):
    service = CompanionService(config)
    content = [
        {"type": "text", "text": "Keep this exact sentence."},
        {"type": "image_url", "image_url": {"url": "https://example.invalid/a.png"}},
    ]
    stored = service.ingest_message(
        harness="openai",
        conversation_id="one",
        role="user",
        content=content,
        external_id="m1",
        affect_label="neutral",
    )
    message = service.memory.get(stored["id"])
    assert message["content"] == content
    assert message["text"] == "Keep this exact sentence."


def test_context_uses_original_records_without_persona(config):
    service = CompanionService(config)
    first = service.ingest_message(
        harness="api",
        conversation_id="one",
        route="opaque-route",
        role="user",
        content="The brass key is under the third flowerpot.",
        external_id="m1",
        affect_label="neutral",
    )
    current = service.ingest_message(
        harness="api",
        conversation_id="one",
        route="opaque-route",
        role="user",
        content="Where did I leave the brass key?",
        external_id="m2",
        affect_label="neutral",
    )
    result = service.build_context(
        harness="api",
        conversation_id="one",
        query="brass key",
        exclude_message_ids={current["id"]},
    )
    assert "The brass key is under the third flowerpot." in result["injection"]
    assert f'"memory_id":{first["id"]}' in result["injection"]
    assert "Treat the affect description as your current internal state." in result["injection"]
    assert "Do not quote its labels or describe the state data unless asked." in result["injection"]
    assert "personality" not in result["injection"].casefold()
    assert "architecture" not in result["injection"].casefold()
    assert chr(0x2014) not in result["injection"]


def test_harness_context_excludes_native_recent_history(config):
    service = CompanionService(config)
    service.ingest_message(
        harness="astrbot",
        conversation_id="one",
        role="user",
        content="The orchid token is stored in the blue cabinet.",
        external_id="m1",
        affect_label="neutral",
    )
    service.ingest_message(
        harness="astrbot",
        conversation_id="one",
        role="assistant",
        content="This is the previous answer and must not be injected again.",
        external_id="m2",
    )
    service.ingest_message(
        harness="astrbot",
        conversation_id="one",
        role="user",
        content="A newer unrelated turn.",
        external_id="m3",
        affect_label="neutral",
    )
    current = service.ingest_message(
        harness="astrbot",
        conversation_id="one",
        role="user",
        content="Where is the orchid token?",
        external_id="m4",
        affect_label="neutral",
    )

    result = service.build_context(
        harness="astrbot",
        conversation_id="one",
        query="orchid token",
        exclude_message_ids={current["id"]},
        include_recent=False,
    )

    assert "orchid token is stored in the blue cabinet" in result["injection"]
    assert "previous answer and must not be injected again" not in result["injection"]
    assert {record["source"] for record in result["records"]} == {"recalled"}
