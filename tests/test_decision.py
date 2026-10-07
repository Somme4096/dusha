from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from dusha import api
from dusha.config import AppConfig, DecisionConfig


def _suite(tmp_path: Path, name: str, main: str, workspace: bool = False) -> tuple[Path, Path]:
    if workspace:
        (tmp_path / "other").mkdir(parents=True)
        (tmp_path / "pyproject.toml").write_text(
            '[tool.uv.workspace]\nmembers = ["other"]\n', encoding="utf-8"
        )
    mods = tmp_path / "mods"
    suite = mods / name
    suite.mkdir(parents=True)
    (suite / "README.md").write_text("decision suite\n", encoding="utf-8")
    (suite / "main.py").write_text(main, encoding="utf-8")
    (suite / "pyproject.toml").write_text(
        '[project]\nname = "decision-suite"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n',
        encoding="utf-8",
    )
    subprocess.run(["uv", "--no-config", "lock", "--directory", str(suite)], check=True)
    return mods, suite


def _config(tmp_path: Path, mods: Path, module: str, options: dict | None = None) -> AppConfig:
    return AppConfig(
        data_dir=tmp_path / "data",
        decision=DecisionConfig(module=module, mods_dir=str(mods), options=options or {}, increment=0.25),
    )


async def _post(config: AppConfig, content: str = "hello") -> httpx.Response:
    app = api.create_app(config)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(
            "/state/v1/messages",
            json={"harness": "api", "conversation_id": "one", "role": "user", "content": content},
        )


@pytest.mark.asyncio
async def test_http_suite_supports_helper_import_and_preserves_request(tmp_path: Path):
    marker = tmp_path / "marker.json"
    mods, _ = _suite(
        tmp_path,
        "chooser",
        "from helper import choose\n"
        "import json\n"
        "def decide(request, options):\n"
        "    with open(options['marker'], 'w') as handle:\n"
        "        json.dump({'message': request.message, 'instruction': request.instruction, 'cwd': __import__('os').getcwd()}, handle)\n"
        "    return {'emotion': choose(request.emotions)}\n",
        workspace=True,
    )
    suite = mods / "chooser"
    (suite / "helper.py").write_text("def choose(emotions):\n    return 'fear' if 'fear' in emotions else None\n", encoding="utf-8")
    response = await _post(_config(tmp_path, mods, "chooser", {"marker": str(marker)}))
    assert response.status_code == 200
    assert response.json()["affect"]["emotion"] == "fear"
    captured = json.loads(marker.read_text(encoding="utf-8"))
    assert captured["message"] == "hello"
    assert captured["cwd"] == str(suite)


@pytest.mark.asyncio
async def test_http_suite_sanitizes_environment_and_uses_designated_python(tmp_path: Path, monkeypatch):
    marker = tmp_path / "environment.json"
    monkeypatch.setenv("GATEWAY_SECRET", "secret")
    monkeypatch.setenv("PYTHONPATH", "gateway-path")
    monkeypatch.setenv("VIRTUAL_ENV", "gateway-venv")
    mods, _ = _suite(
        tmp_path,
        "environment",
        "import json, os, sys\n"
        "def decide(request, options):\n"
        "    with open(options['marker'], 'w') as handle:\n"
        "        json.dump({'secret': os.getenv('GATEWAY_SECRET'), 'path': os.getenv('PYTHONPATH'), 'venv': os.getenv('VIRTUAL_ENV'), 'python': sys.executable}, handle)\n"
        "    return {'emotion': 'fear'}\n",
    )
    response = await _post(_config(tmp_path, mods, "environment", {"marker": str(marker)}))
    assert response.status_code == 200
    captured = json.loads(marker.read_text(encoding="utf-8"))
    assert captured["secret"] is None
    assert captured["path"] is None
    assert captured["venv"] is None
    assert str(mods / "environment" / ".venv") in captured["python"]


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["None", "{'emotion': 'missing'}", "{'wrong': 'fear'}"])
async def test_http_suite_rejects_abstain_and_invalid_results(tmp_path: Path, result: str):
    mods, _ = _suite(tmp_path, "bad", f"def decide(request, options):\n    return {result}\n")
    response = await _post(_config(tmp_path, mods, "bad"))
    assert response.status_code == 200
    assert response.json()["affect"] is None


@pytest.mark.asyncio
async def test_http_suite_handles_crash_timeout_and_output_limits(tmp_path: Path):
    cases = [
        "def decide(request, options):\n    raise RuntimeError('boom')\n",
        "import time\ndef decide(request, options):\n    time.sleep(10)\n    return {'emotion': 'fear'}\n",
        "def decide(request, options):\n    print('x' * 2000000)\n    return {'emotion': 'fear'}\n",
        "import sys\ndef decide(request, options):\n    sys.stderr.write('x' * 2000000)\n    return {'emotion': 'fear'}\n",
    ]
    for index, main in enumerate(cases):
        mods, _ = _suite(tmp_path / str(index), "case", main)
        config = _config(tmp_path / str(index), mods, "case")
        config.decision.timeout_seconds = 0.2
        response = await _post(config)
        assert response.status_code == 200
        assert response.json()["affect"] is None


@pytest.mark.asyncio
async def test_http_suite_cleans_descendant_after_worker_exit(tmp_path: Path):
    marker = tmp_path / "marker"
    mods, _ = _suite(
        tmp_path,
        "descendant",
        f"import subprocess, sys, time\n"
        f"def decide(request, options):\n"
        f"    subprocess.Popen([sys.executable, '-c', \"import pathlib, time; time.sleep(2); pathlib.Path({str(marker)!r}).write_text('alive')\"])\n"
        f"    time.sleep(0.05)\n"
        f"    return None\n",
    )
    config = _config(tmp_path, mods, "descendant")
    config.decision.timeout_seconds = 1.0
    started = time.monotonic()
    response = await _post(config)
    elapsed = time.monotonic() - started
    assert response.status_code == 200
    assert response.json()["affect"] is None
    assert elapsed < 2.0
    await asyncio.sleep(2.2)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_http_suite_allows_runtime_longer_than_five_seconds(tmp_path: Path):
    mods, _ = _suite(
        tmp_path,
        "slow_success",
        "import time\n"
        "def decide(request, options):\n"
        "    time.sleep(5.2)\n"
        "    return {'emotion': 'fear'}\n",
    )
    config = _config(tmp_path, mods, "slow_success")
    config.decision.timeout_seconds = 6.0
    response = await _post(config)
    assert response.status_code == 200
    assert response.json()["affect"]["emotion"] == "fear"


def test_invalid_suites_and_required_file_escape_are_disabled(tmp_path: Path):
    mods = tmp_path / "mods"
    (mods / "missing").mkdir(parents=True)
    config = _config(tmp_path, mods, "missing")
    assert api.create_app(config).state.service.decision.enabled is False

    outside = tmp_path / "outside"
    outside.write_text("outside", encoding="utf-8")
    suite = mods / "escaping"
    suite.mkdir()
    (suite / "README.md").write_text("suite", encoding="utf-8")
    (suite / "main.py").write_text("def decide(request, options): return None", encoding="utf-8")
    (suite / "pyproject.toml").write_text("[project]\nname='x'\nversion='0.1.0'\n", encoding="utf-8")
    (suite / "uv.lock").symlink_to(outside)
    assert api.create_app(_config(tmp_path, mods, "escaping")).state.service.decision.enabled is False
    assert api.create_app(_config(tmp_path, mods, "../outside")).state.service.decision.enabled is False


@pytest.mark.asyncio
async def test_http_restart_preserves_suite_decision_state(tmp_path: Path):
    mods, _ = _suite(tmp_path, "restart", "def decide(request, options):\n    return {'emotion': 'fear'}\n")
    config = _config(tmp_path, mods, "restart")
    first = api.create_app(config)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=first), base_url="http://test") as client:
        response = await client.post(
            "/state/v1/messages",
            json={"harness": "api", "conversation_id": "one", "role": "user", "content": "hello", "external_id": "restart"},
        )
    message_id = response.json()["id"]
    first.state.service.close()
    second = api.create_app(config)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=second), base_url="http://test") as client:
        restored = await client.get(f"/state/v1/messages/{message_id}")
    second.state.service.close()
    assert restored.status_code == 200
    assert restored.json()["text"] == "hello"
