from __future__ import annotations

import json

import httpx
import pytest

from dusha import api
from dusha.config import PluginEntry

ACCEPT = {"Accept": "application/json, text/event-stream"}


class Session:
    def __init__(self, url: str, token: str = ""):
        headers = {**ACCEPT, **({"X-Companion-Token": token} if token else {})}
        self.client = httpx.Client(base_url=url, headers=headers, timeout=30)
        self.sent = 0

    def post(self, body, **headers) -> httpx.Response:
        return self.client.post("/mcp", content=json.dumps(body), headers=headers)

    def request(self, method: str, params: dict | None = None, **headers) -> dict:
        self.sent += 1
        message = {"jsonrpc": "2.0", "id": self.sent, "method": method, "params": params or {}}
        response = self.post(message, **headers)
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("application/json")
        reply = response.json()
        assert reply["id"] == self.sent
        return reply

    def call(self, name: str, **arguments) -> tuple[dict, bool]:
        result = self.request("tools/call", {"name": name, "arguments": arguments})["result"]
        return json.loads(result["content"][0]["text"]), result["isError"]


@pytest.fixture
def session_for(run_app):
    opened = []

    def start(config, token: str = "") -> Session:
        context = run_app(api.create_app(config))
        url = context.__enter__()
        opened.append(context)
        return Session(url, token)

    yield start
    for context in opened:
        context.__exit__(None, None, None)


def test_mcp_serves_the_companion_tools_over_http(config, session_for):
    session = session_for(config)
    stored = session.client.post("/state/v1/messages", json={"role": "user", "content": "The amber window."})
    opened = session.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    initialized = session.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
    tools = session.request("tools/list", **{"MCP-Protocol-Version": "2025-06-18"})["result"]["tools"]
    memo, memo_failed = session.call("memo_add", text="Water the fern.", ignored="extra")
    memos, _ = session.call("memo_list")
    fact, _ = session.call("evergreen_remember", key="user.name", text="The user is Birch.")
    facts, _ = session.call("evergreen_list")
    stale, stale_failed = session.call(
        "evergreen_revise", fact_id=fact["fact"]["fact_id"], expected_revision=99, text="Changed."
    )
    clash, clash_failed = session.call(
        "evergreen_revise", fact_id=fact["fact"]["fact_id"], expected_revision=1, text="Changed.",
        expires_at="2030-01-01T00:00:00+00:00", clear_expires_at=True,
    )
    revised, _ = session.call(
        "evergreen_revise", fact_id=fact["fact"]["fact_id"], expected_revision=1,
        text="The user is Birch Tree.",
    )
    found, _ = session.call("memory_search", query="amber window")
    record, _ = session.call("memory_get", message_id=stored.json()["id"])
    absent_record, absent_failed = session.call("memory_get", message_id=999999)
    affect, affect_failed = session.call("affect_status")
    missing, missing_failed = session.call("memo_done", note_id=memo["memo"]["id"])
    wrong, wrong_failed = session.call("memo_done", note_id="x", reason="r")
    unknown = session.request("tools/call", {"name": "nope", "arguments": {}})
    absent = session.request("resources/list")
    ping = session.request("ping")
    garbled = session.client.post("/mcp", content="{not json")
    assert opened["result"]["protocolVersion"] == "2025-06-18"
    assert opened["result"]["serverInfo"]["name"] == "dusha"
    assert opened["result"]["capabilities"] == {"tools": {}}
    assert initialized.status_code == 202 and initialized.content == b""
    assert {tool["name"] for tool in tools} == {
        "memory_search", "memory_get", "evergreen_list", "evergreen_remember", "evergreen_revise",
        "evergreen_forget", "memo_add", "memo_list", "memo_done", "affect_status",
    }
    assert all(tool["inputSchema"]["type"] == "object" for tool in tools)
    memo_list = next(tool for tool in tools if tool["name"] == "memo_list")["inputSchema"]
    assert memo_list["properties"]["status"]["enum"] == ["active", "archived"]
    assert memo_list["required"] == []
    assert not memo_failed and memo["memo"]["text"] == "Water the fern."
    assert [item["text"] for item in memos["memos"]] == ["Water the fern."]
    assert [item["key"] for item in facts["facts"]] == ["user.name"]
    assert fact["fact"]["created_by"] == "agent"
    assert stale_failed and "revision" in stale["detail"]
    assert clash_failed and "cannot be used together" in clash["detail"]
    assert revised["fact"]["text"] == "The user is Birch Tree."
    assert "The amber window." in json.dumps(found)
    assert record["messages"][0]["text"] == "The amber window."
    assert absent_failed and absent_record == {"detail": "memory record not found"}
    assert not affect_failed and "mood" in affect
    assert missing_failed and missing == {"detail": "missing argument(s): ['reason']"}
    assert wrong_failed and wrong == {"detail": "note_id must be int"}
    assert unknown["error"]["code"] == -32602
    assert absent["error"]["code"] == -32601
    assert ping["result"] == {}
    assert garbled.status_code == 400 and garbled.json()["error"]["code"] == -32700


def test_mcp_answers_a_batch_and_negotiates_the_protocol(config, session_for):
    session = session_for(config)
    batch = session.post([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
    ])
    only_notes = session.post([{"jsonrpc": "2.0", "method": "notifications/initialized"}])
    unsupported = session.post(
        {"jsonrpc": "2.0", "id": 3, "method": "ping"}, **{"MCP-Protocol-Version": "1999-01-01"}
    )
    foreign = session.post({"jsonrpc": "2.0", "id": 4, "method": "ping"}, Origin="https://evil.example")
    local = session.post({"jsonrpc": "2.0", "id": 5, "method": "ping"}, Origin="http://localhost:3000")
    stream = session.client.get("/mcp")
    assert batch.status_code == 200
    [opened, ping] = batch.json()
    assert opened["result"]["protocolVersion"] == "2025-11-25"
    assert ping == {"jsonrpc": "2.0", "id": 2, "result": {}}
    assert only_notes.status_code == 202
    assert unsupported.status_code == 400
    assert foreign.status_code == 403
    assert local.status_code == 200
    assert stream.status_code == 405 and stream.headers["allow"] == "POST"


def test_mcp_needs_the_companion_token(config, session_for):
    config.api_token = "server-secret"
    allowed = session_for(config, token="server-secret")
    denied = Session(str(allowed.client.base_url).rstrip("/"))
    ping = allowed.request("ping")
    refused = denied.post({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert ping["result"] == {}
    assert refused.status_code == 401


def test_mcp_lists_only_the_tools_of_active_plugins(config, session_for):
    config.plugins["memo"] = PluginEntry(config.plugins["memo"].config, disabled=True)
    config.plugins["proactive"] = PluginEntry(config.plugins["proactive"].config, disabled=True)
    config.plugins["affect"] = PluginEntry(config.plugins["affect"].config, disabled=True)
    session = session_for(config)
    tools = {tool["name"] for tool in session.request("tools/list")["result"]["tools"]}
    hidden = session.request("tools/call", {"name": "memo_add", "arguments": {"text": "x"}})
    assert tools == {
        "memory_search", "memory_get", "evergreen_list", "evergreen_remember", "evergreen_revise",
        "evergreen_forget",
    }
    assert hidden["error"]["code"] == -32602
