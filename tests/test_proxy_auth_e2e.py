from __future__ import annotations

import json
import os
import select
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from dusha.client import settings
from dusha.client.proxy import create_proxy_app

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


async def test_auth_login_saves_a_preset_the_proxy_then_uses(tmp_path, stub):
    url, seen = stub
    _catalog(tmp_path, url)
    path = _home(tmp_path, "nova", port=8765, api_token="local", proxy={"server": {"url": DEAD_SERVER}})
    added = _cli(tmp_path, "proxy", "nova", "auth", "login", "stubai", stdin="sk-real-12345678\n")
    vault = _cli(
        tmp_path, "proxy", "nova", "auth", "login", "vaultai", "--profile", "vault", stdin="acct\n\n"
    )
    kept = path.read_text(encoding="utf-8")
    declined = _cli(tmp_path, "proxy", "nova", "auth", "login", "stubai", stdin="n\n")
    unchanged = path.read_text(encoding="utf-8")
    status = _cli(tmp_path, "proxy", "nova", "auth", "status")
    removed = _cli(tmp_path, "proxy", "nova", "auth", "logout", "vault")
    absent = _cli(tmp_path, "proxy", "nova", "auth", "logout", "vault")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert added.returncode == 0, added.stderr
    assert "added stubai to nova: http://127.0.0.1:8766/stubai" in added.stderr
    assert "restart dusha proxy nova" in added.stderr
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert vault.returncode == 0, vault.stderr
    assert json.loads(kept)["proxy"]["upstreams"]["vault"] == {
        "base_url": "https://acct.vault.invalid/v1", "key": "{env:VAULT_KEY}", "auth": "bearer",
    }
    assert declined.returncode == 1 and unchanged == kept
    assert json.loads(status.stdout)["stubai"] == {
        "base_url": f"{url}/openai/v1", "endpoints": {"messages": f"{url}/anthropic/v1"}, "auth": "bearer",
        "key": "...5678",
    }
    assert json.loads(status.stdout)["vault"]["key"] == "{env:VAULT_KEY}"
    assert removed.returncode == 0 and absent.returncode == 1 and "no profile named 'vault'" in absent.stderr
    assert (saved["port"], saved["api_token"]) == (8765, "local")
    assert list(saved["proxy"]["upstreams"]) == ["stubai"]

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


def test_auth_login_without_a_terminal_needs_a_known_provider(tmp_path, stub):
    _catalog(tmp_path, stub[0])
    _home(tmp_path, "nova", proxy={"server": {"url": DEAD_SERVER}})
    unnamed = _cli(tmp_path, "proxy", "auth", "login")
    unknown = _cli(tmp_path, "proxy", "auth", "login", "nope")
    blocked = _cli(tmp_path, "proxy", "auth", "login", "bedrocky")
    first = _cli(tmp_path, "proxy", "luna", "auth", "login", "stubai", stdin=f"{DEAD_SERVER}\n\nsk-luna\n")
    luna = json.loads((tmp_path / "config" / "dusha" / "luna" / "config.json").read_text(encoding="utf-8"))
    assert unnamed.returncode == 2 and "name a provider" in unnamed.stderr
    assert unknown.returncode == 2 and "unknown provider 'nope'" in unknown.stderr
    assert blocked.returncode == 2
    assert first.returncode == 0, first.stderr
    assert "Server URL" in first.stderr
    assert luna["proxy"]["server"]["url"] == DEAD_SERVER
    assert luna["proxy"]["upstreams"]["stubai"]["key"] == "sk-luna"


@pytest.mark.skipif(sys.platform == "win32", reason="needs a pseudo-terminal")
def test_auth_login_offers_a_searchable_menu_in_a_terminal(tmp_path, stub):
    import pty

    _catalog(tmp_path, stub[0])
    path = _home(tmp_path, "nova", proxy={"server": {"url": DEAD_SERVER}})
    fd, terminal = pty.openpty()
    child = subprocess.Popen(
        [sys.executable, "-m", "dusha.cli", "proxy", "auth", "login"], stdin=terminal, stdout=terminal,
        stderr=terminal, env=_env(tmp_path), start_new_session=True,
    )
    os.close(terminal)

    def read_until(text: bytes, timeout: float = 20) -> bytes:
        seen, deadline = b"", time.monotonic() + timeout
        while text not in seen:
            assert time.monotonic() < deadline, seen.decode(errors="replace")
            if select.select([fd], [], [], 0.1)[0]:
                try:
                    seen += os.read(fd, 65536)
                except OSError:
                    break
        return seen

    read_until(b"Stub AI")
    os.write(fd, b"vault")
    read_until(b"vault")
    os.write(fd, b"\r")
    read_until(b"VAULT_ACCOUNT")
    os.write(fd, b"team\r")
    read_until(b"API key")
    os.write(fd, b"sk-typed\r")
    output = read_until(b"restart dusha proxy nova")
    code = child.wait(timeout=20)
    os.close(fd)
    profile = json.loads(path.read_text(encoding="utf-8"))["proxy"]["upstreams"]["vaultai"]
    assert code == 0
    assert b"sk-typed" not in output
    assert profile == {"base_url": "https://team.vault.invalid/v1", "key": "sk-typed", "auth": "bearer"}
