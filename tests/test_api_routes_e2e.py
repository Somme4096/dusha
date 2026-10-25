from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from dusha import api


async def _run_inline(function, /, *args, **kwargs):
    return function(*args, **kwargs)


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _stored(app):
    messages = []
    async with _client(app) as client:
        for message_id in range(1, 50):
            response = await client.get(f"/state/v1/messages/{message_id}")
            if response.status_code == 404:
                break
            messages.append(response.json())
    return messages


@pytest.fixture(autouse=True)
def _to_thread_inline(monkeypatch):
    monkeypatch.setattr(api.asyncio, "to_thread", _run_inline)


async def test_consolidated_error_mappings_keep_status_and_detail(config, monkeypatch):
    cases = [
        ("GET", "/state/v1/evergreen/facts/missing/history", None, 404, "evergreen fact not found"),
        ("POST", "/state/v1/evergreen/facts/missing/revisions", {"expected_revision": 1, "text": "nope"},
         404, "evergreen fact not found"),
        ("POST", "/state/v1/evergreen/facts/missing/forget", {"expected_revision": 1, "reason": "nope"},
         404, "evergreen fact not found"),
        ("POST", "/state/v1/evergreen/facts", {"key": "INVALID KEY", "text": "x"}, 422, None),
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


async def test_unknown_message_field_is_rejected(config):
    async with _client(api.create_app(config)) as client:
        rejected = await client.post(
            "/state/v1/messages",
            json={"harness": "astrbot", "conversation_id": "one", "role": "user",
                  "content": "I hate you.", "affect_label": "hostile"},
        )
    assert rejected.status_code == 422


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


async def test_memo_api_lifecycle_and_auth(config, monkeypatch):
    async with _client(api.create_app(config)) as client:
        added = await client.post("/state/v1/memo/add", json={"text": "Draft the report."})
        assert added.status_code == 200
        note = added.json()["memo"]
        assert note["status"] == "active"

        listed = await client.get("/state/v1/memo/list")
        assert [memo["id"] for memo in listed.json()["memos"]] == [note["id"]]

        empty_reason = await client.post(
            f"/state/v1/memo/{note['id']}/done", json={"reason": "   "}
        )
        assert empty_reason.status_code == 422

        missing = await client.post("/state/v1/memo/999999/done", json={"reason": "Missing."})
        assert missing.status_code == 404
        assert missing.json()["detail"] == "memo not found"

        done = await client.post(
            f"/state/v1/memo/{note['id']}/done", json={"reason": "Published."}
        )
        assert done.status_code == 200
        assert done.json()["memo"]["status"] == "archived"

        active = await client.get("/state/v1/memo/list", params={"status": "active"})
        assert active.json()["memos"] == []
        archived = await client.get("/state/v1/memo/list", params={"status": "archived"})
        assert archived.json()["memos"][0]["reason"] == "Published."


async def test_api_context_shape_and_same_state_auth(config, monkeypatch):
    async with _client(api.create_app(config)) as client:
        response = await client.post(
            "/state/v1/context", json={"harness": "api", "conversation_id": "one", "query": ""}
        )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"injection", "records", "search_hits", "context"}
    assert "<companion_state>" in body["injection"]

    config.api_token = "secret-token"
    async with _client(api.create_app(config)) as client:
        denied = await client.post("/state/v1/context", json={"query": ""})
        allowed = await client.post(
            "/state/v1/context", json={"query": ""}, headers={"X-Companion-Token": "secret-token"}
        )
    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert "identity" not in allowed.json()["context"]["blocks"]


async def test_api_context_validation_errors(config):
    async with _client(api.create_app(config)) as client:
        invalid = await client.post("/state/v1/context", json={"query": 123})
    assert invalid.status_code == 422
    detail = invalid.json()["detail"]
    assert isinstance(detail, list)
    assert detail
    for item in detail:
        assert isinstance(item, dict)
        assert {"loc", "msg", "type"} <= set(item)


DOC_FILES = [
    Path(__file__).parents[1] / "README.md",
    Path(__file__).parents[1] / "docs" / "configuration.md",
    Path(__file__).parents[1] / "docs" / "guide.md",
    Path(__file__).parents[1] / "docs" / "api.md",
]
MESSAGE_KEYS = {"id", "conversation_id", "role", "text", "content", "external_id", "occurred_at",
                "ingested_at", "harness", "external_conversation_id", "route"}


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
            "/state/v1/plugins", "/state/v1/memory/search", "/state/v1/memory/{message_id}",
            "/state/v1/evergreen/facts", "/state/v1/evergreen/facts/{fact_id}/history",
            "/state/v1/evergreen/facts/{fact_id}/revisions", "/state/v1/evergreen/facts/{fact_id}/forget",
            "/state/v1/memo/add", "/state/v1/memo/list", "/state/v1/memo/{note_id}/done",
            "/state/v1/context", "/state/v1/affect",
            "/state/v1/proactive/evaluate", "/state/v1/proactive/events",
            "/state/v1/proactive/events/{event_id}/ack", "/state/v1/transcript",
        ]:
            assert path in paths, f"missing documented path {path}"


async def test_configuration_export_commands_roundtrip(tmp_path):
    from dusha import emotions
    from dusha import prompts as prompts_module
    from dusha.config import AppConfig, PromptsConfig

    configuration = (Path(__file__).parents[1] / "docs" / "configuration.md").read_text(encoding="utf-8")
    commands = [cmd for block in re.findall(r"```sh\n(.*?)```", configuration, re.DOTALL)
                for cmd in re.findall(r'python -c "(.*?)"', block, re.DOTALL)]
    prompts_cmd = next(c for c in commands if "dusha.prompts" in c)
    emotions_cmd = next(c for c in commands if "dusha.emotions" in c)
    for command in (emotions_cmd, prompts_cmd):
        result = subprocess.run([sys.executable, "-c", command], cwd=tmp_path, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    prompts_file = tmp_path / "prompts.json"
    emotions_file = tmp_path / "emotions.json"
    assert prompts_file.exists() and emotions_file.exists()
    prompts_module.load_prompts(prompts_file)
    emotions.load_emotions(emotions_file)

    data = json.loads(prompts_file.read_text(encoding="utf-8"))
    data["affect"]["instruction"] = "SENTINEL_EXPORT"
    prompts_file.write_text(json.dumps(data), encoding="utf-8")
    cfg = AppConfig(
        data_dir=tmp_path / "data",
        timezone="Asia/Taipei",
        prompts=PromptsConfig(path=str(prompts_file)),
    )
    async with _client(api.create_app(cfg)) as client:
        injection = (await client.post("/state/v1/context", json={"query": ""})).json()["injection"]
    assert "SENTINEL_EXPORT" in injection
    assert "Treat the affect description" not in injection


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
        missing = await client.get("/state/v1/messages/999999")
        assert missing.status_code == 404
        assert isinstance(missing.json()["detail"], str)
        plugins = await client.get("/state/v1/plugins")
        assert plugins.status_code == 200
        assert list(plugins.json()) == ["identity", "memory", "evergreen", "memo", "affect", "proactive"]
        assert {report["state"] for report in plugins.json().values()} == {"active"}
        search = await client.post("/state/v1/memory/search", json={"query": "amber window", "limit": 2})
        assert search.status_code == 200
        for hit in search.json()["results"]:
            assert {"hit_id", "rank", "messages"} <= set(hit)
        affect = await client.get("/state/v1/affect")
        assert affect.status_code == 200
        assert {"base", "mood", "last_updated_at", "last_user_message_at", "last_proactive_sent_at",
                "unanswered_proactive"} <= set(affect.json())


PROTECTED_ROUTES = [
    ("GET", "/state/v1/plugins", None, None),
    ("POST", "/state/v1/messages",
     {"harness": "api", "conversation_id": "auth", "role": "user", "content": "hi"}, None),
    ("GET", "/state/v1/messages/1", None, None),
    ("POST", "/state/v1/memory/search", {"query": "hi"}, None),
    ("GET", "/state/v1/memory/1", None, None),
    ("POST", "/state/v1/evergreen/facts", {"key": "auth.key", "text": "value"}, None),
    ("GET", "/state/v1/evergreen/facts", None, None),
    ("GET", "/state/v1/evergreen/facts/auth/history", None, None),
    ("POST", "/state/v1/evergreen/facts/auth/revisions",
     {"expected_revision": 1, "text": "value"}, None),
    ("POST", "/state/v1/evergreen/facts/auth/forget",
     {"expected_revision": 1, "reason": "done"}, None),
    ("POST", "/state/v1/memo/add", {"text": "auth memo"}, None),
    ("GET", "/state/v1/memo/list", None, None),
    ("POST", "/state/v1/memo/1/done", {"reason": "done"}, None),
    ("POST", "/state/v1/context", {"query": ""}, None),
    ("GET", "/state/v1/affect", None, None),
    ("POST", "/state/v1/proactive/evaluate", None, None),
    ("GET", "/state/v1/proactive/events", None, {"consumer": "c"}),
    ("POST", "/state/v1/proactive/events/evt/ack", {"consumer": "c", "outcome": "sent"}, None),
    ("POST", "/state/v1/transcript", {"messages": [{"role": "user", "content": "hi"}]}, None),
]


async def test_auth_disabled_local_mode_is_open(config):
    app = api.create_app(config)
    async with _client(app) as client:
        models = await client.get("/v1/models")
        context = await client.post("/state/v1/context", json={"query": ""})
        openapi = await client.get("/openapi.json")
    assert models.status_code == 404
    assert context.status_code == 200
    assert openapi.status_code == 200


@pytest.mark.parametrize(("method", "path", "payload", "params"), PROTECTED_ROUTES)
async def test_protected_routes_require_companion_token(
    config, monkeypatch, method, path, payload, params
):
    config.api_token = "secret-token"
    app = api.create_app(config)
    async with _client(app) as client:
        missing = await client.request(method, path, json=payload, params=params)
        wrong = await client.request(
            method, path, json=payload, params=params, headers={"X-Companion-Token": "wrong"}
        )
        right = await client.request(
            method, path, json=payload, params=params,
            headers={"X-Companion-Token": "secret-token"},
        )
    assert missing.status_code == 401
    assert missing.json()["detail"] == "invalid companion token"
    assert wrong.status_code == 401
    assert right.status_code != 401


async def test_health_stays_public_and_carries_no_token(config, monkeypatch):
    config.api_token = "secret-token"
    monkeypatch.setenv("TEST_TOKEN_ENV", "super-secret-token")
    app = api.create_app(config)
    async with _client(app) as client:
        health = await client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert "super-secret-token" not in health.text


async def test_api_docs_disabled_when_auth_enabled(config, monkeypatch):
    config.api_token = "secret-token"
    app = api.create_app(config)
    async with _client(app) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            response = await client.get(path)
            assert response.status_code == 404, path
