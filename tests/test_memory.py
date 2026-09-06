from __future__ import annotations

from companion_gateway.service import CompanionService


def test_raw_text_is_canonical_search_result_and_survives_restart(config):
    service = CompanionService(config)
    original = "昨日、Birch said exactly: 『青い硝子を忘れないで』\nSecond line, unchanged."
    stored = service.ingest_message(
        companion_id="sophia",
        harness="astrbot",
        conversation_id="discord:FriendMessage:42",
        route="discord:FriendMessage:42",
        role="user",
        content=original,
        external_id="discord-message-1",
        affect_label="neutral",
    )
    duplicate = service.ingest_message(
        companion_id="sophia",
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
    results = restarted.memory.search("sophia", "青い硝子")
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
        companion_id="sophia",
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
        companion_id="sophia",
        harness="api",
        conversation_id="one",
        route="opaque-route",
        role="user",
        content="The brass key is under the third flowerpot.",
        external_id="m1",
        affect_label="neutral",
    )
    current = service.ingest_message(
        companion_id="sophia",
        harness="api",
        conversation_id="one",
        route="opaque-route",
        role="user",
        content="Where did I leave the brass key?",
        external_id="m2",
        affect_label="neutral",
    )
    result = service.build_context(
        companion_id="sophia",
        harness="api",
        conversation_id="one",
        query="brass key",
        exclude_message_ids={current["id"]},
    )
    assert "The brass key is under the third flowerpot." in result["injection"]
    assert f'"memory_id":{first["id"]}' in result["injection"]
    assert "personality" not in result["injection"].casefold()
    assert "architecture" not in result["injection"].casefold()
    assert chr(0x2014) not in result["injection"]
