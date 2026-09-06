from __future__ import annotations

import httpx

from companion_gateway import api


async def _run_inline(function, /, *args, **kwargs):
    return function(*args, **kwargs)


async def test_state_api_and_openai_proxy_preserve_one_canonical_transcript(config, monkeypatch):
    config.upstream.base_url = "https://upstream.invalid/v1"
    captured: list[dict] = []
    replies = iter(["First reply.", "Second reply."])

    async def fake_upstream(request, cfg, path, body):
        captured.append(body)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": next(replies)},
                        "finish_reason": "stop",
                    }
                ],
            },
            headers={"content-type": "application/json"},
        )

    monkeypatch.setattr(api, "_upstream_request", fake_upstream)
    monkeypatch.setattr(api.asyncio, "to_thread", _run_inline)
    app = api.create_app(config)
    transport = httpx.ASGITransport(app=app)
    headers = {
        "X-Companion-Id": "sophia",
        "X-Conversation-Id": "portable-thread",
        "X-Harness": "test-harness",
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"model": "upstream", "messages": [{"role": "user", "content": "Hello."}]},
        )
        assert first.status_code == 200

        second = await client.post(
            "/v1/chat/completions",
            headers=headers,
            json={
                "model": "upstream",
                "messages": [
                    {"role": "user", "content": "Hello."},
                    {"role": "assistant", "content": "First reply."},
                    {"role": "user", "content": "Remember the amber window."},
                ],
            },
        )
        assert second.status_code == 200

        search = await client.post(
            "/state/v1/memory/search",
            json={"companion_id": "sophia", "query": "amber window"},
        )
        assert search.status_code == 200
        recalled = [message["text"] for hit in search.json()["results"] for message in hit["messages"]]
        assert "Remember the amber window." in recalled

    stored = app.state.service.memory.recent("sophia", limit=10)
    assert [message["text"] for message in stored] == [
        "Hello.",
        "First reply.",
        "Remember the amber window.",
        "Second reply.",
    ]
    assert stored[0]["content"] == {"role": "user", "content": "Hello."}
    assert len(captured) == 2
    injected = captured[1]["messages"][0]
    assert injected["role"] == "system"
    assert "<companion_state>" in injected["content"]
    assert "amber window" not in injected["content"]


async def test_proxy_archives_tool_call_messages(config, monkeypatch):
    config.upstream.base_url = "https://upstream.invalid/v1"
    tool_message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"key":"value"}'},
            }
        ],
    }

    async def fake_upstream(request, cfg, path, body):
        return httpx.Response(
            200,
            json={"choices": [{"index": 0, "message": tool_message, "finish_reason": "tool_calls"}]},
            headers={"content-type": "application/json"},
        )

    monkeypatch.setattr(api, "_upstream_request", fake_upstream)
    monkeypatch.setattr(api.asyncio, "to_thread", _run_inline)
    app = api.create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"X-Conversation-Id": "tools"},
            json={"model": "upstream", "messages": [{"role": "user", "content": "Look it up."}]},
        )
    assert response.status_code == 200
    stored = app.state.service.memory.recent("sophia", limit=10)
    assert stored[-1]["content"] == tool_message
    assert '"name":"lookup"' in stored[-1]["text"]


async def test_message_api_rejects_empty_content(config, monkeypatch):
    monkeypatch.setattr(api.asyncio, "to_thread", _run_inline)
    app = api.create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/state/v1/messages",
            json={
                "companion_id": "sophia",
                "harness": "test",
                "conversation_id": "one",
                "role": "user",
                "content": "",
            },
        )
    assert response.status_code == 422
    assert response.json()["detail"] == "message content is empty"
