from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from dusha import api
from dusha.config import AppConfig, ExternalPluginConfig, PluginEntry, RelayConfig, load_config

DEVICES_SOURCE = """\
import json
import pathlib


def apply(ctx, config):
    path = pathlib.Path(config["snapshot"])
    ctx.on("presence", lambda: json.loads(path.read_text()))
    ctx.on("notification.send", lambda notice: {"delivered": [notice["channel"]], "notice": notice})
"""


@pytest.fixture
def plugins_dir(tmp_path: Path) -> Path:
    suite = tmp_path / "plugins" / "devices"
    suite.mkdir(parents=True)
    (suite / "devices.py").write_text(DEVICES_SOURCE, encoding="utf-8")
    (suite / "pyproject.toml").write_text('[tool.dusha]\nentry = "devices"\n', encoding="utf-8")
    return suite.parent


@pytest.fixture
def device(tmp_path: Path):
    calls: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append({"path": self.path, "token": self.headers.get("x-companion-token"), "body": body})
            payload = json.dumps({"delivered": ["desktop"]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", calls
    server.shutdown()
    server.server_close()


def gateway(tmp_path: Path, plugins_dir: Path, snapshot: Path | None, relay: RelayConfig | None):
    plugins: dict = {}
    if snapshot is not None:
        plugins["devices"] = PluginEntry(ExternalPluginConfig(options={"snapshot": str(snapshot)}))
    if relay is not None:
        plugins["relay"] = PluginEntry(relay)
    config = AppConfig(data_dir=tmp_path / "data", plugins_dir=str(plugins_dir), plugins=plugins)
    return api.create_app(config)


def mcp(url: str, tool: str, **arguments) -> tuple[bool, dict]:
    params = {"name": tool, "arguments": arguments}
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    result = httpx.post(url + "/mcp", json=message, timeout=30).json()["result"]
    return bool(result.get("isError")), json.loads(result["content"][0]["text"])


def test_relay_notifies_the_phone_or_the_active_device_and_explains_refusals(
    tmp_path, plugins_dir, device, run_app
):
    url, calls = device
    snapshot = tmp_path / "snapshot.json"
    macbook = {"device": "macbook", "status": "online", "url": url, "capabilities": ["notify"]}
    nas = {"device": "nas", "status": "online", "local": True, "capabilities": ["notify"]}
    snapshot.write_text(json.dumps({"local": "nas", "active_device": None, "devices": [nas, macbook]}))
    relay = RelayConfig(token="shared", tokens={"macbook": "mac-secret"}, mirror_proactive=False)
    with run_app(gateway(tmp_path, plugins_dir, snapshot, relay)) as hub:
        away_failed, away = mcp(hub, "notify", title="away", body="you there?")
        active = {"local": "nas", "active_device": "macbook", "devices": [nas, macbook]}
        snapshot.write_text(json.dumps(active))
        here_failed, here = mcp(hub, "notify", title="here", body="hi!")
        look_failed, look = mcp(hub, "look", device="macbook")
        unknown_failed, unknown = mcp(hub, "notify", title="x", device="toaster")
        gone = {**macbook, "status": "off", "since": "2026-11-03T05:02:00+00:00"}
        snapshot.write_text(json.dumps({"local": "nas", "active_device": None, "devices": [nas, gone]}))
        off_failed, off = mcp(hub, "notify", title="gone", device="macbook")
    assert not away_failed and away["delivered"] == ["phone"]
    assert away["notice"] == {"title": "away", "body": "you there?", "urgency": "normal", "channel": "phone"}
    assert not here_failed and here == {"delivered": ["desktop"]}
    assert calls == [
        {
            "path": "/state/v1/notification",
            "token": "mac-secret",
            "body": {"title": "here", "body": "hi!", "urgency": "normal", "channel": "desktop"},
        }
    ]
    assert look_failed and look["detail"] == "macbook can't look, it offers: notify"
    assert unknown_failed and "unknown device 'toaster'" in unknown["detail"]
    assert off_failed and off["detail"].startswith("macbook is off since ")


def test_relay_without_presence_says_so(tmp_path, plugins_dir, run_app):
    with run_app(gateway(tmp_path, plugins_dir, None, RelayConfig())) as hub:
        failed, result = mcp(hub, "notify", title="x")
    assert failed and result["detail"] == "relay needs the presence plugin to know which devices exist"


def test_relay_stays_off_until_config_lists_it(tmp_path, plugins_dir, run_app):
    with run_app(gateway(tmp_path, plugins_dir, None, None)) as hub:
        plugins = httpx.get(hub + "/state/v1/plugins").json()
        listed = httpx.post(hub + "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert "relay" not in plugins
    assert not names & {"look", "notify", "spawn", "shell", "speak"}


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ({"options": {"token": "x"}}, "unknown field(s) under plugins.relay: ['options']"),
        ({"speak_order": ["presence", "pigeon"]}, "unknown plugins.relay.speak_order route(s): ['pigeon']"),
        ({"timeout_seconds": 0}, "plugins.relay.timeout_seconds must be above 0"),
    ],
)
def test_relay_config_rejects_bad_sections(tmp_path, section, message):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"plugins": {"relay": section}}), encoding="utf-8")
    with pytest.raises(ValueError) as error:
        load_config(path)
    assert message in str(error.value)


def test_relay_config_reads_flat_options(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"plugins": {"relay": {"token": "s", "spawn_timeout_seconds": 1}}}))
    relay = load_config(path).plugins["relay"]
    assert not relay.disabled and relay.config.token == "s" and relay.config.spawn_timeout_seconds == 1
