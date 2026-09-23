"""Phase 3: documentation example JSON, OpenAPI schema shapes, and CLI smoke."""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

import httpx

from companion_gateway import api

DOC_FILES = [
    Path(__file__).parents[1] / "README.md",
    Path(__file__).parents[1] / "docs" / "API.md",
    Path(__file__).parents[1] / "docs" / "CONFIGURATION.md",
    Path(__file__).parents[1] / "docs" / "CONFIGURATION_MIGRATION.md",
    Path(__file__).parents[1] / "docs" / "DECISION_MODULE_DESIGN.md",
    Path(__file__).parents[1] / "docs" / "INTEGRATION_GUIDE.md",
]


async def _run_inline(function, /, *args, **kwargs):
    return function(*args, **kwargs)


def _app(tmp_path):
    from companion_gateway.config import AppConfig, MemoryConfig

    return AppConfig(
        data_dir=tmp_path / "data",
        timezone="Asia/Taipei",
        memory=MemoryConfig(
            recent_messages=2, search_hits=4, context_messages=1, injection_max_chars=20_000
        ),
    )


def test_all_doc_json_fences_are_valid_json():
    for doc in DOC_FILES:
        text = doc.read_text(encoding="utf-8")
        blocks = re.findall(r"```json\n(.*?)```", text, re.DOTALL)
        for block in blocks:
            assert "..." not in block, f"{doc}: ellipsis in json fence"
            data = json.loads(block)  # raises on invalid or commented JSON
            assert isinstance(data, (dict, list))


def _resolve(schema, root):
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return _resolve(root["components"]["schemas"][name], root)
    if schema.get("type") == "object":
        return {
            key: _resolve(value, root)
            for key, value in schema.get("properties", {}).items()
        }
    return schema


async def test_openapi_documents_endpoints_and_context_shape(config, monkeypatch):
    monkeypatch.setattr(api.asyncio, "to_thread", _run_inline)
    app = api.create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        openapi = (await client.get("/openapi.json")).json()
        context_response = await client.post(
            "/state/v1/context", json={"query": "example"}
        )
    assert context_response.status_code == 200
    paths = openapi["paths"]
    for path in [
        "/health",
        "/state/v1/messages",
        "/state/v1/messages/{message_id}",
        "/state/v1/memory/index",
        "/state/v1/memory/search",
        "/state/v1/memory/{message_id}",
        "/state/v1/evergreen/facts",
        "/state/v1/evergreen/facts/{fact_id}/history",
        "/state/v1/evergreen/facts/{fact_id}/revisions",
        "/state/v1/evergreen/facts/{fact_id}/forget",
        "/state/v1/context",
        "/state/v1/affect",
        "/state/v1/messages/{message_id}/affect",
        "/state/v1/affect/events",
        "/state/v1/proactive/evaluate",
        "/state/v1/proactive/events",
        "/state/v1/proactive/events/{event_id}/ack",
        "/v1/models",
        "/v1/chat/completions",
    ]:
        assert path in paths, f"missing documented path {path}"

    ctx_schema = paths["/state/v1/context"]["post"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    props = _resolve(ctx_schema, openapi)
    assert set(props) == {
        "injection",
        "affect",
        "evergreen_facts",
        "records",
        "search_hits",
        "context",
    }
    assert set(props["context"]) == {"version", "identity", "instructions", "memory", "emotion"}
    assert set(props["context"]["identity"]) == {"configured", "text", "revision"}


async def test_doc_request_examples_run_against_app(monkeypatch):
    api_text = (Path(__file__).parents[1] / "docs" / "API.md").read_text(encoding="utf-8")
    sections = re.split(r"(?m)^### (POST|GET) (\S+)\s*$", api_text)
    expected_keys = {
        "POST /state/v1/messages": {"id", "duplicate", "conversation_id", "sha256", "affect"},
        "POST /state/v1/memory/search": {"results"},
        "POST /state/v1/context": {
            "injection",
            "affect",
            "evergreen_facts",
            "records",
            "search_hits",
            "context",
        },
        "POST /state/v1/affect/events": {"label", "event_id", "state"},
        "POST /state/v1/evergreen/facts": {"fact"},
    }
    with tempfile.TemporaryDirectory() as td:
        cfg = _app(Path(td))
        monkeypatch.setattr(api.asyncio, "to_thread", _run_inline)
        app = api.create_app(cfg)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for index in range(1, len(sections), 3):
                method, path, body = sections[index], sections[index + 1], sections[index + 2]
                key = f"{method} {path}"
                if key not in expected_keys:
                    continue
                blocks = re.findall(r"```json\n(.*?)```", body, re.DOTALL)
                assert blocks, f"{key}: missing request json block"
                payload = json.loads(blocks[0])
                response = await client.post(path, json=payload)
                assert response.status_code == 200, (
                    f"{key}: expected 200, got {response.status_code}: {response.text[:200]}"
                )
                assert expected_keys[key] <= set(response.json()), f"{key}: missing response keys"


def _json_blocks(text):
    return [json.loads(block) for block in re.findall(r"```json\n(.*?)```", text, re.DOTALL)]


def test_api_doc_examples_match_actual_response_shapes():
    api_text = (Path(__file__).parents[1] / "docs" / "API.md").read_text(encoding="utf-8")
    blocks = _json_blocks(api_text)

    health = next(b for b in blocks if isinstance(b, dict) and "memory_index" in b)
    assert {"mode", "enabled", "configured", "chunker_key", "embedding_key", "messages",
            "chunked_messages", "chunks", "embedded_chunks", "dimensions", "cooling_down",
            "last_error"} <= set(health["memory_index"])

    search = next(b for b in blocks if isinstance(b, dict) and "results" in b)
    hit = search["results"][0]
    assert "hit_id" in hit and "rank" in hit and "messages" in hit
    assert "message_id" not in hit and "similarity" not in hit
    message = hit["messages"][0]
    assert {"id", "conversation_id", "role", "text", "content", "external_id",
            "occurred_at", "ingested_at", "sha256", "harness", "external_conversation_id",
            "route"} <= set(message)
    assert len(message["sha256"]) == 64

    stored = next(
        b for b in blocks if isinstance(b, dict) and "harness" in b and "external_conversation_id" in b
    )
    assert len(stored["sha256"]) == 64

    context = next(b for b in blocks if isinstance(b, dict) and "context" in b)
    assert set(context["context"]) == {"version", "identity", "instructions", "memory", "emotion"}
    assert set(context["context"]["identity"]) == {"configured", "text", "revision"}
    assert set(context["context"]["memory"]) == {"evergreen", "session"}
    assert set(context["context"]["emotion"]) == {"values", "description", "preface", "fingerprints"}


def test_readme_export_commands_roundtrip(tmp_path):
    import subprocess

    from companion_gateway import emotions
    from companion_gateway import prompts as prompts_module
    from companion_gateway.config import AppConfig, PromptsConfig
    from companion_gateway.service import CompanionService

    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    shell_blocks = re.findall(r"```sh\n(.*?)```", readme, re.DOTALL)
    commands = []
    for block in shell_blocks:
        commands.extend(re.findall(r'python -c "(.*?)"', block, re.DOTALL))
    prompts_cmd = next(c for c in commands if "companion_gateway.prompts" in c)
    emotions_cmd = next(c for c in commands if "companion_gateway.emotions" in c)

    for command in (emotions_cmd, prompts_cmd):
        result = subprocess.run(
            [sys.executable, "-c", command], cwd=tmp_path, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr

    prompts_file = tmp_path / "prompts.json"
    emotions_file = tmp_path / "emotions.json"
    assert prompts_file.exists() and emotions_file.exists()

    # The exported prompts overlay loads (metadata stripped) and emotions snapshot validates.
    prompts_module.load_prompts(prompts_file)
    emotions.load_emotions(emotions_file)

    # Use the exported prompts file via config with a replacement sentinel.
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


async def test_endpoint_responses_comply_with_declared_shapes(config, monkeypatch):
    monkeypatch.setattr(api.asyncio, "to_thread", _run_inline)
    app = api.create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ingest = await client.post(
            "/state/v1/messages",
            json={
                "harness": "api",
                "conversation_id": "docs",
                "role": "user",
                "content": "Remember the amber window.",
                "external_id": "docs-example",
            },
        )
        assert ingest.status_code == 200
        body = ingest.json()
        assert {"id", "duplicate", "conversation_id", "sha256", "affect"} <= set(body)
        assert isinstance(body["sha256"], str) and len(body["sha256"]) == 64

        message_id = body["id"]
        stored = await client.get(f"/state/v1/messages/{message_id}")
        assert stored.status_code == 200
        row = stored.json()
        assert {"id", "conversation_id", "role", "text", "content", "external_id",
                "occurred_at", "ingested_at", "sha256", "harness",
                "external_conversation_id", "route"} <= set(row)
        assert len(row["sha256"]) == 64

        missing = await client.get("/state/v1/messages/999999")
        assert missing.status_code == 404
        assert isinstance(missing.json()["detail"], str)

        index = await client.get("/state/v1/memory/index")
        assert index.status_code == 200
        assert {"mode", "enabled", "configured", "chunker_key", "embedding_key", "messages",
                "chunked_messages", "chunks", "embedded_chunks", "dimensions", "cooling_down",
                "last_error"} <= set(index.json())

        search = await client.post(
            "/state/v1/memory/search", json={"query": "amber window", "limit": 2}
        )
        assert search.status_code == 200
        for hit in search.json()["results"]:
            assert {"hit_id", "rank", "messages"} <= set(hit)

        context = await client.post(
            "/state/v1/context",
            json={"harness": "api", "conversation_id": "docs", "query": "amber window"},
        )
        assert context.status_code == 200
        ctx = context.json()["context"]
        assert set(ctx) == {"version", "identity", "instructions", "memory", "emotion"}
        assert set(ctx["identity"]) == {"configured", "text", "revision"}
        assert set(ctx["memory"]) == {"evergreen", "session"}
        assert set(ctx["emotion"]) == {"values", "description", "preface", "fingerprints"}

        affect = await client.get("/state/v1/affect")
        assert affect.status_code == 200
        assert {"base", "mood", "last_updated_at", "last_user_message_at",
                "last_proactive_sent_at", "unanswered_proactive"} <= set(affect.json())

        bad_label = await client.post(
            f"/state/v1/messages/{message_id}/affect", json={"label": "not-a-real-label"}
        )
        assert bad_label.status_code == 422
        assert isinstance(bad_label.json()["detail"], str)

        duplicate = await client.post(
            "/state/v1/evergreen/facts",
            json={"key": "docs.duplicate", "text": "First version."},
        )
        assert duplicate.status_code == 200
        duplicate_again = await client.post(
            "/state/v1/evergreen/facts",
            json={"key": "docs.duplicate", "text": "Second version."},
        )
        assert duplicate_again.status_code == 409
        assert isinstance(duplicate_again.json()["detail"], str)


async def test_openapi_documents_error_and_success_responses(config, monkeypatch):
    monkeypatch.setattr(api.asyncio, "to_thread", _run_inline)
    app = api.create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        openapi = (await client.get("/openapi.json")).json()
    paths = openapi["paths"]

    revise = paths["/state/v1/evergreen/facts/{fact_id}/revisions"]["post"]["responses"]
    assert {"401", "404", "409", "422"} <= set(revise)

    context = paths["/state/v1/context"]["post"]["responses"]
    assert {"401", "422"} <= set(context)

    message = paths["/state/v1/messages/{message_id}"]["get"]["responses"]
    assert {"200", "404"} <= set(message)
    assert "MessageResponse" in openapi["components"]["schemas"]

    index = paths["/state/v1/memory/index"]["get"]["responses"]
    assert "200" in index
    assert "MemoryIndexStatus" in openapi["components"]["schemas"]

    error_schema = openapi["components"]["schemas"]["ErrorDetail"]
    detail = error_schema["properties"]["detail"]
    assert "anyOf" in detail
    detail_types = {item.get("type") for item in detail["anyOf"]}
    assert "string" in detail_types and "array" in detail_types


async def test_context_validation_and_budget_errors_match_error_schema(config, monkeypatch, tmp_path):
    from companion_gateway.config import (
        AppConfig,
        IdentityPromptConfig,
        MemoryConfig,
    )

    monkeypatch.setattr(api.asyncio, "to_thread", _run_inline)
    app = api.create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        invalid = await client.post("/state/v1/context", json={"query": 123})
        assert invalid.status_code == 422
        detail = invalid.json()["detail"]
        assert isinstance(detail, list)
        assert detail
        for item in detail:
            assert isinstance(item, dict)
            assert {"loc", "msg", "type"} <= set(item)

    identity_path = tmp_path / "identity.md"
    identity_path.write_text("IDENTITY_SENTINEL text", encoding="utf-8")
    small = MemoryConfig(
        recent_messages=2, search_hits=4, context_messages=1, injection_max_chars=64
    )
    cfg = AppConfig(
        data_dir=tmp_path / "data",
        timezone="Asia/Taipei",
        memory=small,
        identity_prompt=IdentityPromptConfig(path=str(identity_path)),
    )
    app2 = api.create_app(cfg)
    transport2 = httpx.ASGITransport(app=app2)
    async with httpx.AsyncClient(transport=transport2, base_url="http://test") as client2:
        budget = await client2.post("/state/v1/context", json={"query": ""})
        assert budget.status_code == 422
        assert isinstance(budget.json()["detail"], str)


def test_cli_migrate_config_smoke(tmp_path, monkeypatch):
    from companion_gateway import cli as cli_module

    source = tmp_path / "c.yaml"
    source.write_text("host: example-host\n", encoding="utf-8")
    destination = tmp_path / "c.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["companion-gateway", "migrate-config", str(source), str(destination)],
    )
    cli_module.main()
    assert destination.exists()
    assert json.loads(destination.read_text(encoding="utf-8"))["host"] == "example-host"