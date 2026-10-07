from __future__ import annotations

import json
import logging
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from companion_gateway import api
from companion_gateway.config import AppConfig, DecisionConfig, MemoryPluginConfig

MEMORY_SOURCE = '''\
import os


def status(request, options):
    return {"status": {name: os.getenv(name) for name in ("GATEWAY_SECRET", "SHARED_VALUE")}}


def ingest_messages(request, options):
    return {"highest_id": max((int(item["id"]) for item in request.messages), default=0)}


def match_phrase(request, options):
    return {"deltas": None}


def close(request, options):
    return None
'''

DECISION_SOURCE = '''\
import json, os, sys


def decide(request, options):
    if options.get("crash"):
        print("decider exploded: bad endpoint", file=sys.stderr)
        raise SystemExit(3)
    with open(options["marker"], "w") as handle:
        json.dump({name: os.getenv(name) for name in ("GATEWAY_SECRET", "SHARED_VALUE")}, handle)
    return {"emotion": "fear"}


if __name__ == "__main__":
    raise SystemExit("main guard must not run inside the gateway")
'''


@pytest.fixture(scope="module")
def mods(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("env-mods")
    for name, source in (("env_memory", MEMORY_SOURCE), ("env_decider", DECISION_SOURCE)):
        suite = root / name
        suite.mkdir()
        (suite / "README.md").write_text("environment e2e plugin\n", encoding="utf-8")
        (suite / "main.py").write_text(source, encoding="utf-8")
        (suite / "pyproject.toml").write_text(
            f'[project]\nname = "{name.replace("_", "-")}"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n',
            encoding="utf-8",
        )
        subprocess.run(["uv", "--no-config", "lock", "--directory", str(suite)], check=True)
    return root


@asynccontextmanager
async def _client(config: AppConfig):
    app = api.create_app(config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client


def _config(tmp_path: Path, mods: Path, passthrough: list[str], **options) -> AppConfig:
    return AppConfig(
        data_dir=tmp_path / "data",
        decision=DecisionConfig(
            module="env_decider", mods_dir=str(mods), options=options, env_passthrough=passthrough
        ),
        memory_plugin=MemoryPluginConfig(
            module="env_memory", mods_dir=str(mods), env_passthrough=passthrough
        ),
    )


async def _post(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post(
        "/state/v1/messages",
        json={"harness": "e2e", "conversation_id": "env", "role": "user", "content": "hello"},
    )


@pytest.mark.parametrize(
    ("passthrough", "expected"),
    [
        ([], {"GATEWAY_SECRET": None, "SHARED_VALUE": None}),
        (["SHARED_VALUE"], {"GATEWAY_SECRET": None, "SHARED_VALUE": "shared"}),
    ],
)
async def test_http_both_plugin_kinds_see_the_same_filtered_environment(
    tmp_path, mods, monkeypatch, passthrough, expected
):
    monkeypatch.setenv("GATEWAY_SECRET", "secret")
    monkeypatch.setenv("SHARED_VALUE", "shared")
    marker = tmp_path / "decision-env.json"
    async with _client(_config(tmp_path, mods, passthrough, marker=str(marker))) as client:
        stored = await _post(client)
        index = await client.get("/state/v1/memory/index")
    assert stored.json()["affect"]["emotion"] == "fear"
    assert json.loads(marker.read_text(encoding="utf-8")) == expected
    assert index.json() == expected


async def test_http_decision_plugin_crash_logs_its_stderr_and_ingest_survives(tmp_path, mods, caplog):
    caplog.set_level(logging.WARNING, logger="companion_gateway")
    async with _client(_config(tmp_path, mods, [], crash=True)) as client:
        stored = await _post(client)
    assert stored.status_code == 200
    assert stored.json()["affect"] is None
    assert "decision plugin stderr: decider exploded: bad endpoint" in caplog.text


def test_config_rejects_invalid_env_passthrough():
    with pytest.raises(ValueError, match="decision.env_passthrough"):
        DecisionConfig(env_passthrough=["OK", ""])
    with pytest.raises(ValueError, match="memory_plugin.env_passthrough"):
        MemoryPluginConfig(env_passthrough="PATH")
