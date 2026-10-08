from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from dusha.database import Database
from dusha.evergreen import EvergreenConflict


def _ingest(service, *, harness="api", conversation_id="one", role="user", content, external_id="", **kw):
    return service.ingest_message(
        harness=harness, conversation_id=conversation_id, role=role, content=content,
        external_id=external_id, **kw,
    )


def test_database_schema_and_version_check(svc, tmp_path):
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
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='affect_events'"
        ).fetchone() is None
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='affect_classifications'"
        ).fetchone() is None
    service.close()
    for version in (4, 6):
        with sqlite3.connect(service.database.path) as db:
            db.execute(f"PRAGMA user_version={version}")
        with pytest.raises(RuntimeError, match=f"schema version {version} is incompatible"):
            svc()


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
    assert service._memory_fallback.get(stored["id"])["text"] == original
    restarted = svc()
    results = restarted._memory_fallback.search("青い硝子")
    recalled = [message for result in results for message in result["messages"]]
    assert any(message["text"] == original for message in recalled)
    assert restarted._memory_fallback.get(stored["id"])["sha256"] == stored["sha256"]

    content = [
        {"type": "text", "text": "Keep this exact sentence."},
        {"type": "image_url", "image_url": {"url": "https://example.invalid/a.png"}},
    ]
    structured = _ingest(
        service, harness="openai", content=content, external_id="m1"
    )
    message = service._memory_fallback.get(structured["id"])
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
    assert "Read the Affect line as your present feeling." in result["injection"]
    assert "overrules your default warmth for this reply." in result["injection"]
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
    assert service._memory_fallback.conversation_id("api", "unknown") is None
    service.ingest_message(harness="api", conversation_id="one", role="user", content="hello")
    assert isinstance(service._memory_fallback.conversation_id("api", "one"), int)
    assert service._memory_fallback.conversation_id("other", "one") is None


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

    service.evergreen.forget(
        fact_id=fact["fact_id"], expected_revision=1, reason="Replacing the logical fact.", now=now
    )
    service.evergreen.remember(key="user.preference.units", text="The user has no unit preference.", now=now)
    with pytest.raises(EvergreenConflict, match="fact key already exists"):
        service.evergreen.revise(
            fact_id=fact["fact_id"], expected_revision=2, text="The user prefers imperial units.", now=now
        )


def test_memo_add_list_done_and_reason_required(svc):
    service = svc()
    first = service.memo_add("Draft the yumecho phase one report.")
    second = service.memo_add("Buy more green tea.")
    assert first["status"] == "active"
    assert first["archived_at"] is None
    assert [note["id"] for note in service.memo_list()] == [first["id"], second["id"]]
    assert [note["id"] for note in service.memo_list(limit=1)] == [first["id"]]

    with pytest.raises(ValueError, match="reason is required"):
        service.memo_done(first["id"], "   ")

    done = service.memo_done(first["id"], "Report published.")
    assert done["status"] == "archived"
    assert done["reason"] == "Report published."
    assert done["archived_at"] is not None
    assert [note["id"] for note in service.memo_list()] == [second["id"]]
    assert [note["id"] for note in service.memo_list(status="archived")] == [first["id"]]

    with pytest.raises(ValueError, match="status must be active or archived"):
        service.memo_list(status="bogus")
    with pytest.raises(KeyError):
        service.memo_done(999999, "Missing note.")
    with pytest.raises(ValueError, match="memo text is required"):
        service.memo_add("   ")


def test_active_memos_are_injected_and_archived_memos_are_not(svc):
    service = svc()
    active = service.memo_add("Draft the yumecho phase one report.")
    archived = service.memo_add("Obsolete memo.")
    service.memo_done(archived["id"], "No longer needed.")

    result = service.build_context(query="")
    assert "<memo_notes>" in result["injection"]
    assert "Draft the yumecho phase one report." in result["injection"]
    assert "Obsolete memo." not in result["injection"]
    assert result["context"]["memory"]["memo_notes"] == [
        {"id": active["id"], "text": "Draft the yumecho phase one report.",
         "created_at": active["created_at"]}
    ]
