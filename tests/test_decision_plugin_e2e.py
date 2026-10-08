from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from dusha import api
from dusha.config import AppConfig, DecisionConfig

SYSTEM_ONE = Path(__file__).parents[1] / "examples" / "mods" / "system_one"

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


def decide(request, options):
    mode = options.get("mode", "choose")
    if "marker" in options:
        seen = {
            "message": request.message,
            "instruction": request.instruction,
            "emotions": sorted(request.emotions),
            "state": sorted(request.state),
            "cwd": os.getcwd(),
            "python": sys.executable,
            "env": {name: os.getenv(name) for name in ("PYTHONPATH", "VIRTUAL_ENV")},
        }
        pathlib.Path(options["marker"]).write_text(json.dumps(seen))
    if mode == "abstain":
        return None
    if mode == "unknown":
        return {"emotion": "missing"}
    if mode == "malformed":
        return {"wrong": "fear"}
    if mode == "crash":
        raise RuntimeError("boom")
    if mode == "sleep":
        time.sleep(options["seconds"])
    if mode == "stdout_flood":
        print("x" * 2000000)
    if mode == "stderr_flood":
        sys.stderr.write("x" * 2000000)
    if mode == "descendant":
        subprocess.Popen([sys.executable, "-c", CHILD, options["started"], options["alive"]])
        while not os.path.exists(options["started"]):
            time.sleep(0.01)
        return None
    return {"emotion": choose(request.emotions)}
'''


@pytest.fixture(scope="module")
def mods(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("decision-workspace")
    (root / "other").mkdir()
    (root / "pyproject.toml").write_text('[tool.uv.workspace]\nmembers = ["other"]\n', encoding="utf-8")
    suite = root / "mods" / "probe"
    suite.mkdir(parents=True)
    (suite / "README.md").write_text("decision e2e plugin\n", encoding="utf-8")
    (suite / "main.py").write_text(PROBE_SOURCE, encoding="utf-8")
    (suite / "helper.py").write_text(HELPER_SOURCE, encoding="utf-8")
    (suite / "pyproject.toml").write_text(
        '[project]\nname = "probe"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n', encoding="utf-8"
    )
    subprocess.run(["uv", "--no-config", "lock", "--directory", str(suite)], check=True)
    shutil.copytree(
        SYSTEM_ONE,
        root / "mods" / "system_one",
        ignore=shutil.ignore_patterns(".venv", "__pycache__", "*.pyc"),
    )
    return root / "mods"


class _Classifier:
    def __init__(self, status: int = 200, redirect_to: str | None = None):
        self.requests: list[dict] = []
        classifier = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                classifier.requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "body": json.loads(body or b"{}"),
                    }
                )
                if redirect_to:
                    self.send_response(302)
                    self.send_header("Location", redirect_to)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                payload = json.dumps({"answers": {"emotion": {"type": "choice", "choice": "fear"}}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()


# Local stand-in for the external SystemOne classifier service, the only stubbed component.
@pytest.fixture
def classifier():
    started: list[_Classifier] = []

    def start(**kwargs) -> _Classifier:
        started.append(_Classifier(**kwargs))
        return started[-1]

    yield start
    for server in started:
        server.close()


@asynccontextmanager
async def _client(config: AppConfig):
    app = api.create_app(config)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def _config(tmp_path: Path, mods: Path, module: str = "probe", timeout: float = 15.0, **options) -> AppConfig:
    return AppConfig(
        data_dir=tmp_path / "data",
        decision=DecisionConfig(
            module=module, mods_dir=str(mods), options=options, increment=0.25, timeout_seconds=timeout
        ),
    )


async def _post(client: httpx.AsyncClient, content: str = "hello") -> httpx.Response:
    return await client.post(
        "/state/v1/messages",
        json={"harness": "e2e", "conversation_id": "decision", "role": "user", "content": content},
    )


async def _base(client: httpx.AsyncClient) -> dict:
    response = await client.get("/state/v1/affect")
    assert response.status_code == 200, response.text
    return response.json()["base"]


async def _post_expecting_no_decision(config: AppConfig) -> float:
    async with _client(config) as client:
        before = await _base(client)
        started = time.monotonic()
        stored = await _post(client)
        elapsed = time.monotonic() - started
        after = await _base(client)
    assert stored.status_code == 200, stored.text
    assert stored.json()["affect"] is None
    assert after == pytest.approx(before, abs=1e-3)
    return elapsed


async def test_http_plugin_picks_a_dimension_with_its_helper_and_the_documented_request(
    tmp_path, mods, monkeypatch
):
    monkeypatch.setenv("PYTHONPATH", "gateway-path")
    monkeypatch.setenv("VIRTUAL_ENV", "gateway-venv")
    marker = tmp_path / "request.json"
    async with _client(_config(tmp_path, mods, marker=str(marker))) as client:
        before = await _base(client)
        stored = await _post(client)
        after = await _base(client)
    assert stored.status_code == 200, stored.text
    affect = stored.json()["affect"]
    assert affect["emotion"] == "fear"
    assert affect["increment"] == pytest.approx(0.25)
    assert affect["state"]["base"]["fear"] == pytest.approx(before["fear"] + 0.25, abs=1e-3)
    assert after["fear"] == pytest.approx(before["fear"] + 0.25, abs=1e-3)
    seen = json.loads(marker.read_text(encoding="utf-8"))
    assert seen["message"] == "hello"
    assert seen["instruction"].strip()
    assert set(seen["emotions"]) == set(before)
    assert "base" in seen["state"]
    assert seen["cwd"] == str(mods / "probe")
    assert seen["python"].startswith(str(mods / "probe" / ".venv"))
    assert seen["env"] == {"PYTHONPATH": None, "VIRTUAL_ENV": None}


@pytest.mark.parametrize(
    ("mode", "logged"),
    [
        ("abstain", None),
        ("unknown", "decision plugin returned invalid emotion: 'missing'"),
        ("malformed", "decision plugin returned an invalid result"),
    ],
)
async def test_http_abstain_unknown_dimension_and_malformed_result_leave_affect_unchanged(
    tmp_path, mods, caplog, mode, logged
):
    caplog.set_level(logging.WARNING, logger="dusha")
    await _post_expecting_no_decision(_config(tmp_path, mods, mode=mode))
    if logged is None:
        assert "decision plugin" not in caplog.text
    else:
        assert logged in caplog.text


@pytest.mark.parametrize(
    ("mode", "logged"),
    [
        ("crash", "decision plugin stderr: RuntimeError: boom"),
        ("stdout_flood", "decision plugin stderr: RuntimeError: decision plugin output limit exceeded"),
        ("stderr_flood", "decision plugin stderr: xxxx"),
    ],
)
async def test_http_crash_and_oversized_output_leave_affect_unchanged(tmp_path, mods, caplog, mode, logged):
    caplog.set_level(logging.WARNING, logger="dusha")
    await _post_expecting_no_decision(_config(tmp_path, mods, mode=mode))
    assert logged in caplog.text
    assert "decision plugin failed. No decision-driven adjustment" in caplog.text


async def test_http_timeout_stops_the_plugin_and_ingest_survives(tmp_path, mods, caplog):
    caplog.set_level(logging.WARNING, logger="dusha")
    marker = tmp_path / "started.json"
    config = _config(tmp_path, mods, timeout=2.0, mode="sleep", seconds=30, marker=str(marker))
    elapsed = await _post_expecting_no_decision(config)
    assert marker.exists()
    assert elapsed < 10.0
    assert "decision plugin failed. No decision-driven adjustment" in caplog.text


async def test_http_runtime_longer_than_five_seconds_succeeds_within_the_timeout(tmp_path, mods):
    async with _client(_config(tmp_path, mods, timeout=8.0, mode="sleep", seconds=5.2)) as client:
        started = time.monotonic()
        stored = await _post(client)
        elapsed = time.monotonic() - started
    assert stored.status_code == 200, stored.text
    assert stored.json()["affect"]["emotion"] == "fear"
    assert elapsed > 5.0


async def test_http_descendant_process_is_stopped_after_the_worker_exits(tmp_path, mods):
    started, alive = tmp_path / "started", tmp_path / "alive"
    config = _config(tmp_path, mods, mode="descendant", started=str(started), alive=str(alive))
    await _post_expecting_no_decision(config)
    assert started.exists()
    await asyncio.sleep(2.0)
    assert not alive.exists()


@pytest.mark.parametrize(
    ("module", "logged"),
    [
        ("missing", "invalid decision plugin: missing or escaping README.md"),
        ("escaping", "invalid decision plugin: missing or escaping uv.lock"),
        ("../outside", "decision.module must contain a plain plugin name"),
    ],
)
async def test_http_invalid_plugin_directory_disables_the_plugin(tmp_path, caplog, module, logged):
    caplog.set_level(logging.WARNING, logger="dusha")
    broken = tmp_path / "mods"
    (broken / "missing").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    suite = broken / "escaping"
    suite.mkdir()
    (suite / "README.md").write_text("suite\n", encoding="utf-8")
    (suite / "main.py").write_text("def decide(request, options):\n    return {'emotion': 'fear'}\n")
    (suite / "pyproject.toml").write_text("[project]\nname='x'\nversion='0.1.0'\n", encoding="utf-8")
    (suite / "uv.lock").symlink_to(outside / "uv.lock")
    await _post_expecting_no_decision(_config(tmp_path, broken, module=module))
    assert f"failed to initialize decision plugin {module!r}" in caplog.text
    assert logged in caplog.text


async def test_http_restart_preserves_decision_state(tmp_path, mods):
    config = _config(tmp_path, mods)
    async with _client(config) as client:
        neutral = await _base(client)
        first = (await _post(client, "before restart")).json()
    assert first["affect"]["emotion"] == "fear"
    async with _client(config) as client:
        restored = await _base(client)
        message = await client.get(f"/state/v1/messages/{first['id']}")
        second = (await _post(client, "after restart")).json()
    assert restored["fear"] == pytest.approx(neutral["fear"] + 0.25, abs=1e-3)
    assert message.status_code == 200
    assert message.json()["text"] == "before restart"
    assert second["affect"]["decision_id"] > first["affect"]["decision_id"]
    assert second["affect"]["state"]["base"]["fear"] == pytest.approx(neutral["fear"] + 0.5, abs=1e-3)


@pytest.mark.parametrize("allowlist", [{"allowed_ips": ["127.0.0.1"]}, {}])
async def test_http_system_one_asks_the_classifier_and_applies_its_choice(
    tmp_path, mods, classifier, allowlist
):
    server = classifier()
    options = {"base_url": server.url, "api_key": "secret", "model": "tiny", **allowlist}
    async with _client(_config(tmp_path, mods, module="system_one", **options)) as client:
        dimensions = set(await _base(client))
        stored = await _post(client)
    assert stored.status_code == 200, stored.text
    assert stored.json()["affect"]["emotion"] == "fear"
    assert len(server.requests) == 1
    request = server.requests[0]
    assert request["path"] == "/v1/systemone"
    assert request["authorization"] == "Bearer secret"
    assert request["body"]["model"] == "tiny"
    assert request["body"]["state"]["message"] == "hello"
    assert "base" in request["body"]["state"]["emotion_state"]
    question = request["body"]["questions"]["emotion"]
    assert question["type"] == "choice"
    assert question["instructions"].strip()
    assert set(question["criteria"]) == dimensions


@pytest.mark.parametrize(
    ("allowed_ips", "logged"),
    [
        (["127.0.0.2"], "base_url address 127.0.0.1 is not in 'allowed_ips'"),
        (["not-an-ip"], "'allowed_ips' contains invalid address 'not-an-ip'"),
        ("127.0.0.1", "'allowed_ips' must be a list of IPv4 or IPv6 addresses"),
        ([3], "'allowed_ips' must contain only IP address strings"),
    ],
)
async def test_http_system_one_rejects_denied_or_invalid_allowed_ips_before_any_request(
    tmp_path, mods, classifier, caplog, allowed_ips, logged
):
    caplog.set_level(logging.WARNING, logger="dusha")
    server = classifier()
    config = _config(tmp_path, mods, module="system_one", base_url=server.url, allowed_ips=allowed_ips)
    await _post_expecting_no_decision(config)
    assert server.requests == []
    assert logged in caplog.text


async def test_http_system_one_redirect_cannot_leave_the_allowlist(tmp_path, mods, classifier, caplog):
    caplog.set_level(logging.WARNING, logger="dusha")
    server = classifier(redirect_to="http://127.0.0.2:1/v1/systemone")
    config = _config(tmp_path, mods, module="system_one", base_url=server.url, allowed_ips=["127.0.0.1"])
    await _post_expecting_no_decision(config)
    assert len(server.requests) == 1
    assert "redirect address 127.0.0.2 is not in 'allowed_ips'" in caplog.text


async def test_http_system_one_cross_origin_redirect_drops_credentials(tmp_path, mods, classifier):
    target = classifier()
    origin = classifier(redirect_to=f"{target.url}/v1/systemone")
    config = _config(tmp_path, mods, module="system_one", base_url=origin.url, api_key="secret")
    async with _client(config) as client:
        stored = await _post(client)
    assert stored.status_code == 200, stored.text
    assert stored.json()["affect"]["emotion"] == "fear"
    assert [request["authorization"] for request in origin.requests] == ["Bearer secret"]
    assert [request["authorization"] for request in target.requests] == [None]
    assert target.requests[0]["body"] == origin.requests[0]["body"]


async def test_http_system_one_provider_failure_leaves_affect_unchanged(tmp_path, mods, classifier, caplog):
    caplog.set_level(logging.WARNING, logger="dusha")
    server = classifier(status=503)
    await _post_expecting_no_decision(_config(tmp_path, mods, module="system_one", base_url=server.url))
    assert len(server.requests) == 1
    assert f"system_one provider returned HTTP 503 for {server.url}/v1/systemone" in caplog.text
