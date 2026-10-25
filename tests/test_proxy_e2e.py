from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import uvicorn

from dusha import api
from dusha.config import load_proxy_config
from dusha.proxy import create_proxy_app

STREAM_CHUNKS = [
    b'data: {"choices":[{"delta":{"content":"streamed "}}]}\n\n',
    b'data: {"choices":[{"delta":{"content":"reply"}}]}\n\n',
    b"data: [DONE]\n\n",
]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def _running(app):
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(10)


class Provider:
    def __init__(self):
        self.seen: list[dict] = []
        self.reply: dict = {"role": "assistant", "content": "stub reply"}
        self.status = 200
        self.host = ""


@pytest.fixture
def provider():
    # Stands in for an external chat provider.
    stub = Provider()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, payload, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _record(self, body):
            headers = {key.lower(): value for key, value in self.headers.items()}
            stub.seen.append({"method": self.command, "path": self.path, "headers": headers, "body": body})

        def do_GET(self):
            self._record(None)
            self._send(200, b'{"data":[{"id":"stub-model"}]}', "application/json")

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self._record(body)
            if stub.status >= 400:
                self._send(stub.status, b"raw upstream error body", "text/plain")
            elif body.get("stream"):
                self._send(200, b"".join(STREAM_CHUNKS), "text/event-stream")
            elif self.path.endswith("/messages"):
                self._send(200, b'{"type":"message"}', "application/json")
            else:
                reply = {"choices": [{"index": 0, "message": stub.reply}]}
                self._send(200, json.dumps(reply).encode(), "application/json")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stub.host = f"127.0.0.1:{server.server_address[1]}"
    yield stub
    server.shutdown()
    server.server_close()


@pytest.fixture
def server(config):
    with _running(api.create_app(config)) as url:
        yield url


def _proxy_config(tmp_path: Path, raw: dict):
    path = tmp_path / "proxy" / "proxy.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw), encoding="utf-8")
    return load_proxy_config(path)


@asynccontextmanager
async def _proxy(tmp_path, server_url, provider, *, token="", base="v1", headers=None):
    upstream = {"base_url": f"http://{provider.host}/{base}".rstrip("/"), "headers": headers or {}}
    cfg = _proxy_config(
        tmp_path, {"server": {"url": server_url, "token": token}, "upstreams": {"stub": upstream}}
    )
    app = create_proxy_app(cfg)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as client:
            yield client


def _stored(server_url, token=""):
    headers = {"X-Companion-Token": token} if token else {}
    messages = []
    for message_id in range(1, 50):
        response = httpx.get(f"{server_url}/state/v1/messages/{message_id}", headers=headers)
        if response.status_code == 404:
            break
        messages.append(response.json())
    return messages


def _chat(client, messages, headers=None, path="/stub/chat/completions", **extra):
    return client.post(path, json={"model": "any-model", "messages": messages, **extra}, headers=headers)


async def test_proxy_carries_a_conversation_through_the_server_to_the_profile(
    tmp_path, server, provider, monkeypatch
):
    monkeypatch.setenv("STUB_KEY", "profile-key")
    profile_headers = {"x-session": "s-{conversation_id}", "x-key": "{env:STUB_KEY}"}
    routing = {"X-Conversation-Id": "portable-thread", "X-Harness": "test-harness", "X-Companion-Route": "r"}
    caller = {**routing, "Authorization": "Bearer caller-token"}
    async with _proxy(tmp_path, server, provider, headers=profile_headers) as client:
        provider.reply = {"role": "assistant", "content": "First reply."}
        first = await _chat(
            client,
            [{"role": "system", "content": "caller system"}, {"role": "user", "content": "Hello."}],
            caller,
            path="/stub/chat/completions?api-version=7",
            custom_top_level_field={"keep": "me"},
        )
        provider.reply = {"role": "assistant", "content": "Second reply."}
        second = await _chat(
            client,
            [
                {"role": "user", "content": "Hello."},
                {"role": "assistant", "content": "First reply."},
                {"role": "user", "content": "Remember the amber window.", "custom_message_field": "kept"},
            ],
            caller,
        )
    assert (first.status_code, second.status_code) == (200, 200)
    assert first.json()["choices"][0]["message"]["content"] == "First reply."
    assert [item["path"] for item in provider.seen] == [
        "/v1/chat/completions?api-version=7",
        "/v1/chat/completions",
    ]
    for item in provider.seen:
        assert item["headers"]["authorization"] == "Bearer caller-token"
        assert item["headers"]["x-session"] == "s-portable-thread"
        assert item["headers"]["x-key"] == "profile-key"
        assert not {name.lower() for name in routing} & set(item["headers"])
    opening = provider.seen[0]["body"]
    assert opening["custom_top_level_field"] == {"keep": "me"}
    assert [message["role"] for message in opening["messages"]] == ["system", "system", "user"]
    assert "<companion_state>" in opening["messages"][1]["content"]
    assert provider.seen[1]["body"]["messages"][-1]["custom_message_field"] == "kept"
    stored = _stored(server)
    assert [message["text"] for message in stored] == [
        "Hello.", "First reply.", "Remember the amber window.", "Second reply.",
    ]
    assert {message["harness"] for message in stored} == {"test-harness"}
    assert {message["route"] for message in stored} == {"r"}


async def test_proxy_replay_dedups_and_names_the_harness_after_the_profile(tmp_path, server, provider):
    transcript = [
        {"role": "system", "content": "ignored"},
        {"role": "user", "content": "Hello."},
        {"role": "assistant", "content": "A reply."},
        {"role": "user", "content": "Remember the amber window."},
    ]
    async with _proxy(tmp_path, server, provider) as client:
        first = await _chat(client, transcript)
        replay = await _chat(client, transcript)
    assert first.status_code == replay.status_code == 200
    stored = _stored(server)
    assert [message["text"] for message in stored] == [
        "Hello.", "A reply.", "Remember the amber window.", "stub reply",
    ]
    assert {message["harness"] for message in stored} == {"stub"}


async def test_proxy_streams_unchanged_and_archives_the_streamed_reply(tmp_path, server, provider):
    async with _proxy(tmp_path, server, provider) as client:
        response = await _chat(client, [{"role": "user", "content": "Stream it."}], stream=True)
    assert response.status_code == 200
    assert response.content == b"".join(STREAM_CHUNKS)
    assert response.headers["content-type"].startswith("text/event-stream")
    stored = _stored(server)
    assert [(message["role"], message["text"]) for message in stored] == [
        ("user", "Stream it."), ("assistant", "streamed reply"),
    ]
    assert stored[-1]["external_id"].startswith("proxy:")


async def test_proxy_archives_a_tool_call_reply_as_sent(tmp_path, server, provider):
    tool_message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": '{"k":"v"}'}}
        ],
    }
    provider.reply = tool_message
    async with _proxy(tmp_path, server, provider) as client:
        response = await _chat(client, [{"role": "user", "content": "Look it up."}])
    assert response.status_code == 200
    stored = _stored(server)
    assert stored[-1]["content"] == tool_message
    assert '"name":"lookup"' in stored[-1]["text"]


async def test_proxy_passes_other_paths_through_verbatim(tmp_path, server, provider):
    async with _proxy(tmp_path, server, provider, base="") as client:
        models = await client.get("/stub/v1/models?page=2", headers={"Authorization": "Bearer k"})
        messages = await client.post(
            "/stub/v1/messages", json={"model": "m", "messages": [{"role": "user", "content": "Raw."}]}
        )
    assert models.json() == {"data": [{"id": "stub-model"}]}
    assert messages.json() == {"type": "message"}
    assert [item["path"] for item in provider.seen] == ["/v1/models?page=2", "/v1/messages"]
    assert provider.seen[0]["headers"]["authorization"] == "Bearer k"
    assert provider.seen[1]["body"]["messages"] == [{"role": "user", "content": "Raw."}]
    assert _stored(server) == []


async def test_proxy_reports_unknown_profiles_and_passes_upstream_errors(tmp_path, server, provider):
    provider.status = 503
    async with _proxy(tmp_path, server, provider) as client:
        unknown = await _chat(client, [{"role": "user", "content": "Hi"}], path="/nowhere/chat/completions")
        failed = await _chat(client, [{"role": "user", "content": "Hi"}])
        invalid = await client.post("/stub/chat/completions", content=b"not json")
    assert unknown.status_code == 404
    assert failed.status_code == 503
    assert failed.content == b"raw upstream error body"
    assert failed.headers["content-type"].startswith("text/plain")
    assert invalid.status_code == 422
    assert [message["text"] for message in _stored(server)] == ["Hi"]


@pytest.mark.parametrize("stream", [False, True])
async def test_proxy_maps_an_unreachable_upstream_to_502(tmp_path, server, stream):
    dead = Provider()
    dead.host = f"127.0.0.1:{_free_port()}"
    async with _proxy(tmp_path, server, dead) as client:
        responses = [
            await client.get("/stub/models"),
            await _chat(client, [{"role": "user", "content": "Anyone?"}], stream=stream),
        ]
    assert [response.status_code for response in responses] == [502, 502]
    assert all(response.json()["detail"].startswith("upstream request failed: ") for response in responses)


async def test_proxy_forwards_without_context_while_the_server_is_down(tmp_path, provider):
    async with _proxy(tmp_path, f"http://127.0.0.1:{_free_port()}", provider) as client:
        health = await client.get("/health")
        response = await _chat(client, [{"role": "user", "content": "Still there?"}])
    assert health.json()["server"]["status"] == "unreachable"
    assert health.json()["upstreams"] == ["stub"]
    assert response.status_code == 200
    assert provider.seen[0]["body"]["messages"] == [{"role": "user", "content": "Still there?"}]


async def test_proxy_sends_the_server_token_itself(tmp_path, config, provider):
    config.api_token = "server-secret"
    with _running(api.create_app(config)) as url:
        async with _proxy(tmp_path, url, provider, token="server-secret") as client:
            health = await client.get("/health")
            await _chat(client, [{"role": "user", "content": "Authorized."}])
        async with _proxy(tmp_path, url, provider, token="wrong") as client:
            denied = await client.get("/health")
            unauthorized = await _chat(client, [{"role": "user", "content": "Denied."}])
        stored = [message["text"] for message in _stored(url, "server-secret")]
    assert health.json()["server"]["status"] == "ok"
    assert denied.json()["server"]["status"] == "unauthorized"
    assert unauthorized.status_code == 200
    assert stored == ["Authorized.", "stub reply"]
    assert "<companion_state>" in provider.seen[0]["body"]["messages"][0]["content"]
    assert provider.seen[1]["body"]["messages"] == [{"role": "user", "content": "Denied."}]
    assert all("x-companion-token" not in item["headers"] for item in provider.seen)


@pytest.mark.parametrize(
    ("raw", "pattern"),
    [
        ({"host": "0.0.0.0"}, "host must be a loopback address"),
        ({"bogus": 1}, r"unknown top-level field\(s\)"),
        ({"server": {"url": "remote:8765"}}, "server.url must be an http or https URL"),
        ({"upstreams": {"health": {"base_url": "https://a.invalid"}}}, "upstream name must be"),
        ({"upstreams": {"a": {"base_url": "a.invalid/v1"}}}, "upstreams.a.base_url must be an http"),
        ({"upstreams": {"a": {"base_url": "https://u:p@a.invalid"}}}, "must not carry credentials"),
        ({"upstreams": {"a": {"base_url": "https://a.invalid", "timeout_seconds": 0}}}, "above 0"),
        ({"upstreams": {"a": {"base_url": "https://a.invalid", "headers": {"x": "{session}"}}}},
         r"unknown placeholder \{session\}"),
        ({"upstreams": {"a": {"base_url": "https://a.invalid", "headers": {"x": "{env:DUSHA_NOPE}"}}}},
         "needs the environment variable DUSHA_NOPE"),
        ({"upstreams": {"a": {"base_url": "https://a.invalid", "region": "eu"}}},
         r"unknown field\(s\) under upstreams.a"),
    ],
)
def test_proxy_config_rejects_invalid_files(tmp_path, raw, pattern):
    with pytest.raises(ValueError, match=pattern):
        _proxy_config(tmp_path, {"server": {"url": "http://127.0.0.1:8765"}, **raw})


def test_proxy_config_finds_a_local_server_beside_it(tmp_path):
    home = tmp_path / "proxy"
    home.mkdir()
    with pytest.raises(ValueError, match="server.url is empty and no config.json"):
        _proxy_config(tmp_path, {})
    (home / "config.json").write_text(
        json.dumps({"host": "0.0.0.0", "port": 9123, "api_token": "local-secret"}), encoding="utf-8"
    )
    cfg = _proxy_config(tmp_path, {})
    assert (cfg.server.url, cfg.server.token) == ("http://127.0.0.1:9123", "local-secret")
    assert (cfg.host, cfg.port) == ("127.0.0.1", 8766)
