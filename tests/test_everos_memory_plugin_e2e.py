from __future__ import annotations

import os
import json
import socket
import shutil
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from companion_gateway import api
from companion_gateway.config import AppConfig, EvergreenConfig, MemoryConfig, MemoryPluginConfig


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _DummyLLMHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        if length:
            self.rfile.read(length)
        payload = {
            "id": "dummy-completion",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "{}"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def dummy_llm_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DummyLLMHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def everos_server(tmp_path: Path, dummy_llm_server: str):
    binary = Path("/tmp/opencode/everos-source/.venv/bin/everos")
    if not binary.is_file():
        pytest.fail(f"EverOS executable is missing: {binary}")
    root = tmp_path / "everos-root"
    root.mkdir()
    subprocess.run([str(binary), "init", "--root", str(root)], check=True, stdout=subprocess.DEVNULL)
    (root / "everos.toml").write_text(
        f'[llm]\nmodel = "phase1-deferred"\napi_key = "phase1-no-call"\nbase_url = "{dummy_llm_server}"\n',
        encoding="utf-8",
    )
    port = _unused_port()
    env = {**os.environ, "HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "config")}
    command = [str(binary), "server", "start", "--host", "127.0.0.1", "--port", str(port), "--root", str(root)]

    def launch() -> subprocess.Popen:
        return subprocess.Popen(
            command,
            cwd=binary.parents[2],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    process = launch()
    url = f"http://127.0.0.1:{port}"

    def wait_ready() -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail("EverOS exited during startup")
            try:
                response = httpx.get(url + "/health", timeout=0.5, trust_env=False)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        pytest.fail("EverOS did not become healthy")

    wait_ready()

    def restart() -> None:
        nonlocal process
        stop()
        process = launch()
        wait_ready()

    def stop() -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)

    try:
        yield url, root, stop, restart
    finally:
        stop()


def _config(data_dir: Path, mods_dir: Path, sidecar_url: str) -> AppConfig:
    return AppConfig(
        data_dir=data_dir,
        memory=MemoryConfig(recent_messages=8, search_hits=8, context_messages=1, injection_max_chars=20_000),
        evergreen=EvergreenConfig(enabled=True, max_items=32, max_chars=4_000),
        memory_plugin=MemoryPluginConfig(
            module="everos_memory",
            mods_dir=str(mods_dir),
            options={
                "everos": {
                    "url": sidecar_url,
                    "app_id": "sophia-e2e",
                    "project_id": "phase1",
                    "sender_id": "sophia-e2e",
                    "timeout_seconds": 1,
                }
            },
        ),
    )


@asynccontextmanager
async def _client(config: AppConfig):
    application = api.create_app(config)
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://sophia") as client:
            yield client


async def _ingest(client: httpx.AsyncClient, conversation: str, content: str, role: str = "user") -> dict:
    response = await client.post(
        "/state/v1/messages",
        json={"harness": "everos-e2e", "conversation_id": conversation, "role": role, "content": content},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _everos_search(url: str, session_id: str, query: str) -> dict:
    async with httpx.AsyncClient(base_url=url, timeout=10, trust_env=False) as client:
        response = await client.post(
            "/api/v2/memory/search",
            json={
                "user_id": "sophia-e2e",
                "app_id": "sophia-e2e",
                "project_id": "phase1",
                "query": query,
                "method": "keyword",
                "top_k": 10,
                "filters": {"session_id": session_id},
            },
        )
        assert response.status_code == 200, response.text
        return response.json()


@pytest.mark.asyncio
async def test_real_everos_projection_failure_retry_isolation_and_restart(tmp_path: Path, everos_server, request):
    sidecar_url, _, stop_everos, restart_everos = everos_server
    mods_dir = Path(request.config.rootpath) / "examples" / "mods"
    plugin_mods = tmp_path / "mods"
    shutil.copytree(mods_dir / "everos-memory", plugin_mods / "everos_memory", ignore=shutil.ignore_patterns(".venv"))
    data_dir = tmp_path / "sophia-data"

    async with _client(_config(data_dir, plugin_mods, sidecar_url)) as client:
        first = await _ingest(client, "one", "Conversation one keeps the amber lantern.")
        second = await _ingest(client, "two", "Conversation two keeps the violet telescope.")
        assert first["id"] != second["id"]
        delivered = (await client.get("/state/v1/memory/index")).json()
        assert delivered["everos"]["enabled"] is True
        assert delivered["everos"]["pending"] == 0

    restart_everos()
    one = await _everos_search(sidecar_url, "sophia-1", "amber lantern")
    two = await _everos_search(sidecar_url, "sophia-2", "amber lantern")
    assert "Conversation one keeps the amber lantern." in repr(one)
    assert "Conversation one keeps the amber lantern." not in repr(two)

    stop_everos()
    async with _client(_config(data_dir, plugin_mods, sidecar_url)) as client:
        outage = await _ingest(client, "three", "Conversation three keeps the silver key.")
        assert outage["id"] > second["id"]
        failed_status = (await client.get("/state/v1/memory/index")).json()
        assert failed_status["everos"]["enabled"] is True
        assert failed_status["everos"]["pending"] == 1
        assert failed_status["everos"]["attempts"] >= 1
        local = await client.post("/state/v1/memory/search", json={"query": "amber lantern"})
        assert local.status_code == 200
        assert local.json()["results"]

    restart_everos()
    async with _client(_config(data_dir, plugin_mods, sidecar_url)) as client:
        retry = await _ingest(client, "one", "Conversation one adds a brass compass.")
        assert retry["id"] > outage["id"]
        recovered = (await client.get("/state/v1/memory/index")).json()
        assert recovered["everos"]["enabled"] is True
        assert recovered["everos"]["pending"] == 0
        persisted = await client.get(f"/state/v1/messages/{first['id']}")
        assert persisted.status_code == 200
        assert persisted.json()["text"] == "Conversation one keeps the amber lantern."
        context = await client.post(
            "/state/v1/context", json={"harness": "everos-e2e", "conversation_id": "one", "query": ""}
        )
        assert context.status_code == 200
        context_text = [item["text"] for item in context.json()["records"]]
        assert "Conversation one keeps the amber lantern." in context_text
        assert "Conversation two keeps the violet telescope." not in context_text

        fact = await client.post(
            "/state/v1/evergreen/facts",
            json={"key": "user.home", "text": "The home is the amber harbor."},
        )
        assert fact.status_code == 200
        fact_id = fact.json()["fact"]["fact_id"]
        duplicate = await client.post(
            "/state/v1/evergreen/facts",
            json={"key": "user.home", "text": "A conflicting home."},
        )
        assert duplicate.status_code == 409
        revised = await client.post(
            f"/state/v1/evergreen/facts/{fact_id}/revisions",
            json={"expected_revision": 1, "text": "The home is the brass harbor."},
        )
        assert revised.status_code == 200
        forgotten = await client.post(
            f"/state/v1/evergreen/facts/{fact_id}/forget",
            json={"expected_revision": 2, "reason": "phase one lifecycle"},
        )
        assert forgotten.status_code == 200

    async with _client(_config(data_dir, plugin_mods, sidecar_url)) as client:
        after_restart = await client.get(f"/state/v1/messages/{retry['id']}")
        assert after_restart.status_code == 200
        assert after_restart.json()["text"] == "Conversation one adds a brass compass."
        facts = await client.get("/state/v1/evergreen/facts")
        assert facts.status_code == 200
        assert all(item["fact_id"] != fact_id for item in facts.json()["facts"])

    restart_everos()
    one = await _everos_search(sidecar_url, "sophia-1", "amber lantern")
    two = await _everos_search(sidecar_url, "sophia-2", "amber lantern")
    three = await _everos_search(sidecar_url, "sophia-3", "silver key")
    assert "Conversation one keeps the amber lantern." in repr(one)
    assert "Conversation one keeps the amber lantern." not in repr(two)
    assert "Conversation three keeps the silver key." in repr(three)
