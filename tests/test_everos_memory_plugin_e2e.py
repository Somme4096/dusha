from __future__ import annotations

import asyncio
import json
import os
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

from dusha import api
from dusha.config import (
    AppConfig,
    EvergreenConfig,
    MemoryConfig,
    MemoryPluginConfig,
)


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _plugin_mods(root: Path, source: Path) -> Path:
    target = root / "mods"
    shutil.copytree(source / "everos-memory", target / "everos_memory", ignore=shutil.ignore_patterns(".venv"))
    return target


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
        try:
            self.wfile.write(encoded)
        except BrokenPipeError:
            return

    def log_message(self, format: str, *args: object) -> None:
        return


class _SidecarHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        server = self.server
        server.requests.append(self.path)
        length = int(self.headers.get("content-length", "0"))
        if length:
            self.rfile.read(length)
        if server.mode == "slow":
            time.sleep(2)
        if server.mode == "malformed":
            payload = {}
        else:
            payload = {"request_id": "external-stub", "data": {"message_count": 1, "status": "accumulated"}}
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except BrokenPipeError:
            return

    def log_message(self, format: str, *args: object) -> None:
        return


class _SelectiveSidecarHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length) if length else b"{}"
        request = json.loads(body)
        messages = request.get("messages", [])
        rejected = any("permanently rejected" in str(message.get("content", "")) for message in messages)
        if rejected:
            status = 400
            payload = {"error": "permanent rejection"}
        else:
            with httpx.Client(timeout=10, trust_env=False) as client:
                upstream = client.post(self.server.upstream + self.path, content=body, headers={"Content-Type": "application/json"})
            status = upstream.status_code
            payload = upstream.json()
        encoded = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except BrokenPipeError:
            return

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
def external_sidecar():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SidecarHandler)
    server.mode = "malformed"
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def selective_sidecar(everos_server):
    upstream, _, _, _ = everos_server
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SelectiveSidecarHandler)
    server.upstream = upstream
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def everos_server(tmp_path: Path, dummy_llm_server: str):
    executable = os.environ.get("EVEROS_EXECUTABLE", "")
    if not executable:
        pytest.skip("dedicated EverOS E2E requires EVEROS_EXECUTABLE")
    binary = Path(executable)
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


def _config(
    data_dir: Path,
    mods_dir: Path,
    sidecar_url: str,
    *,
    instance_namespace: str = "dusha-e2e",
    user_sender_id: str = "user-dusha-e2e",
    assistant_sender_id: str = "assistant-dusha-e2e",
    timeout_seconds: float = 1,
    gateway_timeout_seconds: float = 5,
) -> AppConfig:
    return AppConfig(
        data_dir=data_dir,
        memory=MemoryConfig(
            recent_messages=8,
            search_hits=8,
            context_messages=1,
            injection_max_chars=20_000,
        ),
        evergreen=EvergreenConfig(enabled=True, max_items=32, max_chars=4_000),
        memory_plugin=MemoryPluginConfig(
            module="everos_memory",
            mods_dir=str(mods_dir),
            timeout_seconds=gateway_timeout_seconds,
            ingest_batch_size=2,
            ingest_backfill_interval_seconds=1,
            options={
                "everos": {
                    "url": sidecar_url,
                    "app_id": "dusha-e2e",
                    "project_id": "phase1",
                    "instance_namespace": instance_namespace,
                    "user_sender_id": user_sender_id,
                    "assistant_sender_id": assistant_sender_id,
                    "timeout_seconds": timeout_seconds,
                }
            },
        ),
    )


@asynccontextmanager
async def _client(config: AppConfig):
    application = api.create_app(config)
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://dusha") as client:
            yield client


async def _ingest(client: httpx.AsyncClient, conversation: str, content: str, role: str = "user") -> dict:
    return await _ingest_with(client, conversation, content, role=role)


async def _ingest_with(
    client: httpx.AsyncClient,
    conversation: str,
    content: str,
    *,
    role: str = "user",
    external_id: str = "",
    occurred_at: str | None = None,
) -> dict:
    response = await client.post(
        "/state/v1/messages",
        json={
            "harness": "everos-e2e",
            "conversation_id": conversation,
            "role": role,
            "content": content,
            "external_id": external_id,
            "occurred_at": occurred_at,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _everos_search(url: str, session_id: str, query: str, user_id: str = "user-dusha-e2e") -> dict:
    async with httpx.AsyncClient(base_url=url, timeout=10, trust_env=False) as client:
        response = await client.post(
            "/api/v2/memory/search",
            json={
                "user_id": user_id,
                "app_id": "dusha-e2e",
                "project_id": "phase1",
                "query": query,
                "method": "keyword",
                "top_k": 10,
                "filters": {"session_id": session_id},
            },
        )
        assert response.status_code == 200, response.text
        return response.json()


async def _everos_status(client: httpx.AsyncClient) -> dict:
    response = await client.get("/state/v1/memory/index")
    assert response.status_code == 200, response.text
    return response.json()["everos"]


async def _wait_everos(client: httpx.AsyncClient, predicate) -> dict:
    status = await _everos_status(client)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if predicate(status):
            return status
        await asyncio.sleep(0.1)
        status = await _everos_status(client)
    return status


async def _pending_status(client: httpx.AsyncClient, expected: int = 0) -> dict:
    status = await _everos_status(client)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if status.get("pending") == expected:
            return status
        await asyncio.sleep(0.1)
        status = await _everos_status(client)
    return status


@pytest.mark.asyncio
async def test_real_everos_projection_failure_retry_isolation_and_restart(tmp_path: Path, everos_server, request):
    sidecar_url, _, stop_everos, restart_everos = everos_server
    mods_dir = Path(request.config.rootpath) / "examples" / "mods"
    plugin_mods = _plugin_mods(tmp_path, mods_dir)
    data_dir = tmp_path / "dusha-data"

    async with _client(_config(data_dir, plugin_mods, sidecar_url)) as client:
        first = await _ingest(client, "one", "Conversation one keeps the amber lantern.")
        second = await _ingest(client, "two", "Conversation two keeps the violet telescope.")
        assistant = await _ingest(client, "one", "Assistant records the copper answer.", role="assistant")
        tool = await _ingest(client, "one", "Tool output must stay local.", role="tool")
        duplicate_first = await _ingest_with(
            client, "one", "Duplicate source is retained.", external_id="duplicate-source"
        )
        duplicate_second = await _ingest_with(
            client, "one", "Duplicate source must not be mirrored twice.", external_id="duplicate-source"
        )
        newer = await _ingest_with(
            client, "history", "Historical newer event.", external_id="history-new", occurred_at="2024-01-01T00:00:00Z"
        )
        older = await _ingest_with(
            client, "history", "Historical older event.", external_id="history-old", occurred_at="2020-01-01T00:00:00Z"
        )
        collision_a = await _ingest_with(
            client, "collision", "First same-event record.", external_id="collision-a", occurred_at="2022-02-02T02:02:02Z"
        )
        collision_b = await _ingest_with(
            client, "collision", "Second same-event record.", external_id="collision-b", occurred_at="2022-02-02T02:02:02Z"
        )
        assert first["id"] != second["id"]
        assert duplicate_second["duplicate"] is True
        assert duplicate_second["id"] == duplicate_first["id"]
        assert collision_a["id"] != collision_b["id"]
        historical_times = (
            (newer, "2024-01-01T00:00:00Z"),
            (older, "2020-01-01T00:00:00Z"),
            (collision_a, "2022-02-02T02:02:02Z"),
            (collision_b, "2022-02-02T02:02:02Z"),
        )
        for message, expected_time in historical_times:
            persisted = await client.get(f"/state/v1/messages/{message['id']}")
            assert persisted.status_code == 200
            assert persisted.json()["occurred_at"].startswith(expected_time[:19])
        history_context = await client.post(
            "/state/v1/context", json={"harness": "everos-e2e", "conversation_id": "history", "query": ""}
        )
        assert history_context.status_code == 200
        history_text = [item["text"] for item in history_context.json()["records"]]
        assert history_text.index("Historical older event.") < history_text.index("Historical newer event.")
        delivered = await _pending_status(client, expected=0)
        assert delivered["enabled"] is True
        assert delivered["pending"] == 0

    restart_everos()
    session_one = f"dusha-e2e-dusha-{first['conversation_id']}"
    session_two = f"dusha-e2e-dusha-{second['conversation_id']}"
    one = await _everos_search(sidecar_url, session_one, "amber lantern")
    two = await _everos_search(sidecar_url, session_two, "amber lantern")
    assistant_search = await _everos_search(
        sidecar_url, session_one, "copper answer", user_id="assistant-dusha-e2e"
    )
    tool_search = await _everos_search(sidecar_url, session_one, "Tool output", user_id="user-dusha-e2e")
    assert "Conversation one keeps the amber lantern." in repr(one)
    assert "Conversation one keeps the amber lantern." not in repr(two)
    assert "Assistant records the copper answer." in repr(assistant_search)
    assert "Tool output must stay local." not in repr(one)
    assert "Tool output must stay local." not in repr(tool_search)

    stop_everos()
    async with _client(_config(data_dir, plugin_mods, sidecar_url)) as client:
        outage = await _ingest(client, "three", "Conversation three keeps the silver key.")
        assert outage["id"] > second["id"]
        failed_status = await _wait_everos(
            client, lambda value: value.get("pending") == 1 and value.get("attempts", 0) >= 1
        )
        assert failed_status["enabled"] is True
        assert failed_status["pending"] == 1
        assert failed_status["attempts"] >= 1
        local = await client.post("/state/v1/memory/search", json={"query": "amber lantern"})
        assert local.status_code == 200
        assert local.json()["results"]

    restart_everos()
    async with _client(_config(data_dir, plugin_mods, sidecar_url)) as client:
        retry = await _ingest(client, "one", "Conversation one adds a brass compass.")
        assert retry["id"] > outage["id"]
        recovered = await _pending_status(client, expected=0)
        assert recovered["enabled"] is True
        assert recovered["pending"] == 0
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
    session_three = f"dusha-e2e-dusha-{outage['conversation_id']}"
    one = await _everos_search(sidecar_url, session_one, "amber lantern")
    two = await _everos_search(sidecar_url, session_two, "amber lantern")
    three = await _everos_search(sidecar_url, session_three, "silver key")
    assert "Conversation one keeps the amber lantern." in repr(one)
    assert "Conversation one keeps the amber lantern." not in repr(two)
    assert "Conversation three keeps the silver key." in repr(three)


@pytest.mark.asyncio
async def test_real_everos_instance_namespaces_and_backfill_without_ingest(tmp_path: Path, everos_server, request):
    sidecar_url, _, stop_everos, restart_everos = everos_server
    plugin_mods = _plugin_mods(tmp_path, Path(request.config.rootpath) / "examples" / "mods")
    first_data = tmp_path / "instance-a"
    second_data = tmp_path / "instance-b"

    async with _client(
        _config(first_data, plugin_mods, sidecar_url, instance_namespace="instance-a", user_sender_id="user-a", assistant_sender_id="assistant-a")
    ) as client:
        alpha_message = await _ingest(client, "shared", "Only instance alpha owns this record.")
        await _pending_status(client, expected=0)
    async with _client(
        _config(second_data, plugin_mods, sidecar_url, instance_namespace="instance-b", user_sender_id="user-b", assistant_sender_id="assistant-b")
    ) as client:
        beta_message = await _ingest(client, "shared", "Only instance beta owns this record.")
        await _pending_status(client, expected=0)

    restart_everos()
    alpha_session = f"instance-a-dusha-{alpha_message['conversation_id']}"
    beta_session = f"instance-b-dusha-{beta_message['conversation_id']}"
    alpha = await _everos_search(sidecar_url, alpha_session, "instance alpha", user_id="user-a")
    beta = await _everos_search(sidecar_url, beta_session, "instance alpha", user_id="user-b")
    assert "Only instance alpha owns this record." in repr(alpha)
    assert "Only instance alpha owns this record." not in repr(beta)

    stop_everos()
    async with _client(_config(first_data, plugin_mods, sidecar_url, instance_namespace="instance-a", user_sender_id="user-a", assistant_sender_id="assistant-a")) as client:
        buffered_message = await _ingest(client, "shared", "Buffered alpha survives sidecar outage.")
        pending = await _wait_everos(client, lambda value: value.get("pending") == 1)
        assert pending["pending"] == 1
    restart_everos()
    async with _client(_config(first_data, plugin_mods, sidecar_url, instance_namespace="instance-a", user_sender_id="user-a", assistant_sender_id="assistant-a")) as client:
        recovered = await _pending_status(client)
        assert recovered["pending"] == 0
    buffered_session = f"instance-a-dusha-{buffered_message['conversation_id']}"
    buffered = await _everos_search(sidecar_url, buffered_session, "Buffered alpha", user_id="user-a")
    assert "Buffered alpha survives sidecar outage." in repr(buffered)


@pytest.mark.asyncio
async def test_external_malformed_and_slow_sidecars_keep_worker_alive(tmp_path: Path, external_sidecar, request):
    sidecar, sidecar_url = external_sidecar
    plugin_mods = _plugin_mods(tmp_path, Path(request.config.rootpath) / "examples" / "mods")
    data_dir = tmp_path / "dusha-data"

    async with _client(_config(data_dir, plugin_mods, sidecar_url, timeout_seconds=0.2, gateway_timeout_seconds=2)) as client:
        malformed = await _ingest(client, "malformed", "Malformed sidecar response remains pending.")
        status = await _wait_everos(
            client, lambda value: value.get("pending") == 1 and value.get("last_error")
        )
        assert status["pending"] == 1
        assert status["last_error"]
        assert malformed["duplicate"] is False
        sidecar.mode = "slow"
        slow = await _ingest(client, "slow", "Slow sidecar does not kill the worker.")
        status = await _wait_everos(client, lambda value: value.get("pending") == 2)
        assert status["pending"] == 2
        assert slow["duplicate"] is False
        assert (await client.get(f"/state/v1/messages/{slow['id']}")).status_code == 200

    sidecar.mode = "valid"
    recovered = None
    for _ in range(3):
        async with _client(_config(data_dir, plugin_mods, sidecar_url, timeout_seconds=0.2, gateway_timeout_seconds=2)) as client:
            recovered = await _pending_status(client)
        if recovered["pending"] == 0:
            break
    assert recovered is not None
    assert recovered["pending"] == 0
    assert recovered["enabled"] is True


@pytest.mark.asyncio
async def test_real_everos_rejection_does_not_block_later_delivery(tmp_path: Path, selective_sidecar, request):
    sidecar, sidecar_url = selective_sidecar
    real_url = sidecar.upstream
    plugin_mods = _plugin_mods(tmp_path, Path(request.config.rootpath) / "examples" / "mods")
    data_dir = tmp_path / "dusha-data"
    config = _config(data_dir, plugin_mods, sidecar_url, instance_namespace="rejection-e2e")

    async with _client(config) as client:
        rejected = await _ingest(client, "rejected", "This message is permanently rejected.")
        later = await _ingest(client, "later", "This later message is delivered.")
        status = await _wait_everos(client, lambda value: value.get("pending") == 1)
        assert status["pending"] == 1
        assert status["enabled"] is True

    async with _client(config) as client:
        status = await _pending_status(client, expected=1)
        assert status["pending"] == 1
        assert (await client.get(f"/state/v1/messages/{later['id']}")).status_code == 200

    later_session = f"rejection-e2e-dusha-{later['conversation_id']}"
    delivered = await _everos_search(real_url, later_session, "later message")
    rejected_search = await _everos_search(real_url, f"rejection-e2e-dusha-{rejected['conversation_id']}", "permanently rejected")
    assert "This later message is delivered." in repr(delivered)
    assert "This message is permanently rejected." not in repr(rejected_search)


@pytest.mark.asyncio
async def test_pre_epoch_message_is_local_but_not_projected(tmp_path: Path, everos_server, request):
    sidecar_url, _, _, _ = everos_server
    plugin_mods = _plugin_mods(tmp_path, Path(request.config.rootpath) / "examples" / "mods")
    data_dir = tmp_path / "dusha-data"
    async with _client(_config(data_dir, plugin_mods, sidecar_url)) as client:
        message = await _ingest_with(
            client,
            "pre-epoch",
            "This pre-1970 record stays in Dusha.",
            external_id="pre-epoch",
            occurred_at="1960-01-01T00:00:00Z",
        )
        persisted = await client.get(f"/state/v1/messages/{message['id']}")
        assert persisted.status_code == 200
        assert persisted.json()["text"] == "This pre-1970 record stays in Dusha."
        status = await _pending_status(client, expected=0)
        assert status["pending"] == 0
    search = await _everos_search(
        sidecar_url,
        f"dusha-e2e-dusha-{message['conversation_id']}",
        "pre-1970 record",
    )
    assert "This pre-1970 record stays in Dusha." not in repr(search)
