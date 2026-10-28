from __future__ import annotations

import json
import os
import select
import stat
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from dusha import api
from dusha.client import settings
from dusha.client.proxy import create_proxy_app
from dusha.config import load_config

SRC = str(Path(__file__).parents[1] / "src")
DEAD_SERVER = "http://127.0.0.1:9"


def _env(root: Path) -> dict[str, str]:
    return {
        **os.environ, "PYTHONPATH": SRC, "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_CACHE_HOME": str(root / "cache"),
    }


def _cli(root: Path, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "dusha.cli", *args], input=stdin, capture_output=True, text=True,
        env=_env(root), check=False, start_new_session=True,
    )


def _home(root: Path, name: str, **raw) -> Path:
    path = root / "config" / "dusha" / name / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def _catalog(root: Path, stub: str) -> None:
    path = root / "cache" / "dusha" / "models-dev.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "stubai": {
            "id": "stubai", "name": "Stub AI", "npm": "@ai-sdk/openai-compatible", "api": f"{stub}/openai/v1",
            "env": ["STUBAI_API_KEY"],
            "models": {
                "chatty": {"id": "chatty"},
                "claudish": {
                    "id": "claudish", "provider": {"npm": "@ai-sdk/anthropic", "api": f"{stub}/anthropic/v1"},
                },
            },
        },
        "vaultai": {
            "id": "vaultai", "name": "Vault AI", "npm": "@ai-sdk/openai-compatible",
            "api": "https://${VAULT_ACCOUNT}.vault.invalid/v1", "env": ["VAULT_ACCOUNT", "VAULT_KEY"],
            "models": {},
        },
        "bedrocky": {"id": "bedrocky", "name": "Bedrocky", "npm": "@ai-sdk/amazon-bedrock", "models": {}},
    }), encoding="utf-8")


@pytest.fixture
def stub():
    # Stands in for an external model provider.
    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            headers = {key.lower(): value for key, value in self.headers.items()}
            seen.append({"path": self.path, "headers": headers})
            message = {"role": "assistant", "content": "ok"}
            body = json.dumps({"choices": [{"index": 0, "message": message}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", seen
    server.shutdown()
    server.server_close()


async def test_provider_add_saves_a_preset_the_proxy_then_uses(tmp_path, stub):
    url, seen = stub
    _catalog(tmp_path, url)
    path = _home(tmp_path, "nova", port=8765, api_token="local", proxy={"server": {"url": DEAD_SERVER}})
    added = _cli(tmp_path, "-c", "nova", "provider", "add", "stubai", stdin="sk-real-12345678\n")
    vault = _cli(tmp_path, "provider", "add", "vaultai", "--profile", "vault", stdin="acct\n\n")
    kept = path.read_text(encoding="utf-8")
    declined = _cli(tmp_path, "provider", "add", "stubai", stdin="n\n")
    unchanged = path.read_text(encoding="utf-8")
    listed = _cli(tmp_path, "provider", "list")
    removed = _cli(tmp_path, "provider", "remove", "vault")
    absent = _cli(tmp_path, "provider", "remove", "vault")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert added.returncode == 0, added.stderr
    assert "New provider stubai added to nova at http://127.0.0.1:8766/stubai." in added.stderr
    assert "Restart dusha proxy nova and dusha serve nova" in added.stderr
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert vault.returncode == 0, vault.stderr
    assert json.loads(kept)["providers"]["vault"] == {
        "base_url": "https://acct.vault.invalid/v1", "key": "{env:VAULT_KEY}", "auth": "bearer",
    }
    assert declined.returncode == 1 and unchanged == kept
    assert json.loads(listed.stdout)["stubai"] == {
        "base_url": f"{url}/openai/v1", "endpoints": {"messages": f"{url}/anthropic/v1"}, "auth": "bearer",
        "key": "...5678",
    }
    assert json.loads(listed.stdout)["vault"]["key"] == "{env:VAULT_KEY}"
    assert removed.returncode == 0 and absent.returncode == 1 and "No provider named 'vault'" in absent.stderr
    assert (saved["port"], saved["api_token"]) == (8765, "local")
    assert list(saved["providers"]) == ["stubai"] and "upstreams" not in saved["proxy"]

    app = create_proxy_app(settings.load(path))
    dummy = {"Authorization": "Bearer dummy", "x-api-key": "dummy"}
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as client:
            chat = await client.post(
                "/stubai/chat/completions", headers=dummy,
                json={"model": "chatty", "messages": [{"role": "user", "content": "Hi"}]},
            )
            claude = await client.post(
                "/stubai/v1/messages", headers=dummy,
                json={"model": "claudish", "max_tokens": 8, "messages": [{"role": "user", "content": "Hi"}]},
            )
    assert chat.status_code == claude.status_code == 200
    assert [item["path"] for item in seen] == ["/openai/v1/chat/completions", "/anthropic/v1/messages"]
    assert [item["headers"]["authorization"] for item in seen] == ["Bearer sk-real-12345678"] * 2
    assert all("x-api-key" not in item["headers"] for item in seen)


def test_provider_add_without_a_terminal_needs_a_known_provider(tmp_path, stub):
    _catalog(tmp_path, stub[0])
    _home(tmp_path, "nova", proxy={"server": {"url": DEAD_SERVER}})
    unnamed = _cli(tmp_path, "provider", "add")
    unknown = _cli(tmp_path, "provider", "add", "nope")
    blocked = _cli(tmp_path, "provider", "add", "bedrocky")
    missing = _cli(tmp_path, "-c", "luna", "provider", "add", "stubai")
    assert unnamed.returncode == 2 and "Name a provider" in unnamed.stderr
    assert unknown.returncode == 2 and "Unknown provider 'nope'" in unknown.stderr
    assert blocked.returncode == 2
    assert missing.returncode == 2 and "configuration file not found" in missing.stderr


def test_proxy_auth_still_works_with_a_deprecation_notice(tmp_path, stub):
    _catalog(tmp_path, stub[0])
    first = _cli(tmp_path, "proxy", "luna", "auth", "login", "stubai", stdin=f"{DEAD_SERVER}\n\nsk-luna\n")
    status = _cli(tmp_path, "proxy", "luna", "auth", "status")
    luna = json.loads((tmp_path / "config" / "dusha" / "luna" / "config.json").read_text(encoding="utf-8"))
    assert first.returncode == 0, first.stderr
    assert "Server URL" in first.stderr and "Use dusha provider" in first.stderr
    assert "Use dusha provider" in status.stderr and json.loads(status.stdout)["stubai"]["key"] == "..."
    assert luna["proxy"]["server"]["url"] == DEAD_SERVER
    assert luna["providers"]["stubai"]["key"] == "sk-luna"


class Terminal:
    def __init__(self, root: Path, *args: str):
        import pty

        self.fd, terminal = pty.openpty()
        self.child = subprocess.Popen(
            [sys.executable, "-m", "dusha.cli", *args], stdin=terminal, stdout=terminal, stderr=terminal,
            env=_env(root), start_new_session=True,
        )
        os.close(terminal)
        self.seen = self.output = b""

    def expect(self, text: str, timeout: float = 20) -> None:
        deadline = time.monotonic() + timeout
        while text.encode() not in self.seen:
            assert time.monotonic() < deadline, self.seen.decode(errors="replace")
            if select.select([self.fd], [], [], 0.1)[0]:
                try:
                    chunk = os.read(self.fd, 65536)
                except OSError:
                    break
                self.seen += chunk
                self.output += chunk
        self.seen = self.seen.split(text.encode(), 1)[1]

    def send(self, text: str) -> None:
        os.write(self.fd, text.encode())

    def close(self, *, wait: bool = False) -> int:
        if not wait:
            self.child.terminate()
        code = self.child.wait(timeout=20)
        os.close(self.fd)
        return code


needs_terminal = pytest.mark.skipif(sys.platform == "win32", reason="needs a pseudo-terminal")


@needs_terminal
def test_provider_add_offers_a_searchable_menu_in_a_terminal(tmp_path, stub):
    _catalog(tmp_path, stub[0])
    path = _home(tmp_path, "nova", proxy={"server": {"url": DEAD_SERVER}})
    terminal = Terminal(tmp_path, "provider", "add")
    terminal.expect("Stub AI")
    terminal.send("vault")
    terminal.expect("vault")
    terminal.send("\r")
    terminal.expect("VAULT_ACCOUNT")
    terminal.send("team\r")
    terminal.expect("API key")
    terminal.send("sk-typed\r")
    terminal.expect("Restart dusha proxy nova")
    hidden = b"sk-typed" not in terminal.output
    code = terminal.close(wait=True)
    profile = json.loads(path.read_text(encoding="utf-8"))["providers"]["vaultai"]
    assert code == 0 and hidden
    assert profile == {"base_url": "https://team.vault.invalid/v1", "key": "sk-typed", "auth": "bearer"}


@pytest.fixture
def serving(run_app):
    @contextmanager
    def start(path: Path):
        cfg = load_config(path)
        with run_app(api.create_app(cfg), port=cfg.port) as url:
            yield url

    return start


def _health(port: int) -> dict:
    deadline = time.monotonic() + 20
    while True:
        try:
            return httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).json()
        except httpx.HTTPError:
            assert time.monotonic() < deadline, "the proxy never answered"
            time.sleep(0.2)


@needs_terminal
def test_proxy_offers_the_server_on_this_machine_when_none_is_saved(tmp_path, free_port, serving):
    server_port, proxy_port = free_port(), free_port()
    path = _home(tmp_path, "nova", port=server_port, api_token="local-secret", proxy={"port": proxy_port})
    with serving(path) as url:
        terminal = Terminal(tmp_path, "proxy", "nova")
        terminal.expect("nova isn't connected to a server yet")
        terminal.expect(f"The server on this machine ({url})")
        terminal.send("\r")
        terminal.expect(f"Connected to {url}.")
        health = _health(proxy_port)
        terminal.close()
        again = Terminal(tmp_path, "proxy", "nova")
        again.expect("Application startup complete")
        rerun = _health(proxy_port)
        quiet = b"isn't connected" not in again.output
        again.close()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert health["server"] == {"url": url, "status": "ok"}
    assert rerun["server"]["status"] == "ok" and quiet
    assert saved["proxy"]["server"] == {"url": url, "token": ""}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@needs_terminal
def test_proxy_asks_for_another_server_until_it_answers(tmp_path, free_port, serving):
    server_port, proxy_port = free_port(), free_port()
    server = _home(tmp_path, "home", port=server_port, api_token="server-secret")
    path = _home(tmp_path, "edge", proxy={"port": proxy_port})
    with serving(server) as url:
        terminal = Terminal(tmp_path, "proxy", "edge")
        terminal.expect("edge isn't connected to a server yet")
        terminal.expect("Server URL")
        terminal.send("\x15" + url + "\r")
        terminal.expect("Server token")
        terminal.send("wrong\r")
        terminal.expect("What now?")
        terminal.send("\r")
        terminal.expect("Server URL")
        terminal.send("\r")
        terminal.expect("Server token")
        terminal.send("server-secret\r")
        terminal.expect(f"Connected to {url}.")
        health = _health(proxy_port)
        hidden = b"server-secret" not in terminal.output
        terminal.close()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert health["server"] == {"url": url, "status": "ok"}
    assert hidden
    assert saved["proxy"]["server"] == {"url": url, "token": "server-secret"}
