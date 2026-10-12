from __future__ import annotations

import json
import logging
import shutil
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from dusha import api
from dusha.config import AffectConfig, AppConfig, ModConfig, ModsConfig

MOD_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def mods(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("system-one-mods")
    shutil.copytree(
        MOD_DIR, root / "system_one", ignore=shutil.ignore_patterns(".venv", "__pycache__", "*.pyc", "tests")
    )
    return root


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


def _config(tmp_path: Path, mods: Path, **options) -> AppConfig:
    return AppConfig(
        data_dir=tmp_path / "data",
        affect=AffectConfig(increment=0.25),
        mods=ModsConfig(dir=str(mods), modules={"system_one": ModConfig(options=options)}),
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


async def _post_expecting_no_decision(config: AppConfig) -> None:
    async with _client(config) as client:
        before = await _base(client)
        stored = await _post(client)
        after = await _base(client)
    assert stored.status_code == 200, stored.text
    assert stored.json()["affect"] is None
    assert after == pytest.approx(before, abs=1e-3)


@pytest.mark.parametrize("allowlist", [{"allowed_ips": ["127.0.0.1"]}, {}])
async def test_system_one_asks_the_classifier_and_applies_its_choice(tmp_path, mods, classifier, allowlist):
    server = classifier()
    options = {"base_url": server.url, "api_key": "secret", "model": "tiny", **allowlist}
    async with _client(_config(tmp_path, mods, **options)) as client:
        before = await _base(client)
        stored = await _post(client)
        status = await client.get("/state/v1/mods")
    assert stored.status_code == 200, stored.text
    assert stored.json()["affect"]["emotion"] == "fear"
    assert stored.json()["affect"]["state"]["base"]["fear"] == pytest.approx(before["fear"] + 0.25, abs=1e-3)
    assert status.json()["system_one"]["exports"] == ["decide"]
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
    assert set(question["criteria"]) == set(before)


@pytest.mark.parametrize(
    ("allowed_ips", "logged"),
    [
        (["127.0.0.2"], "base_url address 127.0.0.1 is not in 'allowed_ips'"),
        (["not-an-ip"], "'allowed_ips' contains invalid address 'not-an-ip'"),
        ("127.0.0.1", "'allowed_ips' must be a list of IPv4 or IPv6 addresses"),
        ([3], "'allowed_ips' must contain only IP address strings"),
    ],
)
async def test_system_one_rejects_denied_or_invalid_allowed_ips_before_any_request(
    tmp_path, mods, classifier, caplog, allowed_ips, logged
):
    caplog.set_level(logging.WARNING, logger="dusha")
    server = classifier()
    await _post_expecting_no_decision(_config(tmp_path, mods, base_url=server.url, allowed_ips=allowed_ips))
    assert server.requests == []
    assert logged in caplog.text


async def test_system_one_redirect_cannot_leave_the_allowlist(tmp_path, mods, classifier, caplog):
    caplog.set_level(logging.WARNING, logger="dusha")
    server = classifier(redirect_to="http://127.0.0.2:1/v1/systemone")
    await _post_expecting_no_decision(_config(tmp_path, mods, base_url=server.url, allowed_ips=["127.0.0.1"]))
    assert len(server.requests) == 1
    assert "redirect address 127.0.0.2 is not in 'allowed_ips'" in caplog.text


async def test_system_one_cross_origin_redirect_drops_credentials(tmp_path, mods, classifier):
    target = classifier()
    origin = classifier(redirect_to=f"{target.url}/v1/systemone")
    async with _client(_config(tmp_path, mods, base_url=origin.url, api_key="secret")) as client:
        stored = await _post(client)
    assert stored.status_code == 200, stored.text
    assert stored.json()["affect"]["emotion"] == "fear"
    assert [request["authorization"] for request in origin.requests] == ["Bearer secret"]
    assert [request["authorization"] for request in target.requests] == [None]
    assert target.requests[0]["body"] == origin.requests[0]["body"]


async def test_system_one_provider_failure_leaves_affect_unchanged(tmp_path, mods, classifier, caplog):
    caplog.set_level(logging.WARNING, logger="dusha")
    server = classifier(status=503)
    await _post_expecting_no_decision(_config(tmp_path, mods, base_url=server.url))
    assert len(server.requests) == 1
    assert f"system_one provider returned HTTP 503 for {server.url}/v1/systemone" in caplog.text
