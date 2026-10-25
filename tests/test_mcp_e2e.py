from __future__ import annotations

import json
import os
import select
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from dusha import api


class Session:
    def __init__(self, config: Path):
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
        self.process = subprocess.Popen(
            [sys.executable, "-m", "dusha.cli", "--config", str(config), "mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        self.sent = 0

    def send(self, line: str) -> dict:
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()
        ready, _, _ = select.select([self.process.stdout], [], [], 30)
        assert ready, "no reply from dusha mcp"
        return json.loads(self.process.stdout.readline())

    def request(self, method: str, params: dict | None = None) -> dict:
        self.sent += 1
        message = {"jsonrpc": "2.0", "id": self.sent, "method": method, "params": params or {}}
        reply = self.send(json.dumps(message))
        assert reply["id"] == self.sent
        return reply

    def notify(self, method: str) -> None:
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.process.stdin.flush()

    def call(self, name: str, **arguments) -> tuple[dict, bool]:
        result = self.request("tools/call", {"name": name, "arguments": arguments})["result"]
        return json.loads(result["content"][0]["text"]), result["isError"]

    def close(self) -> str:
        self.process.stdin.close()
        _, stderr = self.process.communicate(timeout=10)
        return stderr


@pytest.fixture
def session_for(tmp_path):
    sessions: list[Session] = []

    def start(server_url: str) -> Session:
        config = tmp_path / "edge" / "config.json"
        config.parent.mkdir(exist_ok=True)
        config.write_text(json.dumps({"proxy": {"server": {"url": server_url}}}), encoding="utf-8")
        sessions.append(Session(config))
        return sessions[-1]

    yield start
    for session in sessions:
        if session.process.poll() is None:
            session.close()


def test_mcp_serves_the_companion_tools(config, run_app, session_for):
    with run_app(api.create_app(config)) as url:
        stored = httpx.post(f"{url}/state/v1/messages", json={"role": "user", "content": "The amber window."})
        session = session_for(url)
        opened = session.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        session.notify("notifications/initialized")
        tools = session.request("tools/list")["result"]["tools"]
        memo, memo_failed = session.call("memo_add", text="Water the fern.")
        memos, _ = session.call("memo_list")
        fact, _ = session.call("evergreen_remember", key="user.name", text="The user is Birch.")
        facts, _ = session.call("evergreen_list")
        stale, stale_failed = session.call(
            "evergreen_revise", fact_id=fact["fact"]["fact_id"], expected_revision=99, text="Changed."
        )
        found, _ = session.call("memory_search", query="amber window")
        record, _ = session.call("memory_get", message_id=stored.json()["id"])
        affect, affect_failed = session.call("affect_status")
        missing, missing_failed = session.call("memo_done", note_id="x")
        unknown = session.request("tools/call", {"name": "nope", "arguments": {}})
        absent = session.request("resources/list")
        ping = session.request("ping")
        garbled = session.send("{not json")
        stderr = session.close()
    assert opened["result"]["protocolVersion"] == "2025-06-18"
    assert opened["result"]["serverInfo"]["name"] == "dusha"
    assert opened["result"]["capabilities"] == {"tools": {}}
    assert {tool["name"] for tool in tools} == {
        "memory_search", "memory_get", "evergreen_list", "evergreen_remember", "evergreen_revise",
        "evergreen_forget", "memo_add", "memo_list", "memo_done", "affect_status",
    }
    assert all(tool["inputSchema"]["type"] == "object" for tool in tools)
    assert not memo_failed and memo["memo"]["text"] == "Water the fern."
    assert [item["text"] for item in memos["memos"]] == ["Water the fern."]
    assert [item["key"] for item in facts["facts"]] == ["user.name"]
    assert stale_failed and "revision" in stale["detail"]
    assert "The amber window." in json.dumps(found)
    assert record["messages"][0]["text"] == "The amber window."
    assert not affect_failed and "mood" in affect
    assert missing_failed and missing == {"error": "missing argument(s): ['reason']"}
    assert unknown["error"]["code"] == -32602
    assert absent["error"]["code"] == -32601
    assert ping["result"] == {}
    assert garbled["error"]["code"] == -32700 and garbled["id"] is None
    assert stderr == ""


def test_mcp_queues_appends_while_the_server_is_down(config, run_app, session_for, free_port):
    port = free_port()
    session = session_for(f"http://127.0.0.1:{port}")
    session.request("initialize", {"protocolVersion": "2025-06-18"})
    memo, memo_failed = session.call("memo_add", text="Queued memo.")
    fact, fact_failed = session.call("evergreen_remember", key="user.pet", text="The user has a cat.")
    listed, list_failed = session.call("memo_list")
    forgot, forget_failed = session.call("evergreen_forget", fact_id="f", expected_revision=1, reason="no")
    with run_app(api.create_app(config), port=port):
        memos, _ = session.call("memo_list")
        facts, _ = session.call("evergreen_list")
    assert (memo["queued"], memo_failed, fact["queued"], fact_failed) == (True, False, True, False)
    assert list_failed and listed["error"].startswith("the server is unreachable")
    assert forget_failed and forgot["error"].startswith("the server is unreachable")
    assert [item["text"] for item in memos["memos"]] == ["Queued memo."]
    assert [item["text"] for item in facts["facts"]] == ["The user has a cat."]
