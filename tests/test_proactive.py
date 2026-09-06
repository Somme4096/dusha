from __future__ import annotations

from datetime import UTC, datetime, timedelta

from companion_gateway.proactive import ProactiveEngine
from companion_gateway.service import CompanionService


def _old_user_message(service: CompanionService, when: datetime, external_id: str = "u1") -> None:
    service.ingest_message(
        companion_id="sophia",
        harness="astrbot",
        conversation_id="discord-main:FriendMessage:123",
        route="discord-main:FriendMessage:123",
        role="user",
        content="I will be away for a while.",
        external_id=external_id,
        occurred_at=when,
        affect_label="neutral",
    )


def test_queue_lease_ack_cooldown_and_restart(config):
    start = datetime(2026, 9, 6, 0, 0, tzinfo=UTC)
    now = start + timedelta(hours=5)
    service = CompanionService(config)
    _old_user_message(service, start)
    engine = ProactiveEngine(service, config)

    created = engine.evaluate("sophia", now)
    assert created is not None
    assert created["target"]["route"] == "discord-main:FriendMessage:123"
    assert chr(0x2014) not in created["generation_instruction"]
    assert engine.evaluate("sophia", now) is None

    leased = engine.poll("astrbot", "sophia", now=now, harness="astrbot")
    assert [item["id"] for item in leased] == [created["id"]]

    restarted_service = CompanionService(config)
    restarted = ProactiveEngine(restarted_service, config)
    assert restarted.poll("other", "sophia", now=now, harness="astrbot") == []
    restarted.acknowledge(created["id"], "astrbot", "sent", text="Checking in.", now=now)
    assert restarted.evaluate("sophia", now + timedelta(minutes=359)) is None
    state = restarted_service.affect.status("sophia", now)
    assert state["unanswered_proactive"] == 1
    assert any(message["text"] == "Checking in." for message in restarted_service.memory.recent("sophia"))


def test_user_reply_resets_unanswered_limit(config):
    start = datetime(2026, 9, 5, 0, 0, tzinfo=UTC)
    first_send = start + timedelta(hours=5)
    service = CompanionService(config)
    _old_user_message(service, start)
    engine = ProactiveEngine(service, config)
    event = engine.evaluate("sophia", first_send)
    engine.poll("astrbot", "sophia", now=first_send, harness="astrbot")
    engine.acknowledge(event["id"], "astrbot", "sent", now=first_send)
    assert engine.evaluate("sophia", first_send + timedelta(hours=12)) is None

    reply_time = first_send + timedelta(hours=12)
    _old_user_message(service, reply_time, external_id="u2")
    state = service.affect.status("sophia", reply_time)
    assert state["unanswered_proactive"] == 0


def test_quiet_hours_block_creation(config):
    config.proactive.quiet_start_hour = 1
    config.proactive.quiet_end_hour = 8
    service = CompanionService(config)
    engine = ProactiveEngine(service, config)
    user_time = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    _old_user_message(service, user_time)
    quiet_local_two_am = datetime(2026, 9, 5, 18, 0, tzinfo=UTC)
    assert engine.evaluate("sophia", quiet_local_two_am) is None


def test_failed_delivery_waits_before_retry(config):
    start = datetime(2026, 9, 6, 0, 0, tzinfo=UTC)
    now = start + timedelta(hours=5)
    service = CompanionService(config)
    _old_user_message(service, start)
    engine = ProactiveEngine(service, config)
    event = engine.evaluate("sophia", now)
    assert event is not None
    assert engine.poll("astrbot", "sophia", now=now, harness="astrbot")
    engine.acknowledge(event["id"], "astrbot", "failed", error="network", now=now)
    assert engine.poll("astrbot", "sophia", now=now + timedelta(minutes=14), harness="astrbot") == []
    retried = engine.poll("astrbot", "sophia", now=now + timedelta(minutes=15), harness="astrbot")
    assert [item["id"] for item in retried] == [event["id"]]
