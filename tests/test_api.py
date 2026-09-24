from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from companion_gateway import api
from companion_gateway.config import AppConfig, IdentityPromptConfig, MemoryConfig


async def _run_inline(function, /, *args, **kwargs):
    return function(*args, **kwargs)


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def _to_thread_inline(monkeypatch):
    monkeypatch.setattr(api.asyncio, "to_thread", _run_inline)


def _proxy_app(config, monkeypatch, fake_upstream=None):
    config.upstream.base_url = "https://upstream.invalid/v1"
    if fake_upstream is not None:
        monkeypatch.setattr(api, "_upstream_request", fake_upstream)
    return api.create_app(config)


def _reply(content, **kwargs):
    return httpx.Response(
        200, json={"choices": [{"index": 0, "message": {"role": "assistant", "content": content}}]}, **kwargs
    )


def _chat(client, messages, headers=None, **extra):
    return client.post(
        "/v1/chat/completions",
        json={"model": "upstream", "messages": messages, **extra},
        headers=headers,
    )


def _budget_cfg(tmp_path):
    identity_path = tmp_path / "identity.md"
    identity_path.write_text("IDENTITY_SENTINEL text", encoding="utf-8")
    return AppConfig(
        data_dir=tmp_path / "data",
        timezone="Asia/Taipei",
        memory=MemoryConfig(recent_messages=2, search_hits=4, context_messages=1, injection_max_chars=64),
        identity_prompt=IdentityPromptConfig(path=str(identity_path)),
    )


STREAM_CHUNKS = [
    b'data: {"choices":[{"delta":{"content":"streamed "}}]}\n\n',
    b'data: {"choices":[{"delta":{"content":"reply"}}]}\n\n',
    b"data: [DONE]\n\n",
]


class _FakeStreamClient:
    def __init__(self, chunks, status_code=200, content_type="text/event-stream"):
        self._chunks = chunks
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self.content = b"".join(chunks)

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    async def aread(self):
        return self.content

    async def aclose(self):
        pass

    def build_request(self, method, url, **kwargs):
        return ("build_request", url)

    async def send(self, request, stream=False):
        return self


async def _stream_post(config, monkeypatch, fake, content="Hi"):
    config.upstream.base_url = "https://upstream.invalid/v1"
    app = api.create_app(config)
    async with _client(app) as client:
        monkeypatch.setattr(api.httpx, "AsyncClient", lambda *args, **kwargs: fake)
        response = await client.post("/v1/chat/completions",
                                     json={"model": "upstream", "stream": True,
                                           "messages": [{"role": "user", "content": content}]})
    return app, response


async def test_consolidated_error_mappings_keep_status_and_detail(config, monkeypatch):
    cases = [
        ("GET", "/state/v1/evergreen/facts/missing/history", None, 404, "evergreen fact not found"),
        ("POST", "/state/v1/evergreen/facts/missing/revisions", {"expected_revision": 1, "text": "nope"},
         404, "evergreen fact not found"),
        ("POST", "/state/v1/evergreen/facts/missing/forget", {"expected_revision": 1, "reason": "nope"},
         404, "evergreen fact not found"),
        ("POST", "/state/v1/evergreen/facts", {"key": "INVALID KEY", "text": "x"}, 422, None),
        ("POST", "/state/v1/affect/events", {"label": "not-a-real-label"}, 422, None),
        ("POST", "/state/v1/messages/999999/affect", {"label": "neutral"},
         404, "affect classification not found"),
        ("POST", "/state/v1/proactive/events/nope/ack", {"consumer": "c", "outcome": "bogus"}, 409, None),
        ("POST", "/state/v1/proactive/events/nope/ack", {"consumer": "c", "outcome": "sent"},
         404, "event not found"),
        ("POST", "/state/v1/messages",
         {"harness": "test", "conversation_id": "one", "role": "user", "content": ""},
         422, "message content is empty"),
    ]
    async with _client(api.create_app(config)) as client:
        for method, path, payload, status, detail in cases:
            response = await client.request(method, path, json=payload)
            assert response.status_code == status
            if detail is not None:
                assert response.json()["detail"] == detail
    with pytest.raises(RuntimeError, match="boom"):
        api._raise_http(RuntimeError("boom"))


async def test_state_api_and_openai_proxy_preserve_one_canonical_transcript(config, monkeypatch):
    captured: list[dict] = []
    replies = iter(["First reply.", "Second reply."])

    async def fake_upstream(request, cfg, path, body):
        captured.append(body)
        return _reply(next(replies), headers={"content-type": "application/json"})

    app = _proxy_app(config, monkeypatch, fake_upstream)
    async with _client(app) as client:
        headers = {"X-Conversation-Id": "portable-thread", "X-Harness": "test-harness"}
        first = await _chat(client, [{"role": "user", "content": "Hello."}], headers)
        assert first.status_code == 200
        second = await _chat(client, [{"role": "user", "content": "Hello."},
                                      {"role": "assistant", "content": "First reply."},
                                      {"role": "user", "content": "Remember the amber window."}], headers)
        assert second.status_code == 200
        search = await client.post("/state/v1/memory/search", json={"query": "amber window"})
        assert search.status_code == 200
        recalled = [m["text"] for hit in search.json()["results"] for m in hit["messages"]]
        assert "Remember the amber window." in recalled
    stored = app.state.service.memory.recent(limit=10)
    assert [m["text"] for m in stored] == ["Hello.", "First reply.", "Remember the amber window.",
                                           "Second reply."]
    assert stored[0]["content"] == {"role": "user", "content": "Hello."}
    assert app.state.service.affect.classification(stored[0]["id"])["status"] == "applied"
    assert app.state.service.affect.classification(stored[2]["id"])["status"] == "applied"
    assert len(captured) == 2
    injected = captured[1]["messages"][0]
    assert injected["role"] == "system"
    assert "<companion_state>" in injected["content"]
    assert "amber window" not in injected["content"]
    assert "First reply." not in injected["content"]


async def test_proxy_replay_dedups_through_upstream_round_trip(config, monkeypatch):
    async def fake_upstream(request, cfg, path, body):
        return _reply("Final reply.")

    app = _proxy_app(config, monkeypatch, fake_upstream)
    transcript = [
        {"role": "system", "content": "ignored"},
        {"role": "user", "content": "Hello."},
        {"role": "assistant", "content": "A reply."},
        {"role": "user", "content": "Remember the amber window."},
    ]
    async with _client(app) as client:
        first = await _chat(client, transcript)
        replay = await _chat(client, transcript)
        assert first.status_code == replay.status_code == 200
    stored = app.state.service.memory.recent(limit=10)
    assert [m["text"] for m in stored] == [
        "Hello.", "A reply.", "Remember the amber window.", "Final reply.",
    ]


async def test_proxy_tool_call_body_preserved_and_archived(config, monkeypatch):
    tool_message = {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "call_1", "type": "function",
                        "function": {"name": "lookup", "arguments": '{"key":"value"}'}}],
    }
    captured: list[dict] = []

    async def fake_tool_reply(request, cfg, path, body):
        return httpx.Response(200, json={"choices": [{"index": 0, "message": tool_message}]})

    async def fake_ok_reply(request, cfg, path, body):
        captured.append(body)
        return _reply("ok")

    app = _proxy_app(config, monkeypatch, fake_tool_reply)
    async with _client(app) as client:
        response = await _chat(
            client, [{"role": "user", "content": "Look it up."}], {"X-Conversation-Id": "tools"}
        )
    assert response.status_code == 200
    stored = app.state.service.memory.recent(limit=10)
    assert stored[-1]["content"] == tool_message
    assert '"name":"lookup"' in stored[-1]["text"]
    assert app.state.service.affect.classification(stored[0]["id"])["status"] == "pending"

    app2 = _proxy_app(config, monkeypatch, fake_ok_reply)
    async with _client(app2) as client:
        forwarded = await _chat(client, [{"role": "user", "content": "Look it up."}, tool_message])
    assert forwarded.status_code == 200
    assert captured[0]["messages"][2] == tool_message


async def test_proxy_preserves_unknown_fields_and_caller_auth(config, monkeypatch):
    captured: dict = {}

    async def fake_upstream(request, cfg, path, body):
        captured.update(body)
        captured["_headers"] = dict(request.headers)
        return _reply("ok")

    async with _client(_proxy_app(config, monkeypatch, fake_upstream)) as client:
        response = await _chat(
            client, [{"role": "user", "content": "Hello.", "custom_message_field": "preserved"}],
            {"Authorization": "Bearer caller-token"}, custom_top_level_field={"keep": "me"},
        )
    assert response.status_code == 200
    assert captured["custom_top_level_field"] == {"keep": "me"}
    assert captured["messages"][0]["role"] == "system"
    assert "<companion_state>" in captured["messages"][0]["content"]
    assert captured["messages"][1] == {"role": "user", "content": "Hello.",
                                       "custom_message_field": "preserved"}
    assert captured["_headers"].get("authorization") == "Bearer caller-token"

    # Upstream error responses are forwarded verbatim.
    async def fake_error(request, cfg, path, body):
        return httpx.Response(503, content=b"raw upstream error body", headers={"content-type": "text/plain"})

    async with _client(_proxy_app(config, monkeypatch, fake_error)) as client:
        error = await _chat(client, [{"role": "user", "content": "Hi"}])
    assert error.status_code == 503
    assert error.content == b"raw upstream error body"


async def test_proxy_models_503_and_passthrough(config, monkeypatch):
    config.upstream.base_url = ""
    async with _client(api.create_app(config)) as client:
        models = await client.get("/v1/models")
    assert models.status_code == 503
    assert models.json()["detail"] == "upstream.base_url is not configured"

    captured: dict = {}

    async def fake_upstream(request, cfg, path, body):
        captured.update({"path": path, "body": body})
        return httpx.Response(
            200, content=b'{"data":[{"id":"gpt-test"}]}', headers={"content-type": "application/json"}
        )

    async with _client(_proxy_app(config, monkeypatch, fake_upstream)) as client:
        response = await client.get("/v1/models")
    assert response.status_code == 200
    assert captured == {"path": "/models", "body": None}
    assert response.content == b'{"data":[{"id":"gpt-test"}]}'
    assert response.headers["content-type"] == "application/json"


@pytest.mark.parametrize(
    ("fake", "status", "content", "check"),
    [
        (_FakeStreamClient(STREAM_CHUNKS), 200, "Hi", "raw"),
        (_FakeStreamClient([b"upstream exploded"], status_code=502, content_type="text/plain"),
         502, "Hi", "error"),
        (_FakeStreamClient(STREAM_CHUNKS), 200, "Stream it.", "drain"),
    ],
)
async def test_proxy_stream_passthrough_and_drain(config, monkeypatch, fake, status, content, check):
    app, response = await _stream_post(config, monkeypatch, fake, content=content)
    assert response.status_code == status
    if check == "error":
        assert response.content == b"upstream exploded"
        assert response.headers["content-type"].startswith("text/plain")
    elif check == "drain":
        stored = app.state.service.memory.recent(limit=10)
        assert [m["role"] for m in stored] == ["user", "assistant"]
        assert stored[-1]["text"] == stored[-1]["content"] == "streamed reply"
        assert stored[-1]["external_id"].startswith("proxy:")
    else:
        assert STREAM_CHUNKS[0] in response.content
        assert b"data: [DONE]\n\n" in response.content


async def test_agent_affect_update_has_resource_wrapper_and_conflict_rules(config, monkeypatch):
    async with _client(api.create_app(config)) as client:
        message = await client.post("/state/v1/messages",
                                    json={"harness": "astrbot", "conversation_id": "one", "role": "user",
                                          "content": "I hate you.", "external_id": "affect-source"})
        message_id = message.json()["id"]
        recorded = await client.post(f"/state/v1/messages/{message_id}/affect", json={"label": "neutral"})
        retry = await client.post(f"/state/v1/messages/{message_id}/affect", json={"label": "neutral"})
        conflict = await client.post(f"/state/v1/messages/{message_id}/affect", json={"label": "hostile"})
        invalid = await client.post(f"/state/v1/messages/{message_id}/affect", json={"label": "angsty"})
    assert recorded.status_code == 200
    assert set(recorded.json()) == {"event"}
    assert recorded.json()["event"]["label"] == "neutral"
    assert recorded.json()["event"]["decision_source"] == "agent"
    assert retry.json() == recorded.json()
    assert conflict.status_code == 409
    assert invalid.status_code == 422


async def test_evergreen_api_lifecycle_and_memory_context(config, monkeypatch):
    async with _client(api.create_app(config)) as client:
        message = await client.post("/state/v1/messages",
                                    json={"harness": "test", "conversation_id": "one", "role": "user",
                                          "content": "My preferred editor is Helix."})
        message_id = message.json()["id"]
        created = await client.post("/state/v1/evergreen/facts",
                                    json={"key": "user.preference.editor", "text": "The user prefers Helix.",
                                          "source_message_id": message_id})
        assert created.status_code == 200
        fact = created.json()["fact"]
        duplicate = await client.post("/state/v1/evergreen/facts",
                                      json={"key": "user.preference.editor",
                                            "text": "The user prefers another editor."})
        assert duplicate.status_code == 409
        wrong_revise = await client.post(f"/state/v1/evergreen/facts/{fact['fact_id']}/revisions",
                                         json={"expected_revision": 99, "text": "Wrong base."})
        assert wrong_revise.status_code == 409
        wrong_forget = await client.post(f"/state/v1/evergreen/facts/{fact['fact_id']}/forget",
                                         json={"expected_revision": 99, "reason": "Wrong base."})
        assert wrong_forget.status_code == 409
        revised = await client.post(f"/state/v1/evergreen/facts/{fact['fact_id']}/revisions",
                                    json={"expected_revision": 1, "text": "The user usually prefers Helix.",
                                          "reason": "Preference clarified."})
        assert revised.status_code == 200
        assert revised.json()["fact"]["revision"] == 2
        listed = await client.get("/state/v1/evergreen/facts")
        assert listed.json()["facts"][0]["text"] == "The user usually prefers Helix."
        history = await client.get(f"/state/v1/evergreen/facts/{fact['fact_id']}/history")
        assert len(history.json()["revisions"]) == 2
        context = await client.get(f"/state/v1/memory/{message_id}", params={"context_messages": 1})
        assert context.status_code == 200
        assert context.json()["messages"][0]["text"] == "My preferred editor is Helix."
        forgotten = await client.post(f"/state/v1/evergreen/facts/{fact['fact_id']}/forget",
                                      json={"expected_revision": 2, "reason": "Preference withdrawn."})
        assert forgotten.status_code == 200
        assert forgotten.json()["fact"]["effective_state"] == "forgotten"


async def test_api_context_shape_and_same_state_auth(config, monkeypatch):
    async with _client(api.create_app(config)) as client:
        response = await client.post(
            "/state/v1/context", json={"harness": "api", "conversation_id": "one", "query": ""}
        )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"injection", "affect", "evergreen_facts", "records", "search_hits", "context"}
    assert body["context"]["emotion"]["fingerprints"]["prompts"].startswith("sha256:")
    assert "<companion_state>" in body["injection"]

    config.api_token_env = "TEST_TOKEN_ENV"
    monkeypatch.setenv("TEST_TOKEN_ENV", "secret-token")
    async with _client(api.create_app(config)) as client:
        denied = await client.post("/state/v1/context", json={"query": ""})
        allowed = await client.post(
            "/state/v1/context", json={"query": ""}, headers={"X-Companion-Token": "secret-token"}
        )
    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert allowed.json()["context"]["identity"]["configured"] is False


async def test_api_context_budget_and_validation_errors(config, monkeypatch, tmp_path):
    async with _client(api.create_app(config)) as client:
        invalid = await client.post("/state/v1/context", json={"query": 123})
    assert invalid.status_code == 422
    detail = invalid.json()["detail"]
    assert isinstance(detail, list)
    assert detail
    for item in detail:
        assert isinstance(item, dict)
        assert {"loc", "msg", "type"} <= set(item)
    async with _client(api.create_app(_budget_cfg(tmp_path))) as client:
        budget = await client.post("/state/v1/context", json={"query": ""})
    assert budget.status_code == 422
    budget_detail = budget.json()["detail"]
    assert isinstance(budget_detail, str)
    assert "too small for mandatory identity and companion" in budget_detail


DOC_FILES = [
    Path(__file__).parents[1] / "README.md",
    Path(__file__).parents[1] / "docs" / "configuration.md",
    Path(__file__).parents[1] / "docs" / "guide.md",
    Path(__file__).parents[1] / "docs" / "api.md",
]
MESSAGE_KEYS = {"id", "conversation_id", "role", "text", "content", "external_id", "occurred_at",
                "ingested_at", "sha256", "harness", "external_conversation_id", "route"}
MEMORY_INDEX_KEYS = {"mode", "enabled", "configured", "chunker_key", "embedding_key", "messages",
                     "chunked_messages", "chunks", "embedded_chunks", "dimensions", "cooling_down",
                     "last_error"}


def test_all_doc_json_fences_are_valid_json():
    for doc in DOC_FILES:
        text = doc.read_text(encoding="utf-8")
        for block in re.findall(r"```json\n(.*?)```", text, re.DOTALL):
            assert "..." not in block, f"{doc}: ellipsis in json fence"
            assert isinstance(json.loads(block), (dict, list)), f"{doc}: invalid json fence"


async def test_openapi_paths_are_documented(config, monkeypatch):
    app = api.create_app(config)
    async with _client(app) as client:
        openapi = (await client.get("/openapi.json")).json()
        paths = openapi["paths"]
        for path in [
            "/health", "/state/v1/messages", "/state/v1/messages/{message_id}",
            "/state/v1/memory/index", "/state/v1/memory/search", "/state/v1/memory/{message_id}",
            "/state/v1/evergreen/facts", "/state/v1/evergreen/facts/{fact_id}/history",
            "/state/v1/evergreen/facts/{fact_id}/revisions", "/state/v1/evergreen/facts/{fact_id}/forget",
            "/state/v1/context", "/state/v1/affect", "/state/v1/messages/{message_id}/affect",
            "/state/v1/affect/events", "/state/v1/proactive/evaluate", "/state/v1/proactive/events",
            "/state/v1/proactive/events/{event_id}/ack", "/v1/models", "/v1/chat/completions",
        ]:
            assert path in paths, f"missing documented path {path}"


def test_configuration_export_commands_roundtrip(tmp_path):
    from companion_gateway import emotions
    from companion_gateway import prompts as prompts_module
    from companion_gateway.config import AppConfig, PromptsConfig
    from companion_gateway.service import CompanionService

    configuration = (Path(__file__).parents[1] / "docs" / "configuration.md").read_text(encoding="utf-8")
    commands = [cmd for block in re.findall(r"```sh\n(.*?)```", configuration, re.DOTALL)
                for cmd in re.findall(r'python -c "(.*?)"', block, re.DOTALL)]
    prompts_cmd = next(c for c in commands if "companion_gateway.prompts" in c)
    emotions_cmd = next(c for c in commands if "companion_gateway.emotions" in c)
    for command in (emotions_cmd, prompts_cmd):
        result = subprocess.run([sys.executable, "-c", command], cwd=tmp_path, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    prompts_file = tmp_path / "prompts.json"
    emotions_file = tmp_path / "emotions.json"
    assert prompts_file.exists() and emotions_file.exists()
    prompts_module.load_prompts(prompts_file)
    emotions.load_emotions(emotions_file)

    data = json.loads(prompts_file.read_text(encoding="utf-8"))
    data["companion_state"]["affect_instruction"] = "SENTINEL_EXPORT"
    prompts_file.write_text(json.dumps(data), encoding="utf-8")
    cfg = AppConfig(
        data_dir=tmp_path / "data",
        timezone="Asia/Taipei",
        prompts=PromptsConfig(path=str(prompts_file)),
    )
    service = CompanionService(cfg)
    injection = service.build_context(query="")["injection"]
    assert "SENTINEL_EXPORT" in injection
    assert "Treat the affect description" not in injection
    service.close()


async def test_endpoint_response_keys_and_conflict_flow(config, monkeypatch):
    app = api.create_app(config)
    async with _client(app) as client:
        ingest = await client.post(
            "/state/v1/messages",
            json={"harness": "api", "conversation_id": "docs", "role": "user",
                  "content": "Remember the amber window.", "external_id": "docs-example"},
        )
        assert ingest.status_code == 200
        message_id = ingest.json()["id"]
        stored = await client.get(f"/state/v1/messages/{message_id}")
        assert stored.status_code == 200
        assert MESSAGE_KEYS <= set(stored.json())
        assert len(stored.json()["sha256"]) == 64
        missing = await client.get("/state/v1/messages/999999")
        assert missing.status_code == 404
        assert isinstance(missing.json()["detail"], str)
        index = await client.get("/state/v1/memory/index")
        assert index.status_code == 200
        assert MEMORY_INDEX_KEYS <= set(index.json())
        search = await client.post("/state/v1/memory/search", json={"query": "amber window", "limit": 2})
        assert search.status_code == 200
        for hit in search.json()["results"]:
            assert {"hit_id", "rank", "messages"} <= set(hit)
        affect = await client.get("/state/v1/affect")
        assert affect.status_code == 200
        assert {"base", "mood", "last_updated_at", "last_user_message_at", "last_proactive_sent_at",
                "unanswered_proactive"} <= set(affect.json())
