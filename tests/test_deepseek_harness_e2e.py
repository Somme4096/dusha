from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from dusha import api

_INTEGRATIONS = Path(__file__).resolve().parents[1] / "integrations"
if str(_INTEGRATIONS) not in sys.path:
    sys.path.insert(0, str(_INTEGRATIONS))

from deepseek_harness_dusha import (  # noqa: E402
    DeepSeekHarnessAdapter,
    DeepSeekHarnessConfig,
    DushaClient,
    DushaError,
    external_id,
)


def _settings(**overrides) -> DeepSeekHarnessConfig:
    values = {"gateway_url": "http://test", "request_timeout_seconds": 5}
    values.update(overrides)
    return DeepSeekHarnessConfig(**values)


@asynccontextmanager
async def _running_adapter(app, settings: DeepSeekHarnessConfig):
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        adapter = DeepSeekHarnessAdapter(settings, client=DushaClient(settings, transport=transport))
        try:
            yield adapter
        finally:
            await adapter.aclose()


class _BrokenContextClient(DushaClient):
    async def build_context(self, **kwargs):
        raise DushaError("context exploded")


def _texts(search: dict) -> list[str]:
    return [message["text"] for hit in search["results"] for message in hit["messages"]]


async def test_adapter_turn_and_all_seven_tools_roundtrip(config):
    app = api.create_app(config)
    async with _running_adapter(app, _settings()) as adapter:
        injection = await adapter.handle_turn("I love green tea", "conv-success")
        assert "<companion_state>" in injection

        await adapter.archive_reply("conv-success", "Noted, you love green tea.")

        search = json.loads(await adapter.memory_search("green tea"))
        assert search["ok"] is True
        texts = _texts(search)
        assert any("I love green tea" in text for text in texts)
        assert any("Noted, you love green tea." in text for text in texts)

        built = json.loads(await adapter.context_build("green tea", "conv-success"))
        assert built["ok"] is True
        assert "<companion_state>" in built["injection"]

        affect = json.loads(await adapter.affect_status())
        assert affect["ok"] is True
        assert "mood" in affect

        remembered = json.loads(
            await adapter.evergreen_remember(
                "e2e.tea",
                "Birch loves green tea",
                priority=70,
                reason="seed",
                review_after="2030-01-01T00:00:00Z",
                expires_at="2030-01-01T00:00:00Z",
            )
        )
        assert remembered["ok"] is True
        fact = remembered["fact"]
        assert fact["review_after"] is not None
        assert fact["expires_at"] is not None

        listed = json.loads(await adapter.evergreen_list())
        assert listed["ok"] is True
        assert any(item["fact_id"] == fact["fact_id"] for item in listed["facts"])

        revised = json.loads(
            await adapter.evergreen_revise(fact["fact_id"], fact["revision"], "Birch loves oolong tea")
        )
        assert revised["ok"] is True
        assert revised["fact"]["review_after"] is not None
        assert revised["fact"]["expires_at"] is not None
        new_revision = revised["fact"]["revision"]

        cleared = json.loads(
            await adapter.evergreen_revise(
                fact["fact_id"], new_revision, "Birch loves oolong tea", expires_at="clear"
            )
        )
        assert cleared["ok"] is True
        assert cleared["fact"]["expires_at"] is None

        forgotten = json.loads(
            await adapter.evergreen_forget(fact["fact_id"], cleared["fact"]["revision"], "test cleanup")
        )
        assert forgotten["ok"] is True


async def test_adapter_client_get_memory(config):
    app = api.create_app(config)
    async with _running_adapter(app, _settings()) as adapter:
        await adapter.archive_reply("conv-memory", "the archive keeps this line")
        search = json.loads(await adapter.memory_search("archive keeps"))
        message_id = search["results"][0]["messages"][0]["id"]
        record = await adapter.client.get_memory(memory_id=message_id, context_messages=1)
        assert any("archive keeps this line" in item["text"] for item in record["messages"])


async def test_adapter_token_header_success_and_401(config, monkeypatch):
    config.api_token_env = "E2E_DEEPSEEK_TOKEN"
    monkeypatch.setenv("E2E_DEEPSEEK_TOKEN", "right-secret")
    async with _running_adapter(api.create_app(config), _settings(api_token="wrong-secret")) as adapter:
        denied = json.loads(await adapter.affect_status())
        assert denied["ok"] is False
        assert denied["status"] == 401
    async with _running_adapter(api.create_app(config), _settings(api_token="right-secret")) as adapter:
        allowed = json.loads(await adapter.affect_status())
        assert allowed["ok"] is True


async def test_adapter_invalid_body_returns_422(config):
    app = api.create_app(config)
    async with _running_adapter(app, _settings()) as adapter:
        incremented = json.loads(await adapter.evergreen_revise("missing-fact", 0, "new text"))
        assert incremented["ok"] is False
        assert incremented["status"] == 422
        with pytest.raises(DushaError) as captured:
            await adapter.client.list_facts(limit=0)
        assert captured.value.status == 422


async def test_adapter_duplicate_external_id(config):
    app = api.create_app(config)
    async with _running_adapter(app, _settings()) as adapter:
        first = await adapter.client.ingest_message(
            harness="deepseek-harness",
            conversation_id="conv-duplicate",
            role="user",
            content="first copy",
            external_id="deepseek-harness:dup-1",
        )
        second = await adapter.client.ingest_message(
            harness="deepseek-harness",
            conversation_id="conv-duplicate",
            role="user",
            content="second copy",
            external_id="deepseek-harness:dup-1",
        )
        assert first["duplicate"] is False
        assert second["duplicate"] is True
        assert second["id"] == first["id"]


async def test_adapter_context_failure_does_not_break_turn(config):
    app = api.create_app(config)
    settings = _settings()
    async with app.router.lifespan_context(app):
        client = _BrokenContextClient(settings, transport=httpx.ASGITransport(app=app))
        adapter = DeepSeekHarnessAdapter(settings, client=client)
        try:
            injection = await adapter.handle_turn("hello from a broken context", "conv-broken")
            assert injection == ""
            search = json.loads(await adapter.memory_search("broken context"))
            assert any("hello from a broken context" in text for text in _texts(search))
        finally:
            await adapter.aclose()


async def test_adapter_messages_survive_restart(config):
    settings = _settings()
    async with _running_adapter(api.create_app(config), settings) as adapter:
        await adapter.handle_turn("remember the lighthouse", "conv-restart")

    async with _running_adapter(api.create_app(config), settings) as adapter:
        search = json.loads(await adapter.memory_search("lighthouse"))
        assert any("remember the lighthouse" in text for text in _texts(search))


async def test_adapter_handle_turn_skips_when_disabled_or_already_injected(config):
    async with _running_adapter(api.create_app(config), _settings(auto_inject=False)) as disabled:
        assert await disabled.handle_turn("hello", "conv-skip") == ""
        empty = json.loads(await disabled.memory_search("hello"))
        assert _texts(empty) == []

    async with _running_adapter(api.create_app(config), _settings()) as adapter:
        marker = "<companion_state>{}</companion_state>"
        assert await adapter.handle_turn("hello", "conv-skip", existing_context=marker) == ""
        assert await adapter.handle_turn("   ", "conv-skip") == ""


async def test_adapter_tool_validation_returns_ok_false(config):
    app = api.create_app(config)
    async with _running_adapter(app, _settings()) as adapter:
        assert json.loads(await adapter.memory_search("   "))["ok"] is False
        assert json.loads(await adapter.evergreen_remember("", "text"))["ok"] is False
        assert json.loads(await adapter.evergreen_revise("f", 1, ""))["ok"] is False
        assert json.loads(await adapter.evergreen_forget("f", 1, "  "))["ok"] is False
        assert json.loads(await adapter.context_build("", "conv"))["ok"] is False


def test_external_id_is_stable_and_prefixed():
    assert external_id("conv", "", "hello") == external_id("conv", "", "hello")
    assert external_id("conv", "", "hello") == "deepseek-harness:131ea8bd"
    assert external_id("conv", "", "hello") != external_id("conv", "", "world")
    assert external_id("conv", "msg-1", "hello") != external_id("conv", "", "hello")
    assert external_id("other", "", "hello").startswith("deepseek-harness:")


def test_config_clamps_timeout():
    assert DeepSeekHarnessConfig().request_timeout_seconds == 60.0
    assert DeepSeekHarnessConfig(request_timeout_seconds=0).request_timeout_seconds == 1.0
    assert DeepSeekHarnessConfig(gateway_url="http://host/").gateway_url == "http://host"
