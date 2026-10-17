from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from dusha import api
from dusha.config import AffectConfig, AppConfig, ExternalPluginConfig, load_config

HELPER_SOURCE = '''\
def choose(emotions):
    return "fear" if "fear" in emotions else None
'''

PROBE_SOURCE = '''\
import json, os, pathlib, subprocess, sys, time

from helper import choose

CHILD = "import pathlib, sys, time\\n" \\
    "pathlib.Path(sys.argv[1]).write_text('started')\\n" \\
    "time.sleep(1.5)\\n" \\
    "pathlib.Path(sys.argv[2]).write_text('alive')\\n"


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
    _note(config, "start", {
        "name": ctx.name,
        "data_dir": str(ctx.data_dir),
        "cwd": os.getcwd(),
        "python": sys.executable,
        "env": {
            name: os.getenv(name)
            for name in ("PYTHONPATH", "VIRTUAL_ENV", "GATEWAY_SECRET", "SHARED_VALUE")
        },
    })
    if config.get("provide"):
        ctx.provide("vault", Vault())
    ctx.on("message", lambda message: on_message(ctx, message, config))
    ctx.on("context", lambda request: on_context(ctx, request, config))
    ctx.on("decide", lambda request: decide(request, config))
    if config.get("listen"):
        ctx.on("probe_event", lambda payload: on_probe_event(payload, config))
    ctx.interval(1, lambda: _note(config, "tick", time.time()))
    ctx.on_dispose(lambda: _note(config, "close", True))


def on_probe_event(payload, config):
    _note(config, "events", payload)
    return {"seen": payload["text"]}


def on_message(ctx, message, config):
    if message["text"] == config.get("fail_on"):
        raise RuntimeError("configured delivery failure")
    if config.get("error_on_message"):
        ctx.error = config["error_on_message"]
    if config.get("emit"):
        ctx.emit("probe_event", {"text": message["text"]})
    _note(config, "messages", {
        "id": message["id"], "role": message["role"], "text": message["text"],
        "harness": message["harness"], "conversation": message["external_conversation_id"],
    })


def on_context(ctx, request, config):
    if config.get("context_fail"):
        raise RuntimeError("configured context failure")
    _note(config, "context", {
        "query": request.query, "harness": request.harness,
        "conversation_id": request.conversation_id, "max_chars": config["context_max_chars"],
    })
    records = [{"source": "probe", "text": "PROBE_RECORD " + request.query}]
    if config.get("recall"):
        latest = ctx.messages.recent(limit=1)
        records.append({"source": "messages", "text": "RECALLED " + latest[-1]["text"]})
        try:
            ctx.memo.list()
        except PermissionError as error:
            records.append({"source": "denied", "text": str(error)})
    if config.get("consume_vault"):
        saved = ctx.vault.remember(request.query)
        records.append({"source": "vault", "text": json.dumps(saved)})
    if config.get("collect"):
        results = ctx.collect("probe_event", {"text": request.query})
        records.append({"source": "collect", "text": json.dumps(results)})
    return {"text": config.get("context_text", "PROBE_CONTEXT"), "records": records}


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
    if mode == "die":
        os.kill(os.getpid(), 9)
    if mode == "sleep_once" and len(_seen(config).get("decide", [])) == 1:
        time.sleep(config["seconds"])
    if mode == "stdout_flood":
        print("x" * 2000000)
    if mode == "descendant":
        subprocess.Popen([sys.executable, "-c", CHILD, config["started"], config["alive"]])
        while not os.path.exists(config["started"]):
            time.sleep(0.01)
        return None
    return {"emotion": choose(request.emotions)}
'''

MANIFEST = (
    '[project]\nname = "probe"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n\n'
    '[tool.dusha]\nentry = "probe"\ninject = {inject}\n'
)


@pytest.fixture(scope="module")
def plugins_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("plugins-workspace")
    (root / "other").mkdir()
    (root / "pyproject.toml").write_text('[tool.uv.workspace]\nmembers = ["other"]\n', encoding="utf-8")
    suite = root / "plugins" / "probe"
    suite.mkdir(parents=True)
    (suite / "probe.py").write_text(PROBE_SOURCE, encoding="utf-8")
    (suite / "helper.py").write_text(HELPER_SOURCE, encoding="utf-8")
    (suite / "pyproject.toml").write_text(MANIFEST.format(inject='["messages"]'), encoding="utf-8")
    subprocess.run(["uv", "--no-config", "lock", "--directory", str(suite)], check=True)
    shutil.copytree(suite, root / "plugins" / "probe_two")
    shutil.copytree(suite, root / "plugins" / "needs_affect")
    (root / "plugins" / "needs_affect" / "pyproject.toml").write_text(
        MANIFEST.format(inject='["affect"]'), encoding="utf-8"
    )
    shutil.copytree(suite, root / "plugins" / "vault_provider")
    (root / "plugins" / "vault_provider" / "pyproject.toml").write_text(
        MANIFEST.format(inject="[]"), encoding="utf-8"
    )
    shutil.copytree(suite, root / "plugins" / "vault_consumer")
    (root / "plugins" / "vault_consumer" / "pyproject.toml").write_text(
        MANIFEST.format(inject='["vault"]'), encoding="utf-8"
    )
    return root / "plugins"


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


async def _post_expecting_no_decision(config: AppConfig) -> float:
    async with _client(config) as client:
        before = await _base(client)
        started = time.monotonic()
        stored = await _post(client)
        elapsed = time.monotonic() - started
        after = await _base(client)
    assert stored.get("affect") is None
    assert after == pytest.approx(before, abs=1e-3)
    return elapsed


def _cli(config: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    return subprocess.run(
        [sys.executable, "-m", "dusha.cli", "--config", str(config), *args],
        capture_output=True, text=True, env=env, check=False,
    )


async def test_http_external_plugin_sees_every_event_with_the_documented_arguments(
    tmp_path, plugins_dir, monkeypatch
):
    monkeypatch.setenv("PYTHONPATH", "gateway-path")
    monkeypatch.setenv("VIRTUAL_ENV", "gateway-venv")
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
    assert block["source"] == "probe" and block["text"] == "PROBE_CONTEXT"
    assert block["records"] == [{"source": "probe", "text": "PROBE_RECORD lantern"}]
    assert "<plugin_context>" in context["injection"] and "PROBE_RECORD lantern" in context["injection"]
    assert context["injection"].index("<affect>") < context["injection"].index("<plugin_context>")
    assert "memory_id" not in json.dumps(block)

    assert "probe" in health.json()["plugins"]
    assert status.json()["probe"] == {
        "state": "active",
        "error": "",
        "inject": ["messages"],
        "provides": [],
        "events": ["context", "decide", "message"],
    }

    seen = _marker(marker)
    [start] = seen["start"]
    assert start["data_dir"] == str(tmp_path / "data" / "plugins" / "probe")
    assert start["cwd"] == str(plugins_dir / "probe")
    assert start["python"].startswith(str(plugins_dir / "probe" / ".venv"))
    assert start["env"]["PYTHONPATH"] is None and start["env"]["VIRTUAL_ENV"] is None
    assert [(item["role"], item["text"], item["conversation"]) for item in seen["messages"]] == [
        ("user", "hello", "one"), ("assistant", "sure thing", "one"), ("tool", "tool output", "two"),
    ]
    assert [item["id"] for item in seen["messages"]] == sorted(item["id"] for item in seen["messages"])
    assert seen["context"] == [
        {"query": "lantern", "harness": "e2e", "conversation_id": "one", "max_chars": 3000}
    ]
    [decision] = seen["decide"]
    assert decision["message"] == "hello" and decision["instruction"].strip()
    assert set(decision["emotions"]) == set(before) and set(decision["state"]) == set(before)
    assert seen["close"] == [True]


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


async def test_http_external_plugin_provides_a_service(tmp_path, plugins_dir):
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


async def test_http_external_plugin_custom_events_cross_the_boundary(tmp_path, plugins_dir):
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


async def test_http_delivery_cursor_replays_a_failed_message_in_order(tmp_path, plugins_dir):
    marker = tmp_path / "seen.json"
    config = _config(tmp_path, plugins_dir, marker=str(marker), fail_on="second")
    async with _client(config) as client:
        await _post(client, "first")
        await _post(client, "second")
        await _post(client, "third")
    assert [item["text"] for item in _marker(marker)["messages"]] == ["first"]

    async with _client(_config(tmp_path, plugins_dir, marker=str(marker))) as client:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and len(_marker(marker).get("messages", [])) < 3:
            await asyncio.sleep(0.1)
        await _post(client, "fourth")
    seen = _marker(marker)
    assert [item["text"] for item in seen["messages"]] == ["first", "second", "third", "fourth"]
    assert seen["tick"]


async def test_http_context_block_respects_its_cap_and_falls_back_on_failure(tmp_path, plugins_dir):
    marker = tmp_path / "seen.json"
    capped = {"probe": ExternalPluginConfig(context_max_chars=500, options={"context_text": "x" * 4000})}
    async with _client(_config(tmp_path, plugins_dir, capped)) as client:
        await _post(client, "VAULT_RECENT_MARKER")
        fact = await client.post("/state/v1/evergreen/facts", json={"key": "vault.key", "text": "VAULT_FACT"})
        assert fact.status_code == 200
        context = await _context(client)
    block = context["context"]["blocks"]["probe"]
    assert len(block["text"]) == 500 and "records" not in block
    assert "VAULT_FACT" in context["injection"] and "VAULT_RECENT_MARKER" in context["injection"]

    failing = {"probe": ExternalPluginConfig(options={"context_fail": True})}
    async with _client(_config(tmp_path, plugins_dir, failing)) as client:
        context = await _context(client)
    assert "probe" not in context["context"]["blocks"]
    assert "VAULT_RECENT_MARKER" in context["injection"]

    silent = {"probe": ExternalPluginConfig(context_max_chars=0, options={"marker": str(marker)})}
    async with _client(_config(tmp_path, plugins_dir, silent)) as client:
        context = await _context(client)
    assert "probe" not in context["context"]["blocks"]
    assert "context" not in _marker(marker)


@pytest.mark.parametrize(
    ("mode", "logged"),
    [
        ("abstain", None),
        ("unknown", "decision returned invalid emotion: 'missing'"),
        ("crash", "probe decide failed: RuntimeError: boom"),
        ("stdout_flood", "probe decide failed: RuntimeError: plugin output limit exceeded"),
    ],
)
async def test_http_decision_failures_leave_affect_unchanged(tmp_path, plugins_dir, caplog, mode, logged):
    caplog.set_level(logging.WARNING, logger="dusha")
    await _post_expecting_no_decision(_config(tmp_path, plugins_dir, mode=mode))
    if logged is None:
        assert "probe" not in caplog.text
    else:
        assert logged in caplog.text
    if mode == "crash":
        assert "probe stderr: RuntimeError: boom" in caplog.text


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


async def test_http_timeout_stops_the_external_plugin_and_the_next_call_restarts_it(
    tmp_path, plugins_dir, caplog
):
    caplog.set_level(logging.WARNING, logger="dusha")
    marker = tmp_path / "seen.json"
    options = {"marker": str(marker), "mode": "sleep_once", "seconds": 30}
    modules = {"probe": ExternalPluginConfig(timeout_seconds=1.0, options=options)}
    async with _client(_config(tmp_path, plugins_dir, modules)) as client:
        started = time.monotonic()
        first = await _post(client, "slow")
        elapsed = time.monotonic() - started
        second = await _post(client, "quick")
        running = await client.get("/state/v1/plugins")
    assert first.get("affect") is None and elapsed < 10
    assert "probe decide failed: plugin timed out" in caplog.text
    assert "probe is not running, restarting" in caplog.text
    assert second["affect"]["emotion"] == "fear"
    assert running.json()["probe"]["state"] == "active"
    assert len(_marker(marker)["start"]) == 2


async def test_http_descendant_process_is_stopped_after_close(tmp_path, plugins_dir):
    started, alive = tmp_path / "started", tmp_path / "alive"
    config = _config(tmp_path, plugins_dir, mode="descendant", started=str(started), alive=str(alive))
    await _post_expecting_no_decision(config)
    assert started.exists()
    await asyncio.sleep(2.0)
    assert not alive.exists()


@pytest.mark.parametrize(
    ("passthrough", "expected"),
    [
        ([], {"GATEWAY_SECRET": None, "SHARED_VALUE": None}),
        (["SHARED_VALUE"], {"GATEWAY_SECRET": None, "SHARED_VALUE": "shared"}),
    ],
)
async def test_http_external_plugin_sees_only_the_passthrough_environment(
    tmp_path, plugins_dir, monkeypatch, passthrough, expected
):
    monkeypatch.setenv("GATEWAY_SECRET", "secret")
    monkeypatch.setenv("SHARED_VALUE", "shared")
    marker = tmp_path / "seen.json"
    modules = {"probe": ExternalPluginConfig(env_passthrough=passthrough, options={"marker": str(marker)})}
    async with _client(_config(tmp_path, plugins_dir, modules)) as client:
        stored = await _post(client)
    assert stored["affect"]["emotion"] == "fear"
    env = _marker(marker)["start"][0]["env"]
    assert {name: env[name] for name in expected} == expected


@pytest.mark.parametrize(
    ("name", "logged"),
    [
        ("missing", "invalid plugin: missing or escaping pyproject.toml"),
        ("escaping", "invalid plugin: missing or escaping uv.lock"),
        ("unmarked", "invalid plugin manifest: missing [tool.dusha]"),
        ("absent", "plugin not found"),
    ],
)
async def test_http_invalid_external_plugin_directory_is_reported_and_the_gateway_keeps_running(
    tmp_path, caplog, name, logged
):
    caplog.set_level(logging.WARNING, logger="dusha")
    broken = tmp_path / "plugins"
    (broken / "missing").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    suite = broken / "escaping"
    suite.mkdir()
    (suite / "escaping.py").write_text("def apply(ctx, config):\n    pass\n")
    (suite / "pyproject.toml").write_text(
        "[project]\nname='x'\nversion='0.1.0'\n\n[tool.dusha]\nentry='escaping'\n", encoding="utf-8"
    )
    (suite / "uv.lock").symlink_to(outside / "uv.lock")
    unmarked = broken / "unmarked"
    unmarked.mkdir()
    (unmarked / "pyproject.toml").write_text("[project]\nname='x'\nversion='0.1.0'\n", encoding="utf-8")
    (unmarked / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    config = _config(tmp_path, broken, {name: ExternalPluginConfig()})
    async with _client(config) as client:
        stored = await _post(client)
        status = await client.get("/state/v1/plugins")
    assert stored.get("affect") is None
    assert f"failed to load plugin {name!r}" in caplog.text and logged in caplog.text
    report = status.json()[name]
    assert report["state"] == "failed" and report["events"] == [] and logged in report["error"]


async def test_http_decide_external_plugin_is_idle_when_affect_is_off(tmp_path, plugins_dir, caplog):
    caplog.set_level(logging.WARNING, logger="dusha")
    marker = tmp_path / "seen.json"
    modules = {"affect": False, "probe": ExternalPluginConfig(options={"marker": str(marker)})}
    async with _client(_config(tmp_path, plugins_dir, modules)) as client:
        stored = await _post(client)
        affect = await client.get("/state/v1/affect")
        status = await client.get("/state/v1/plugins")
    assert stored.get("affect") is None and affect.status_code == 503
    assert status.json()["proactive"]["state"] == "inactive"
    seen = _marker(marker)
    assert "decide" not in seen and len(seen["messages"]) == 1
    assert "probe" not in caplog.text


def test_cli_plugins_reads_the_relative_plugins_dir(tmp_path, plugins_dir):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({
            "data_dir": str(tmp_path / "data"),
            "plugins_dir": os.path.relpath(plugins_dir, tmp_path),
            "plugins": {"probe": {"options": {}}},
        }),
        encoding="utf-8",
    )
    result = _cli(config_path, "plugins")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)["probe"]
    assert report["state"] == "active" and report["events"] == ["context", "decide", "message"], report
    reindex = _cli(config_path, "memory", "reindex")
    assert reindex.returncode == 0, reindex.stderr
    assert json.loads(reindex.stdout) == {"status": "rebuilt"}


def test_config_rejects_bad_external_plugin_names_fields_and_env_passthrough(tmp_path):
    with pytest.raises(ValueError, match="plain directory name"):
        AppConfig(plugins={"../outside": ExternalPluginConfig()})
    with pytest.raises(ValueError, match="external_plugin.env_passthrough"):
        ExternalPluginConfig(env_passthrough=["OK", ""])
    with pytest.raises(ValueError, match="external_plugin.timeout_seconds must be above 0"):
        ExternalPluginConfig(timeout_seconds=0)
    for raw, message in [
        ({"plugins": {"probe": {"bogus": 1}}}, r"unknown field\(s\) under plugins.probe"),
        ({"plugins": {"probe": 1}}, "plugins.probe must be an object"),
        ({"plugins": {"probe": {"disabled": "yes"}}}, "plugins.probe.disabled must be bool"),
        ({"plugins": {"../outside": {}}}, "plain directory name"),
        ({"mods": {"dir": "x"}}, r"unknown top-level field\(s\)"),
        ({"features": {"memo": False}}, r"unknown top-level field\(s\)"),
    ]:
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"data_dir": str(tmp_path / "data"), **raw}), encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            load_config(path)


async def test_http_failed_start_terminates_the_worker_and_reports_it(tmp_path, plugins_dir, caplog):
    caplog.set_level(logging.WARNING, logger="dusha")
    app = api.create_app(_config(tmp_path, plugins_dir, start_fail=True))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            status = await client.get("/state/v1/plugins")
            stored = await _post(client)
        plugin = app.state.service.registry.runtimes[-1].plugin
        assert plugin.proc is not None and plugin.proc.poll() is not None
    report = status.json()["probe"]
    assert report["state"] == "failed" and report["events"] == []
    assert report["error"] == "configured start failure"
    assert stored.get("affect") is None
    assert "failed to load plugin 'probe': configured start failure" in caplog.text


async def test_http_status_reports_a_dead_worker_without_restarting_it(tmp_path, plugins_dir, caplog):
    caplog.set_level(logging.WARNING, logger="dusha")
    marker = tmp_path / "seen.json"
    config = AppConfig(
        data_dir=tmp_path / "data",
        plugins_dir=str(plugins_dir),
        plugins={
            "probe": ExternalPluginConfig(options={"marker": str(marker), "mode": "die"}),
            "affect": AffectConfig(),
        },
    )
    async with _client(config) as client:
        first = await _post(client, "kill")
        dead = await client.get("/state/v1/plugins")
        dead_again = await client.get("/state/v1/plugins")
        assert len(_marker(marker)["start"]) == 1
        second = await _post(client, "again")
        assert len(_marker(marker)["start"]) == 2
    assert first.get("affect") is None and second.get("affect") is None
    assert dead.json()["probe"]["state"] == "failed"
    assert dead.json()["probe"]["error"] == "plugin closed its output"
    assert dead_again.json() == dead.json()
    assert "probe decide failed: plugin closed its output" in caplog.text
    assert "probe is not running, restarting" in caplog.text
