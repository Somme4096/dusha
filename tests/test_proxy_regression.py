"""Phase 2: proxy regression evidence for preserved payload and transport shapes."""

from __future__ import annotations

import httpx

from companion_gateway import api


async def _run_inline(function, /, *args, **kwargs):
    return function(*args, **kwargs)


async def test_proxy_preserves_unknown_request_fields(config, monkeypatch):
    config.upstream.base_url = "https://upstream.invalid/v1"
    captured: dict = {}

    async def fake_upstream(request, cfg, path, body):
        captured.update(body)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            },
        )

    monkeypatch.setattr(api, "_upstream_request", fake_upstream)
    monkeypatch.setattr(api.asyncio, "to_thread", _run_inline)
    app = api.create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "upstream",
                "custom_top_level_field": {"keep": "me"},
                "messages": [
                    {
                        "role": "user",
                        "content": "Hello.",
                        "custom_message_field": "preserved",
                    }
                ],
            },
        )
    assert response.status_code == 200
    assert captured["custom_top_level_field"] == {"keep": "me"}
    assert captured["messages"][0]["role"] == "system"
    assert "<companion_state>" in captured["messages"][0]["content"]
    assert captured["messages"][1] == {
        "role": "user",
        "content": "Hello.",
        "custom_message_field": "preserved",
    }


async def test_proxy_tool_call_message_body_preserved(config, monkeypatch):
    config.upstream.base_url = "https://upstream.invalid/v1"
    captured: dict = {}
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
        captured.update(body)
        return httpx.Response(
            200,
            json={"choices": [{"index": 0, "message": tool_message, "finish_reason": "tool_calls"}]},
        )

    monkeypatch.setattr(api, "_upstream_request", fake_upstream)
    monkeypatch.setattr(api.asyncio, "to_thread", _run_inline)
    app = api.create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "upstream",
                "messages": [
                    {"role": "user", "content": "Look it up."},
                    tool_message,
                ],
            },
        )
    assert response.status_code == 200
    forwarded_tool = captured["messages"][2]
    assert forwarded_tool == tool_message


async def test_proxy_forwards_caller_authorization_header(config, monkeypatch):
    config.upstream.base_url = "https://upstream.invalid/v1"
    captured_headers: dict = {}

    async def fake_upstream(request, cfg, path, body):
        captured_headers.update(dict(request.headers))
        return httpx.Response(
            200,
            json={"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}]},
        )

    monkeypatch.setattr(api, "_upstream_request", fake_upstream)
    monkeypatch.setattr(api.asyncio, "to_thread", _run_inline)
    app = api.create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "upstream", "messages": [{"role": "user", "content": "Hi"}]},
            headers={"Authorization": "Bearer caller-token"},
        )
    assert response.status_code == 200
    assert captured_headers.get("authorization") == "Bearer caller-token"


async def test_proxy_forwards_upstream_error_raw_bytes(config, monkeypatch):
    config.upstream.base_url = "https://upstream.invalid/v1"

    async def fake_upstream(request, cfg, path, body):
        return httpx.Response(
            503,
            content=b"raw upstream error body",
            headers={"content-type": "text/plain"},
        )

    monkeypatch.setattr(api, "_upstream_request", fake_upstream)
    monkeypatch.setattr(api.asyncio, "to_thread", _run_inline)
    app = api.create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "upstream", "messages": [{"role": "user", "content": "Hi"}]},
        )
    assert response.status_code == 503
    assert response.content == b"raw upstream error body"


async def test_proxy_streaming_forwards_raw_bytes(config, monkeypatch):
    config.upstream.base_url = "https://upstream.invalid/v1"

    class FakeResponse:
        def __init__(self, chunks, status_code=200):
            self._chunks = chunks
            self.status_code = status_code
            self.headers = {"content-type": "text/event-stream"}

        async def aiter_bytes(self):
            for chunk in self._chunks:
                yield chunk

        async def aread(self):
            return b"".join(self._chunks)

        async def aclose(self):
            pass

    class FakeClient:
        response: FakeResponse | None = None

        def __init__(self, *args, **kwargs):
            pass

        def build_request(self, method, url, **kwargs):
            return ("build_request", url)

        async def send(self, request, stream=False):
            return self.response

        async def aclose(self):
            pass

    fake = FakeClient()
    fake.response = FakeResponse(
        [
            b'data: {"choices":[{"delta":{"content":"streamed "}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"reply"}}]}\n\n',
            b"data: [DONE]\n\n",
        ]
    )
    monkeypatch.setattr(api.asyncio, "to_thread", _run_inline)
    app = api.create_app(config)
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    # Patch the global httpx.AsyncClient only after the test client is created,
    # so _proxy_stream creates the fake while the test client stays real.
    monkeypatch.setattr(api.httpx, "AsyncClient", lambda *args, **kwargs: fake)
    try:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "upstream",
                "stream": True,
                "messages": [{"role": "user", "content": "Stream it."}],
            },
        )
    finally:
        await client.aclose()
    assert response.status_code == 200
    assert b'data: {"choices":[{"delta":{"content":"streamed "}}]}\n\n' in response.content
    assert b"data: [DONE]\n\n" in response.content