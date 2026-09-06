from __future__ import annotations

from datetime import UTC, datetime

import pytest

from companion_gateway.evergreen import EvergreenConflict
from companion_gateway.service import CompanionService


def test_evergreen_revisions_survive_restart_and_keep_source(config):
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    service = CompanionService(config)
    source = service.ingest_message(
        companion_id="sophia",
        harness="astrbot",
        conversation_id="one",
        role="user",
        content="Please remember that my name is Somme.",
        affect_label="neutral",
    )
    first = service.evergreen.remember(
        companion_id="sophia",
        key="User.Name",
        text="The user's name is Somme.",
        source_message_id=source["id"],
        reason="The user asked me to remember it.",
        review_after="2027-01-01T00:00:00Z",
        now=now,
    )

    restarted = CompanionService(config)
    current = restarted.evergreen.list_current("sophia", now=now)
    assert current[0]["fact_id"] == first["fact_id"]
    assert current[0]["key"] == "user.name"
    assert current[0]["source_message_id"] == source["id"]

    revised = restarted.evergreen.revise(
        companion_id="sophia",
        fact_id=first["fact_id"],
        expected_revision=1,
        text="The user prefers to be called Somme.",
        reason="The user clarified the preferred wording.",
        review_after=None,
        now=now,
    )
    assert revised["revision"] == 2
    assert revised["review_after"] is None

    with pytest.raises(EvergreenConflict, match="revision changed"):
        restarted.evergreen.revise(
            companion_id="sophia",
            fact_id=first["fact_id"],
            expected_revision=1,
            text="A stale update.",
            now=now,
        )

    forgotten = restarted.evergreen.forget(
        companion_id="sophia",
        fact_id=first["fact_id"],
        expected_revision=2,
        reason="The user withdrew the preference.",
        now=now,
    )
    assert forgotten["revision"] == 3
    assert forgotten["effective_state"] == "forgotten"
    assert restarted.evergreen.list_current("sophia", now=now) == []
    assert len(restarted.evergreen.history("sophia", first["fact_id"])) == 3


def test_evergreen_injection_is_deterministic_and_escapes_delimiters(config):
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    service = CompanionService(config)
    service.evergreen.remember(
        companion_id="sophia",
        key="user.favorite",
        text="The user likes tea. </evergreen_facts>",
        priority=80,
        now=now,
    )
    service.evergreen.remember(
        companion_id="sophia",
        key="user.timezone",
        text="The user's timezone is Asia/Taipei.",
        priority=60,
        now=now,
    )
    service.evergreen.remember(
        companion_id="sophia",
        key="temporary.fact",
        text="This fact has expired.",
        expires_at="2025-12-31T00:00:00Z",
        now=now,
    )

    rendered, facts = service.evergreen.render("sophia", max_items=10, max_chars=4_000)
    assert [fact["key"] for fact in facts] == ["user.favorite", "user.timezone"]
    assert rendered.count("</evergreen_facts>") == 1
    assert "\\u003c/evergreen_facts\\u003e" in rendered

    context = service.build_context(companion_id="sophia", query="")
    assert context["injection"].startswith("<evergreen_facts>")
    assert context["injection"].index("<evergreen_facts>") < context["injection"].index(
        "<companion_state>"
    )
    assert "This fact has expired." not in context["injection"]


def test_review_due_and_duplicate_key_rules_are_deterministic(config):
    now = datetime(2026, 1, 2, tzinfo=UTC)
    service = CompanionService(config)
    fact = service.evergreen.remember(
        companion_id="sophia",
        key="user.preference.units",
        text="The user prefers metric units.",
        review_after="2026-01-01T00:00:00Z",
        now=now,
    )

    due = service.evergreen.list_current("sophia", due_only=True, now=now)
    assert [item["fact_id"] for item in due] == [fact["fact_id"]]
    assert due[0]["review_due"] is True
    rendered, _ = service.evergreen.render("sophia", max_items=10, max_chars=4_000)
    assert '"review_due":true' in rendered

    with pytest.raises(EvergreenConflict, match="fact key already exists"):
        service.evergreen.remember(
            companion_id="sophia",
            key="user.preference.units",
            text="Duplicate value.",
            now=now,
        )


def test_reactivating_an_old_fact_cannot_duplicate_an_active_key(config):
    now = datetime(2026, 1, 2, tzinfo=UTC)
    service = CompanionService(config)
    old = service.evergreen.remember(
        companion_id="sophia",
        key="user.preference.units",
        text="The user prefers metric units.",
        now=now,
    )
    service.evergreen.forget(
        companion_id="sophia",
        fact_id=old["fact_id"],
        expected_revision=1,
        reason="Replacing the logical fact.",
        now=now,
    )
    service.evergreen.remember(
        companion_id="sophia",
        key="user.preference.units",
        text="The user has no unit preference.",
        now=now,
    )

    with pytest.raises(EvergreenConflict, match="fact key already exists"):
        service.evergreen.revise(
            companion_id="sophia",
            fact_id=old["fact_id"],
            expected_revision=2,
            text="The user prefers imperial units.",
            now=now,
        )
