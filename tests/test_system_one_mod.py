from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

_MOD_PATH = Path(__file__).resolve().parents[1] / "examples" / "mods" / "system_one" / "main.py"


def _load_system_one():
    spec = importlib.util.spec_from_file_location("system_one_suite_under_test", _MOD_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


system_one = _load_system_one()


def _request() -> SimpleNamespace:
    return SimpleNamespace(
        message="hello",
        emotions={"affectionate": {"description": "warm"}},
        state={"longing": 0.4},
        instruction="Pick one.",
    )


class _Handler(BaseHTTPRequestHandler):
    response_status = 200
    response_body = {"answers": {"emotion": {"type": "choice", "choice": "affectionate"}}}
    redirect_to: str | None = None
    redirect_count = 0
    last_authorization: str | None = None

    def do_POST(self):
        type(self).last_authorization = self.headers.get("Authorization")
        if self.redirect_to and self.redirect_count:
            type(self).redirect_count -= 1
            self.send_response(302)
            self.send_header("Location", self.redirect_to)
            self.end_headers()
            return
        self.send_response(self.response_status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(self.response_body).encode())

    def log_message(self, format, *args):
        pass


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        thread.join()


def _url(server) -> str:
    return f"http://127.0.0.1:{server.server_port}"


def test_real_local_http_success_and_contract(server):
    result = system_one.decide(_request(), {"base_url": _url(server), "allowed_ips": ["127.0.0.1"]})
    assert result == {"emotion": "affectionate"}


def test_allowed_ip_is_optional(server):
    assert system_one.decide(_request(), {"base_url": _url(server)}) == {"emotion": "affectionate"}


def test_denied_address_is_rejected_before_request(server):
    with pytest.raises(ValueError, match="allowed_ips"):
        system_one.decide(_request(), {"base_url": _url(server), "allowed_ips": ["127.0.0.2"]})


@pytest.mark.parametrize("value", [["not-an-ip"], "127.0.0.1", [3]])
def test_invalid_allowed_ips_are_rejected(server, value):
    with pytest.raises(ValueError, match="allowed_ips"):
        system_one.decide(_request(), {"base_url": _url(server), "allowed_ips": value})


def test_redirect_cannot_bypass_allowlist(server):
    _Handler.redirect_to = "http://127.0.0.2:1/v1/systemone"
    _Handler.redirect_count = 1
    try:
        with pytest.raises(system_one.SystemOneModError, match="allowed_ips"):
            system_one.decide(_request(), {"base_url": _url(server), "allowed_ips": ["127.0.0.1"]})
    finally:
        _Handler.redirect_to = None
        _Handler.redirect_count = 0


def test_cross_origin_redirect_drops_explicit_credentials(server):
    target = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=target.serve_forever, daemon=True)
    thread.start()
    _Handler.redirect_to = f"http://127.0.0.1:{target.server_port}/v1/systemone"
    _Handler.redirect_count = 1
    _Handler.last_authorization = None
    try:
        assert system_one.decide(_request(), {"base_url": _url(server), "api_key": "secret"}) == {
            "emotion": "affectionate"
        }
        assert _Handler.last_authorization is None
    finally:
        _Handler.redirect_to = None
        _Handler.redirect_count = 0
        target.shutdown()
        thread.join()


def test_provider_failure_is_reported(server):
    _Handler.response_status = 503
    try:
        with pytest.raises(system_one.SystemOneModError, match="HTTP 503"):
            system_one.decide(_request(), {"base_url": _url(server)})
    finally:
        _Handler.response_status = 200


def test_subprocess_suite_entrypoint(server):
    script = """
import importlib.util, json, sys
from types import SimpleNamespace
spec = importlib.util.spec_from_file_location('suite', sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
req = SimpleNamespace(message='hello', emotions={'affectionate': {}}, state={}, instruction='Pick one')
print(json.dumps(mod.decide(req, {'base_url': sys.argv[2]})))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(_MOD_PATH), _url(server)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == {"emotion": "affectionate"}
