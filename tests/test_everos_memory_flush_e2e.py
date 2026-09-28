from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from companion_gateway import api
from companion_gateway.config import AppConfig, EmbeddingConfig, EvergreenConfig, MemoryConfig, MemoryPluginConfig


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _OpenAIStub(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        server = self.server
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length)) if length else {}
        server.calls.append(body)
        prompt = " ".join(str(item.get("content", "")) for item in body.get("messages", []))
        if "boundaries" in prompt.casefold() or "memcell" in prompt.casefold():
            content = json.dumps({"reasoning": "e2e", "boundaries": [], "should_wait": False})
        else:
            content = json.dumps({
                "title": "Flush E2E episode",
                "content": "The flush E2E episode contains the cobalt-orchid keyword.",
            })
        payload = {
            "id": "flush-e2e-completion",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model", "flush-e2e"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_: object) -> None:
        return


class _FlushDropProxy(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        server = self.server
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length) if length else b""
        if self.path.endswith("/memory/add"):
            server.add_calls += 1
        if self.path.endswith("/memory/flush"):
            server.flush_calls += 1
        with httpx.Client(timeout=10, trust_env=False) as client:
            response = client.post(
                server.upstream + self.path,
                content=body,
                headers={"Content-Type": self.headers.get("Content-Type", "application/json")},
            )
        if self.path.endswith("/memory/flush"):
            server.flush_responses.append((response.status_code, response.json()))
        if self.path.endswith("/memory/flush") and not server.dropped:
            server.dropped = True
            self.close_connection = True
            return
        self.send_response(response.status_code)
        self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(response.content)))
        self.end_headers()
        self.wfile.write(response.content)

    def log_message(self, *_: object) -> None:
        return


@pytest.fixture
def llm_stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAIStub)
    server.calls = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", server
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def everos_server(tmp_path: Path, llm_stub):
    executable = os.environ.get("EVEROS_EXECUTABLE", "")
    if not executable:
        pytest.skip("dedicated EverOS flush E2E requires EVEROS_EXECUTABLE")
    binary = Path(executable)
    if not binary.is_file():
        pytest.fail(f"EverOS executable is missing: {binary}")
    root = tmp_path / "everos-root"
    root.mkdir()
    llm_url, _ = llm_stub
    subprocess.run([str(binary), "init", "--root", str(root)], check=True, stdout=subprocess.DEVNULL)
    (root / "everos.toml").write_text(
        f'[llm]\nmodel = "flush-e2e"\napi_key = "loopback-only"\nbase_url = "{llm_url}"\n',
        encoding="utf-8",
    )
    port = _port()
    env = {**os.environ, "HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "config")}
    command = [str(binary), "server", "start", "--host", "127.0.0.1", "--port", str(port), "--root", str(root)]
    process = subprocess.Popen(command, cwd=binary.parents[2], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"

    def wait_ready() -> None:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail("EverOS exited during startup")
            try:
                if httpx.get(url + "/health", timeout=0.5, trust_env=False).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        pytest.fail("EverOS did not become healthy")

    def stop() -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)

    def restart() -> None:
        nonlocal process
        stop()
        process = subprocess.Popen(command, cwd=binary.parents[2], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_ready()

    wait_ready()
    try:
        yield url, root, stop, restart
    finally:
        stop()


@pytest.fixture
def flush_proxy(everos_server):
    upstream, _, _, _ = everos_server
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FlushDropProxy)
    server.upstream = upstream
    server.add_calls = 0
    server.flush_calls = 0
    server.flush_responses = []
    server.dropped = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}", upstream
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _config(data_dir: Path, mods_dir: Path, sidecar: str, *, enabled: bool) -> AppConfig:
    return AppConfig(
        data_dir=data_dir,
        memory=MemoryConfig(
            recent_messages=8,
            search_hits=8,
            context_messages=1,
            injection_max_chars=20_000,
            embedding=EmbeddingConfig(backfill_interval_seconds=1),
        ),
        evergreen=EvergreenConfig(enabled=True, max_items=32, max_chars=4_000),
        memory_plugin=MemoryPluginConfig(
            module="everos_memory",
            mods_dir=str(mods_dir),
            timeout_seconds=10,
            ingest_batch_size=2,
            ingest_backfill_interval_seconds=1,
            options={"everos": {
                "url": sidecar,
                "app_id": "sophia-e2e",
                "project_id": "phase2",
                "instance_namespace": "flush-e2e",
                "user_sender_id": "flush-user",
                "assistant_sender_id": "flush-assistant",
                "timeout_seconds": 5,
                "flush_after_ingest": enabled,
            }},
        ),
    )


@asynccontextmanager
async def _client(config: AppConfig):
    application = api.create_app(config)
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://sophia") as client:
            yield client


async def _add(client: httpx.AsyncClient, conversation: str, content: str, role: str = "user") -> dict:
    response = await client.post("/state/v1/messages", json={
        "harness": "everos-flush-e2e",
        "conversation_id": conversation,
        "role": role,
        "content": content,
    })
    assert response.status_code == 200, response.text
    return response.json()


async def _status(client: httpx.AsyncClient) -> dict:
    response = await client.get("/state/v1/memory/index")
    assert response.status_code == 200, response.text
    return response.json()["everos"]


async def _wait_status(client: httpx.AsyncClient, predicate) -> dict:
    result = await _status(client)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if predicate(result):
            return result
        await asyncio.sleep(0.1)
        result = await _status(client)
    return result


async def _search(url: str, session_id: str) -> dict:
    async with httpx.AsyncClient(base_url=url, timeout=10, trust_env=False) as client:
        response = await client.post("/api/v2/memory/search", json={
            "user_id": "flush-user",
            "app_id": "sophia-e2e",
            "project_id": "phase2",
            "query": "cobalt-orchid",
            "method": "keyword",
            "top_k": 10,
            "filters": {"session_id": session_id},
        })
        assert response.status_code == 200, response.text
        return response.json()


async def _wait_search(url: str, session_id: str) -> dict:
    deadline = time.monotonic() + 10
    result = await _search(url, session_id)
    while time.monotonic() < deadline:
        if result.get("data", {}).get("episodes"):
            return result
        await asyncio.sleep(0.1)
        result = await _search(url, session_id)
    return result


def _mods(request, tmp_path: Path) -> Path:
    import shutil
    target = tmp_path / "mods"
    shutil.copytree(Path(request.config.rootpath) / "examples" / "mods" / "everos-memory", target / "everos_memory", ignore=shutil.ignore_patterns(".venv"))
    return target


@pytest.mark.asyncio
async def test_flush_extracts_two_messages_and_preserves_session(tmp_path: Path, everos_server, llm_stub, request):
    sidecar, _, _, restart = everos_server
    data_dir = tmp_path / "sophia-data"
    async with _client(_config(data_dir, _mods(request, tmp_path), sidecar, enabled=True)) as client:
        first = await _add(client, "conversation", "Remember cobalt-orchid from the first message.")
        await _add(client, "conversation", "The second message confirms cobalt-orchid.", role="assistant")
        status = await _wait_status(client, lambda value: value.get("processed", 0) >= 2)
        assert status.get("processed", 0) >= 2
    restart()
    result = await _search(sidecar, "flush-e2e-sophia-" + str(first["conversation_id"]))
    episodes = result.get("data", {}).get("episodes", [])
    assert episodes
    assert any("cobalt-orchid" in repr(episode) for episode in episodes)
    assert all(episode.get("session_id") == "flush-e2e-sophia-" + str(first["conversation_id"]) for episode in episodes)


@pytest.mark.asyncio
async def test_flush_default_disabled_makes_no_llm_call(tmp_path: Path, everos_server, llm_stub, request):
    sidecar, _, _, _ = everos_server
    async with _client(_config(tmp_path / "data", _mods(request, tmp_path), sidecar, enabled=False)) as client:
        await _add(client, "disabled", "Buffered cobalt-orchid must not invoke extraction.")
        buffered = await _wait_status(client, lambda value: value.get("buffered", 0) >= 1)
        assert buffered.get("processed", 0) == 0
    assert llm_stub[1].calls == []


@pytest.mark.asyncio
async def test_flush_failure_restarts_without_readding_messages(tmp_path: Path, everos_server, llm_stub, request):
    sidecar, _, stop, restart = everos_server
    mods = _mods(request, tmp_path)
    data_dir = tmp_path / "data"
    stop()
    async with _client(_config(data_dir, mods, sidecar, enabled=True)) as client:
        await _add(client, "recovery", "Recovery stores cobalt-orchid across an EverOS outage.")
        failed = await _wait_status(client, lambda value: value.get("last_error"))
        assert failed.get("last_error")
        assert failed.get("processed", 0) == 0
    calls_before = len(llm_stub[1].calls)
    restart()
    async with _client(_config(data_dir, mods, sidecar, enabled=True)) as client:
        recovered = await _wait_status(client, lambda value: value.get("processed", 0) >= 1)
        assert recovered.get("processed", 0) >= 1
    assert len(llm_stub[1].calls) > calls_before


@pytest.mark.asyncio
async def test_dropped_flush_response_retries_without_readding_messages(tmp_path: Path, flush_proxy, request):
    proxy, proxy_url, real_url = flush_proxy
    mods = _mods(request, tmp_path)
    data_dir = tmp_path / "data"
    async with _client(_config(data_dir, mods, proxy_url, enabled=True)) as client:
        message = await _add(client, "dropped-response", "A dropped response still stores cobalt-orchid.")
        failed = await _wait_status(client, lambda value: proxy.flush_calls >= 1 and value.get("last_error"))
        assert proxy.flush_calls == 1
        assert failed.get("processed", 0) == 0
        assert proxy.add_calls == 1
    async with _client(_config(data_dir, mods, proxy_url, enabled=True)) as client:
        recovered = await _wait_status(client, lambda value: value.get("processed", 0) >= 1)
        assert recovered.get("processed", 0) >= 1
    assert proxy.add_calls == 1
    assert proxy.flush_calls >= 2
    assert [(code, body.get("data", {}).get("status")) for code, body in proxy.flush_responses[:2]] == [
        (200, "extracted"),
        (200, "no_extraction"),
    ]
    result = await _wait_search(real_url, "flush-e2e-sophia-" + str(message["conversation_id"]))
    assert any("cobalt-orchid" in repr(episode) for episode in result.get("data", {}).get("episodes", [])), result


@pytest.mark.asyncio
async def test_enabling_flush_processes_existing_buffer(tmp_path: Path, everos_server, request):
    sidecar, _, _, _ = everos_server
    mods = _mods(request, tmp_path)
    data_dir = tmp_path / "data"
    async with _client(_config(data_dir, mods, sidecar, enabled=False)) as client:
        message = await _add(client, "transition", "A buffered cobalt-orchid enters extraction after enabling flush.")
        buffered = await _wait_status(client, lambda value: value.get("buffered", 0) == 1)
        assert buffered.get("processed", 0) == 0
        assert buffered.get("buffered", 0) == 1
    async with _client(_config(data_dir, mods, sidecar, enabled=True)) as client:
        processed = await _wait_status(client, lambda value: value.get("processed", 0) >= 1)
        assert processed.get("processed", 0) >= 1
    result = await _wait_search(sidecar, "flush-e2e-sophia-" + str(message["conversation_id"]))
    assert any("cobalt-orchid" in repr(episode) for episode in result.get("data", {}).get("episodes", [])), result


@pytest.mark.asyncio
async def test_due_flush_and_pending_add_progress_together(tmp_path: Path, everos_server, request):
    sidecar, _, stop, restart = everos_server
    mods = _mods(request, tmp_path)
    data_dir = tmp_path / "data"
    async with _client(_config(data_dir, mods, sidecar, enabled=False)) as client:
        first = await _add(client, "mixed", "The first cobalt-orchid record waits for flush.")
    stop()
    async with _client(_config(data_dir, mods, sidecar, enabled=True)) as client:
        second = await _add(client, "mixed", "The second cobalt-orchid record waits for add delivery.", role="assistant")
        blocked = await _wait_status(client, lambda value: value.get("pending", 0) >= 1 and value.get("flush_pending", 0) >= 1)
        assert blocked.get("pending", 0) >= 1
        assert blocked.get("flush_pending", 0) >= 1
    restart()
    async with _client(_config(data_dir, mods, sidecar, enabled=True)) as client:
        progressed = await _wait_status(client, lambda value: value.get("pending", 0) == 0 and value.get("processed", 0) >= 2)
        assert progressed.get("pending", 0) == 0
        assert progressed.get("processed", 0) >= 2
    first_result = await _wait_search(sidecar, "flush-e2e-sophia-" + str(first["conversation_id"]))
    second_result = await _wait_search(sidecar, "flush-e2e-sophia-" + str(second["conversation_id"]))
    assert any("cobalt-orchid" in repr(episode) for episode in first_result.get("data", {}).get("episodes", []))
    assert any("cobalt-orchid" in repr(episode) for episode in second_result.get("data", {}).get("episodes", []))


@pytest.mark.asyncio
async def test_flush_context_projects_source_labeled_episode_and_falls_back(
    tmp_path: Path, everos_server, request
):
    sidecar, _, stop, _ = everos_server
    data_dir = tmp_path / "sophia-data"
    async with _client(_config(data_dir, _mods(request, tmp_path), sidecar, enabled=True)) as client:
        first = await _add(client, "context", "Remember cobalt-orchid from the context message.")
        await _add(client, "context", "The assistant confirms the cobalt-orchid context.", role="assistant")
        processed = await _wait_status(client, lambda value: value.get("processed", 0) >= 2)
        assert processed.get("processed", 0) >= 2
        await _wait_search(sidecar, "flush-e2e-sophia-" + str(first["conversation_id"]))

        response = await client.post(
            "/state/v1/context",
            json={"harness": "everos-flush-e2e", "conversation_id": "context", "query": "cobalt-orchid"},
        )
        assert response.status_code == 200, response.text
        plugin_context = response.json()["context"]["memory"].get("plugin_context")
        assert plugin_context is not None, response.text
        assert isinstance(plugin_context.get("source"), str) and plugin_context["source"]
        records = plugin_context.get("records") or []
        assert all(isinstance(item.get("source"), str) and item["source"] for item in records)
        blob = json.dumps(plugin_context)
        assert "keyword" in blob, blob
        assert "memory_id" not in blob, blob

        stop()
        fallback = await client.post(
            "/state/v1/context",
            json={"harness": "everos-flush-e2e", "conversation_id": "context", "query": "cobalt-orchid"},
        )
        assert fallback.status_code == 200, fallback.text
        fallback_body = fallback.json()
        assert "Remember cobalt-orchid from the context message." in fallback_body["injection"]
        assert any(
            "Remember cobalt-orchid from the context message." in item["text"]
            for item in fallback_body["records"]
        )
