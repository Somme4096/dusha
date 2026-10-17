from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from dusha import api
from dusha.config import AffectConfig, AppConfig, ExternalPluginConfig, PluginEntry, load_config
from dusha.external import PluginError, load

PROBE_SOURCE = '''\
import json
import pathlib

from dusha.hooks import Block


def _seen(config):
    path = pathlib.Path(config["marker"])
    return json.loads(path.read_text()) if path.exists() else {}


def _note(config, key, value):
    if "marker" not in config:
        return
    seen = _seen(config)
    seen.setdefault(key, []).append(value)
    pathlib.Path(config["marker"]).write_text(json.dumps(seen))


class Vault:
    def remember(self, text):
        return {"saved": text}


def apply(ctx, config):
    if config.get("start_fail"):
        raise ValueError("configured start failure")
    _note(config, "start", {"name": ctx.name, "data_dir": str(ctx.data_dir)})
    if config.get("provide"):
        ctx.provide("vault", Vault())
    if config.get("route"):
        @ctx.api.get("/state/v1/probe/ping")
        def ping():
            return {"ok": True, "name": ctx.name}
    if config.get("command"):
        parser = ctx.cli.command("probe", "Probe")
        parser.set_defaults(run=lambda args: {"name": ctx.name})
    ctx.on("message", lambda message: on_message(ctx, message, config))
    ctx.on("context", lambda request: on_context(ctx, request, config))
    ctx.on("decide", lambda request: decide(request, config))
    if config.get("listen"):
        ctx.on("probe_event", lambda payload: on_probe_event(payload, config))
    ctx.on_dispose(lambda: _note(config, "close", True))


def on_probe_event(payload, config):
    _note(config, "events", payload)
    return {"seen": payload["text"]}


def on_message(ctx, message, config):
    if config.get("error_on_message"):
        ctx.error = config["error_on_message"]
    if config.get("emit"):
        ctx.emit("probe_event", {"text": message["text"]})
    _note(config, "messages", {
        "id": message["id"], "role": message["role"], "text": message["text"],
        "harness": message["harness"], "conversation": message["external_conversation_id"],
    })


def on_context(ctx, request, config):
    _note(config, "context", {
        "query": request.query, "harness": request.harness,
        "conversation_id": request.conversation_id,
    })
    records = [{"source": "probe", "text": "PROBE_RECORD " + request.query}]
    if config.get("recall"):
        latest = ctx.messages.recent(limit=1)
        records.append({"source": "messages", "text": "RECALLED " + latest[-1]["text"]})
        try:
            ctx.memo.list()
        except AttributeError as error:
            records.append({"source": "denied", "text": str(error)})
    if config.get("consume_vault"):
        saved = ctx.vault.remember(request.query)
        records.append({"source": "vault", "text": json.dumps(saved)})
    if config.get("collect"):
        results = ctx.collect("probe_event", {"text": request.query})
        records.append({"source": "collect", "text": json.dumps(results)})
    data = {"text": config.get("context_text", "PROBE_CONTEXT"), "records": records}
    return Block(ctx.name, "", data, tuple(records))


def decide(request, config):
    mode = config.get("mode", "choose")
    _note(config, "decide", {
        "message": request.message, "instruction": request.instruction,
        "emotions": sorted(request.emotions), "state": request.state["base"],
    })
    if mode == "abstain":
        return None
    if mode == "unknown":
        return {"emotion": "missing"}
    if mode == "deltas":
        return {"deltas": config["deltas"]}
    if mode == "both":
        return {"emotion": choose(request.emotions), "deltas": config["deltas"]}
    if mode == "crash":
        raise RuntimeError("boom")
    return {"emotion": choose(request.emotions)}


def choose(emotions):
    return "fear" if "fear" in emotions else None
'''

MANIFEST = (
    '[project]\nname = "{name}"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n\n'
    '[tool.dusha]\nentry = "probe"\ninject = {inject}\n'
)


@pytest.fixture(scope="module")
def plugins_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("plugins")
    for name, inject in [
        ("probe", '["messages", "api", "cli"]'),
        ("probe_two", '["messages", "api", "cli"]'),
        ("needs_affect", '["affect"]'),
        ("vault_provider", "[]"),
        ("vault_consumer", '["vault"]'),
    ]:
        suite = root / name
        suite.mkdir(parents=True)
        (suite / "probe.py").write_text(PROBE_SOURCE, encoding="utf-8")
        (suite / "pyproject.toml").write_text(MANIFEST.format(name=name, inject=inject), encoding="utf-8")
    return root


@asynccontextmanager
async def _client(config: AppConfig):
    app = api.create_app(config)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def _config(tmp_path: Path, plugins_dir: Path, modules: dict[str, Any] | None = None, **options) -> AppConfig:
    return AppConfig(
        data_dir=tmp_path / "data",
        plugins_dir=str(plugins_dir),
        plugins={
            "affect": AffectConfig(increment=0.25),
            **(modules if modules is not None else {"probe": ExternalPluginConfig(options=options)}),
        },
    )


def _marker(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


async def _post(
    client: httpx.AsyncClient, content: str = "hello", role: str = "user", conversation: str = "one"
) -> dict:
    response = await client.post(
        "/state/v1/messages",
        json={"harness": "e2e", "conversation_id": conversation, "role": role, "content": content},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _base(client: httpx.AsyncClient) -> dict:
    response = await client.get("/state/v1/affect")
    assert response.status_code == 200, response.text
    return response.json()["base"]


async def _context(client: httpx.AsyncClient, query: str = "lantern", conversation: str = "one") -> dict:
    response = await client.post(
        "/state/v1/context", json={"harness": "e2e", "conversation_id": conversation, "query": query}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _post_expecting_no_decision(config: AppConfig) -> None:
    async with _client(config) as client:
        before = await _base(client)
        stored = await _post(client)
        after = await _base(client)
    assert stored.get("affect") is None
    assert after == pytest.approx(before, abs=1e-3)


def _cli(config: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    return subprocess.run(
        [sys.executable, "-m", "dusha.cli", "--config", str(config), *args],
        capture_output=True, text=True, env=env, check=False,
    )


def _write_suite(root: Path, name: str, source: str, manifest: str | None) -> None:
    suite = root / name
    suite.mkdir(parents=True)
    (suite / "probe.py").write_text(source, encoding="utf-8")
    if manifest is not None:
        (suite / "pyproject.toml").write_text(manifest, encoding="utf-8")


async def test_http_external_plugin_sees_every_event_with_the_documented_arguments(
    tmp_path, plugins_dir
):
    marker = tmp_path / "seen.json"
    async with _client(_config(tmp_path, plugins_dir, marker=str(marker))) as client:
        before = await _base(client)
        stored = await _post(client, "hello")
        await _post(client, "sure thing", role="assistant")
        await _post(client, "tool output", role="tool", conversation="two")
        context = await _context(client, "lantern")
        health = await client.get("/health")
        status = await client.get("/state/v1/plugins")
    assert stored["affect"]["emotion"] == "fear"
    assert stored["affect"]["increment"] == pytest.approx(0.25)
    assert stored["affect"]["state"]["base"]["fear"] == pytest.approx(before["fear"] + 0.25, abs=1e-3)

    block = context["context"]["blocks"]["probe"]
    assert block["text"] == "PROBE_CONTEXT"
    assert block["records"] == [{"source": "probe", "text": "PROBE_RECORD lantern"}]
    assert {"source": "probe", "text": "PROBE_RECORD lantern"} in context["context"]["session"]
    assert "memory_id" not in json.dumps(block)

    assert "probe" in health.json()["plugins"]
    assert status.json()["probe"] == {
        "state": "active",
        "error": "",
        "inject": ["messages", "api", "cli"],
        "provides": [],
        "events": ["context", "decide", "message"],
    }

    seen = _marker(marker)
    [start] = seen["start"]
    assert start["name"] == "probe"
    assert start["data_dir"] == str(tmp_path / "data" / "plugins" / "probe")
    assert [(item["role"], item["text"], item["conversation"]) for item in seen["messages"]] == [
        ("user", "hello", "one"), ("assistant", "sure thing", "one"), ("tool", "tool output", "two"),
    ]
    assert [item["id"] for item in seen["messages"]] == sorted(item["id"] for item in seen["messages"])
    assert seen["context"] == [{"query": "lantern", "harness": "e2e", "conversation_id": "one"}]
    [decision] = seen["decide"]
    assert decision["message"] == "hello" and decision["instruction"].strip()
    assert set(decision["emotions"]) == set(before) and set(decision["state"]) == set(before)
    assert seen["close"] == [True]


async def test_http_external_plugin_registers_api_routes_and_cli_commands(tmp_path, plugins_dir):
    options = {"route": True, "command": True}
    async with _client(_config(tmp_path, plugins_dir, **options)) as client:
        response = await client.get("/state/v1/probe/ping")
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "name": "probe"}

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({
            "data_dir": str(tmp_path / "data"),
            "plugins_dir": os.path.relpath(plugins_dir, tmp_path),
            "plugins": {"probe": {"options": options}},
        }),
        encoding="utf-8",
    )
    result = _cli(config_path, "probe")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"name": "probe"}


async def test_http_external_plugin_calls_back_into_injected_services_only(tmp_path, plugins_dir):
    async with _client(_config(tmp_path, plugins_dir, recall=True)) as client:
        await _post(client, "the orchid is in the cabinet")
        context = await _context(client, "orchid")
    records = context["context"]["blocks"]["probe"]["records"]
    assert {"source": "messages", "text": "RECALLED the orchid is in the cabinet"} in records
    [denied] = [item for item in records if item["source"] == "denied"]
    assert denied["text"] == "probe must inject 'memo' to use it"


async def test_http_external_plugin_with_a_missing_dependency_stays_inactive(tmp_path, plugins_dir):
    marker = tmp_path / "seen.json"
    needs_affect = ExternalPluginConfig(options={"marker": str(marker)})
    modules = {"affect": False, "proactive": False, "needs_affect": needs_affect}
    async with _client(_config(tmp_path, plugins_dir, modules)) as client:
        await _post(client)
        status = await client.get("/state/v1/plugins")
        closed = await client.get("/state/v1/needs_affect/anything")
    report = status.json()["needs_affect"]
    assert report["state"] == "inactive" and report["error"] == "missing service(s): affect"
    assert status.json()["affect"]["state"] == "disabled"
    assert closed.status_code == 503
    assert closed.json()["detail"] == "the needs_affect plugin is inactive, missing service(s): affect"
    assert not marker.exists()


async def test_http_external_plugin_provides_and_another_plugin_consumes_the_service(
    tmp_path, plugins_dir
):
    provider_marker = tmp_path / "provider.json"
    consumer_marker = tmp_path / "consumer.json"
    modules = {
        "vault_provider": ExternalPluginConfig(
            options={"provide": True, "marker": str(provider_marker)}
        ),
        "vault_consumer": ExternalPluginConfig(
            options={"consume_vault": True, "marker": str(consumer_marker)}
        ),
    }
    async with _client(_config(tmp_path, plugins_dir, modules)) as client:
        await _post(client, "hello")
        context = await _context(client, "lantern")
        status = await client.get("/state/v1/plugins")
    records = context["context"]["blocks"]["vault_consumer"]["records"]
    assert {"source": "vault", "text": json.dumps({"saved": "lantern"})} in records
    assert status.json()["vault_provider"]["provides"] == ["vault"]
    consumer = status.json()["vault_consumer"]
    assert consumer["inject"] == ["vault"] and consumer["state"] == "active"


async def test_http_external_plugin_custom_events_cross_plugins(tmp_path, plugins_dir):
    emitter_marker = tmp_path / "emitter.json"
    listener_marker = tmp_path / "listener.json"
    modules = {
        "probe": ExternalPluginConfig(
            options={"marker": str(emitter_marker), "emit": True, "collect": True, "listen": True}
        ),
        "probe_two": ExternalPluginConfig(options={"marker": str(listener_marker), "listen": True}),
    }
    async with _client(_config(tmp_path, plugins_dir, modules)) as client:
        await _post(client, "hello")
        context = await _context(client, "lantern")
    assert {"text": "hello"} in _marker(listener_marker)["events"]
    records = context["context"]["blocks"]["probe"]["records"]
    [collected] = [item for item in records if item["source"] == "collect"]
    results = json.loads(collected["text"])
    assert results["probe"] == {"seen": "lantern"}
    assert results["probe_two"] == {"seen": "lantern"}


async def test_http_external_plugin_reports_its_name_and_error(tmp_path, plugins_dir):
    marker = tmp_path / "seen.json"
    config = _config(tmp_path, plugins_dir, marker=str(marker), error_on_message="configured failure")
    async with _client(config) as client:
        await _post(client, "hello")
        status = await client.get("/state/v1/plugins")
    [start] = _marker(marker)["start"]
    assert start["name"] == "probe"
    report = status.json()["probe"]
    assert report["state"] == "failed" and report["error"] == "configured failure"


def test_create_app_fails_when_an_external_plugin_apply_raises(tmp_path, plugins_dir):
    with pytest.raises(ValueError):
        api.create_app(_config(tmp_path, plugins_dir, start_fail=True))


@pytest.mark.parametrize(
    ("name", "source", "manifest", "message"),
    [
        ("unmarked", "def apply(ctx, config):\n    pass\n", None, r"missing or escaping pyproject.toml"),
        (
            "unmarked",
            "def apply(ctx, config):\n    pass\n",
            '[project]\nname = "x"\nversion = "0.1.0"\n',
            r"missing \[tool.dusha\]",
        ),
        (
            "unmarked",
            "def apply(ctx, config):\n    pass\n",
            '[tool.dusha]\nentry = "probe"\nbogus = 1\n',
            "unknown field",
        ),
        (
            "unmarked",
            "VALUE = 1\n",
            '[tool.dusha]\nentry = "probe"\n',
            "does not define apply",
        ),
    ],
)
def test_manifest_validation_rejects_invalid_suites(tmp_path, name, source, manifest, message):
    plugins = tmp_path / "plugins"
    _write_suite(plugins, name, source, manifest)
    with pytest.raises(PluginError, match=message):
        load(name, plugins)


async def test_http_disabled_external_plugin_leaves_an_unavailable_placeholder(tmp_path, plugins_dir):
    modules = {"probe": PluginEntry(ExternalPluginConfig(), disabled=True)}
    async with _client(_config(tmp_path, plugins_dir, modules)) as client:
        status = await client.get("/state/v1/plugins")
        closed = await client.get("/state/v1/probe/ping")
    assert status.json()["probe"]["state"] == "disabled"
    assert closed.status_code == 503
    assert closed.json()["detail"] == "the probe plugin is disabled"


def test_config_rejects_bad_external_plugin_names_and_fields(tmp_path):
    with pytest.raises(ValueError, match="plain directory name"):
        AppConfig(plugins={"../outside": ExternalPluginConfig()})
    for raw, message in [
        ({"plugins": {"probe": {"timeout_seconds": 1}}}, r"unknown field\(s\) under plugins.probe"),
        ({"plugins": {"probe": {"bogus": 1}}}, r"unknown field\(s\) under plugins.probe"),
        ({"plugins": {"probe": 1}}, "plugins.probe must be an object"),
        ({"plugins": {"probe": {"disabled": "yes"}}}, "plugins.probe.disabled must be bool"),
        ({"plugins": {"../outside": {}}}, "plain directory name"),
    ]:
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"data_dir": str(tmp_path / "data"), **raw}), encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            load_config(path)


@pytest.mark.parametrize(
    ("mode", "logged"),
    [
        ("abstain", None),
        ("unknown", "decision returned invalid emotion: 'missing'"),
        ("crash", "probe decide failed"),
    ],
)
async def test_http_decision_failures_leave_affect_unchanged(tmp_path, plugins_dir, caplog, mode, logged):
    caplog.set_level(logging.WARNING, logger="dusha")
    await _post_expecting_no_decision(_config(tmp_path, plugins_dir, mode=mode))
    if logged is None:
        assert "probe" not in caplog.text
    else:
        assert logged in caplog.text


async def test_http_deltas_apply_after_the_decision_sees_the_earlier_state(tmp_path, plugins_dir, caplog):
    caplog.set_level(logging.WARNING, logger="dusha")
    marker = tmp_path / "seen.json"
    deltas = {"elation": 0.3, "bogus": 0.1}
    async with _client(
        _config(tmp_path, plugins_dir, marker=str(marker), mode="deltas", deltas=deltas)
    ) as client:
        before = await _base(client)
        stored = await _post(client, "I love lanterns")
        after = await _base(client)
    assert stored.get("affect") is None
    assert after["elation"] > before["elation"]
    assert "decision deltas name unknown emotion dimension(s): ['bogus']" in caplog.text

    async with _client(
        _config(tmp_path, plugins_dir, marker=str(marker), mode="both", deltas=deltas)
    ) as client:
        before = await _base(client)
        stored = await _post(client, "I love lanterns again")
    assert stored["affect"]["emotion"] == "fear"
    assert stored["affect"]["state"]["base"]["elation"] > before["elation"]
    assert _marker(marker)["decide"][-1]["state"]["elation"] == pytest.approx(before["elation"], abs=1e-3)


async def test_http_two_external_plugins_run_in_order_first_emotion_wins_and_deltas_merge(
    tmp_path, plugins_dir
):
    modules = {
        "probe_two": ExternalPluginConfig(options={"mode": "deltas", "deltas": {"elation": 0.3}}),
        "probe": ExternalPluginConfig(options={"mode": "choose"}),
    }
    async with _client(_config(tmp_path, plugins_dir, modules)) as client:
        before = await _base(client)
        stored = await _post(client)
        context = await _context(client)
        health = await client.get("/health")
    assert stored["affect"]["emotion"] == "fear"
    assert stored["affect"]["state"]["base"]["elation"] > before["elation"]
    assert list(context["context"]["blocks"]) == ["memory", "affect", "probe_two", "probe"]
    assert health.json()["plugins"][-2:] == ["probe_two", "probe"]
