from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from dusha import api
from dusha.config import StorageConfig
from dusha.timeutil import isoformat, utc_now

_INTEGRATION_DIR = Path(__file__).resolve().parents[1] / "integrations" / "astrbot_dusha"
PLATFORM = "telegram-dusha"
ROUTE = "telegram-dusha:FriendMessage:user-1"
ROUTING_ERROR = {"ok": False, "error": "gateway access is disabled for this platform"}
STORAGE_DISABLED = "built-in message storage is disabled"
NUDGE = "thinking of you"


# Stubs stand in for the external AstrBot framework and its LLM provider, the gateway runs for real.
class AstrBotConfig:
    def __init__(self, **values):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


class Star:
    def __init__(self, context):
        self.context = context


class MessageChain:
    def __init__(self):
        self.text = ""

    def message(self, text):
        self.text = text
        return self


class LLMResponse:
    def __init__(self, completion_text=""):
        self.completion_text = completion_text


class ProviderRequest:
    def __init__(self, prompt="hello", system_prompt="persona", func_tool=None):
        self.prompt, self.system_prompt, self.func_tool = prompt, system_prompt, func_tool


class AstrMessageEvent:
    def __init__(self, platform_id=PLATFORM, unified_msg_origin=ROUTE, text="hello", message_id="m-1"):
        self._platform_id = platform_id
        self._text = text
        self.unified_msg_origin = unified_msg_origin
        self.message_obj = types.SimpleNamespace(message_id=message_id)
        self.created_at = 0

    def get_platform_id(self):
        return self._platform_id

    def get_message_str(self):
        return self._text


class MessageSession:
    def __init__(self, platform_id, message_type, session_id):
        self.platform_id, self.message_type, self.session_id = platform_id, message_type, session_id

    @classmethod
    def from_str(cls, text):
        return cls(*text.split(":", 2))


class CronMessageEvent(AstrMessageEvent):
    def __init__(self, context, session, message, message_type):
        route = ":".join((session.platform_id, session.message_type, session.session_id))
        super().__init__(session.platform_id, route, text=message, message_id="")


class TextPart:
    def __init__(self, text):
        self.text = text


class _Segment:
    def __init__(self, content):
        self.content = content


class ToolSet:
    def __init__(self, names=()):
        self.tools = [types.SimpleNamespace(name=name) for name in names]

    def add_tool(self, tool):
        self.tools.append(tool)

    def remove_tool(self, name):
        self.tools = [tool for tool in self.tools if tool.name != name]

    def empty(self):
        return not self.tools


class ProviderNotFoundError(Exception):
    pass


class _ConversationManager:
    def __init__(self, cid):
        self.cid = cid
        self.pairs = []
        self.created = []

    async def get_curr_conversation_id(self, route):
        return self.cid

    async def new_conversation(self, route):
        self.created.append(route)
        self.cid = "cid-new"
        return self.cid

    async def get_conversation(self, route, cid):
        return None

    async def add_message_pair(self, cid, user_message, assistant_message):
        self.pairs.append((cid, user_message.content[0].text, assistant_message.content[0].text))


class _PersonaManager:
    def __init__(self, prompt):
        self.prompt = prompt

    async def get_default_persona_v3(self, umo=None):
        return {"prompt": self.prompt}


class _ProviderManager:
    def __init__(self, provider_ids):
        self.providers = [types.SimpleNamespace(provider_config={"id": item}) for item in provider_ids]

    def get_insts(self):
        return list(self.providers)


class Context:
    def __init__(
        self, *, cid="cid-1", persona="persona", send_result=True, replies=None, provider_ids=(), pinned=""
    ):
        self.conversation_manager = _ConversationManager(cid)
        self.persona_manager = _PersonaManager(persona)
        self.provider_manager = _ProviderManager(provider_ids)
        self.pinned = pinned or "prov"
        self.send_result = send_result
        self.replies = replies or {}
        self.generations = []
        self.sent = []

    async def get_current_chat_provider_id(self, route):
        return self.pinned

    async def llm_generate(self, **kwargs):
        self.generations.append(kwargs)
        reply = self.replies.get(kwargs["chat_provider_id"], NUDGE)
        if isinstance(reply, Exception):
            raise reply
        return LLMResponse(reply)

    async def send_message(self, route, chain):
        self.sent.append((route, chain.text))
        return self.send_result


class _Filter:
    def __init__(self):
        self.tools = {}

    def llm_tool(self, name):
        def decorator(function):
            self.tools[name] = function
            return function

        return decorator

    def on_llm_request(self):
        return lambda function: function

    on_llm_response = on_llm_request


logger = types.SimpleNamespace(messages=[])
logger.warning = lambda message: logger.messages.append(message)
filter_registry = _Filter()

_STUBS = {
    "astrbot": {},
    "astrbot.api": {"logger": logger},
    "astrbot.api.star": {"Star": Star, "Context": Context},
    "astrbot.api.event": {
        "AstrMessageEvent": AstrMessageEvent,
        "MessageChain": MessageChain,
        "filter": filter_registry,
    },
    "astrbot.api.provider": {"LLMResponse": LLMResponse, "ProviderRequest": ProviderRequest},
    "astrbot.core": {},
    "astrbot.core.config": {},
    "astrbot.core.config.astrbot_config": {"AstrBotConfig": AstrBotConfig},
    "astrbot.core.agent": {},
    "astrbot.core.agent.message": {
        "TextPart": TextPart,
        "UserMessageSegment": _Segment,
        "AssistantMessageSegment": _Segment,
    },
    "astrbot.core.agent.tool": {"ToolSet": ToolSet},
    "astrbot.core.exceptions": {"ProviderNotFoundError": ProviderNotFoundError},
    "astrbot.core.cron": {},
    "astrbot.core.cron.events": {"CronMessageEvent": CronMessageEvent},
    "astrbot.core.platform": {},
    "astrbot.core.platform.message_session": {"MessageSession": MessageSession},
}


def _load_main():
    package = "astrbot_dusha"
    main_name = f"{package}.main"
    for name, attributes in _STUBS.items():
        module = types.ModuleType(name)
        module.__dict__.update(attributes)
        sys.modules[name] = module
    package_module = types.ModuleType(package)
    package_module.__path__ = [str(_INTEGRATION_DIR)]
    sys.modules[package] = package_module
    spec = importlib.util.spec_from_file_location(main_name, _INTEGRATION_DIR / "main.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[main_name] = module
    spec.loader.exec_module(module)
    return module


plugin_main = _load_main()

TOOL_CALLS = {
    "remember_evergreen_fact": ("evergreen fact save failed", {"key": "k", "text": "t"}),
    "revise_evergreen_fact": (
        "evergreen fact revision failed",
        {"fact_id": "1", "expected_revision": 1, "text": "t"},
    ),
    "forget_evergreen_fact": (
        "evergreen fact forget failed",
        {"fact_id": "1", "expected_revision": 1, "reason": "r"},
    ),
    "review_evergreen_facts": ("evergreen fact review failed", {}),
    "search_conversation_memory": ("conversation memory search failed", {"query": "q"}),
    "get_conversation_record": ("conversation record read failed", {"memory_id": 1}),
    "yumecho_add": ("memo add failed", {"text": "note"}),
    "yumecho_list": ("memo list failed", {}),
    "yumecho_done": ("memo done failed", {"note_id": 1, "reason": "done"}),
}


class _Gateway(httpx.AsyncBaseTransport):
    def __init__(self, app):
        self.inner = httpx.ASGITransport(app=app)
        self.failures: dict[str, Exception | int] = {}
        self.calls: list[tuple[str, str, int | None]] = []

    async def handle_async_request(self, request):
        path = request.url.path
        for suffix, failure in self.failures.items():
            if path.endswith(suffix):
                self.calls.append((request.method, path, None))
                if isinstance(failure, Exception):
                    raise failure
                return httpx.Response(failure, text="unparseable body")
        response = await self.inner.handle_async_request(request)
        self.calls.append((request.method, path, response.status_code))
        return response


class _Rig:
    def __init__(self, app):
        self.app = app
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.plugins = []

    async def plugin(self, *, context=None, platform_id=PLATFORM, **settings):
        plugin = plugin_main.DushaPlugin(
            context=context or Context(), config=AstrBotConfig(platform_id=platform_id, **settings)
        )
        timeout = plugin.client.timeout
        await plugin.client.aclose()
        plugin.gateway = _Gateway(self.app)
        plugin.client = httpx.AsyncClient(transport=plugin.gateway, timeout=timeout)
        self.plugins.append(plugin)
        return plugin

    async def json(self, method, path, **kwargs):
        response = await self.http.request(method, path, **kwargs)
        assert response.status_code == 200, response.text
        return response.json()

    async def search(self, query):
        body = {"query": query, "limit": 50, "context_messages": 0}
        found = await self.json("POST", "/state/v1/memory/search", json=body)
        messages = [message for hit in found["results"] for message in hit["messages"]]
        return sorted(messages, key=lambda message: message["id"])

    async def facts(self, **params):
        return (await self.json("GET", "/state/v1/evergreen/facts", params=params))["facts"]

    async def memos(self, status="active"):
        return (await self.json("GET", "/state/v1/memo/list", params={"status": status}))["memos"]

    async def conflict_detail(self, key):
        response = await self.http.post("/state/v1/evergreen/facts", json={"key": key, "text": "probe"})
        assert response.status_code == 409
        return response.json()["detail"]


@asynccontextmanager
async def _running(config):
    app = api.create_app(config)
    async with app.router.lifespan_context(app):
        rig = _Rig(app)
        try:
            yield rig
        finally:
            for plugin in rig.plugins:
                await plugin.terminate()
            await rig.http.aclose()


async def _tool(plugin, name, event=None, **arguments):
    return json.loads(await filter_registry.tools[name](plugin, event or AstrMessageEvent(), **arguments))


async def _chat(plugin, prompt, message_id, reply="", request=None):
    request = request or ProviderRequest(prompt)
    event = AstrMessageEvent(text=prompt, message_id=message_id)
    await plugin.add_state_context(event, request)
    if reply:
        await plugin.store_response(event, LLMResponse(reply))
    return request


def _backdate(app, past):
    with app.state.service.database.connect() as db:
        db.execute(
            "UPDATE affect_state SET last_user_message_at=?, last_updated_at=? WHERE id=1",
            (isoformat(past), isoformat(past)),
        )


async def _due_event(rig, plugin, message_id, hours=4):
    await _chat(plugin, f"hello from turn {message_id}", message_id)
    _backdate(rig.app, utc_now() - timedelta(hours=hours, minutes=5))
    event = (await rig.json("POST", "/state/v1/proactive/evaluate"))["event"]
    assert event is not None
    return event


async def _poll_until(plugin, suffix):
    await plugin.initialize()
    async with asyncio.timeout(10):
        while not any(path.endswith(suffix) for _, path, _ in plugin.gateway.calls):
            await asyncio.sleep(0.01)
    await plugin.terminate()


def _proactive_config(config):
    config.proactive.poll_interval_seconds = 3600
    config.proactive.cooldown_minutes = 0
    config.proactive.max_unanswered = 10
    config.proactive.max_per_day = 20
    return config


async def test_wrong_platform_gets_routing_error_and_reaches_no_gateway(config):
    assert list(filter_registry.tools) == list(plugin_main.GATEWAY_TOOL_NAMES) == list(TOOL_CALLS)
    async with _running(config) as rig:
        plugin = await rig.plugin()
        unrouted = await rig.plugin(platform_id="")
        cases = [(plugin, "discord-dusha"), (plugin, "telegram-dusha-backup"), (unrouted, PLATFORM)]
        for candidate, platform_id in cases:
            event = AstrMessageEvent(platform_id, text="a secret from the wrong platform")
            for name, (_, arguments) in TOOL_CALLS.items():
                assert await _tool(candidate, name, event, **arguments) == ROUTING_ERROR
            assert await _tool(candidate, "search_conversation_memory", event, query=123) == ROUTING_ERROR
            request = ProviderRequest("a secret from the wrong platform", func_tool=ToolSet(TOOL_CALLS))
            request.func_tool.add_tool(types.SimpleNamespace(name="web_search"))
            await candidate.add_state_context(event, request)
            assert request.system_prompt == "persona"
            assert [tool.name for tool in request.func_tool.tools] == ["web_search"]
            await candidate.store_response(event, LLMResponse("a reply on the wrong platform"))
            assert candidate.gateway.calls == []

        logger.messages.clear()
        await unrouted.initialize()
        assert unrouted.poll_task is None
        assert logger.messages == ["[dusha] platform_id is empty. Gateway routing is disabled"]
        assert await rig.search("secret wrong platform reply") == []
        assert await rig.facts(include_inactive=True) == []
        assert await rig.memos() == []
        assert (await rig.json("GET", "/state/v1/affect"))["last_user_message_at"] is None


async def test_evergreen_tools_remember_revise_forget_and_review(config):
    async with _running(config) as rig:
        plugin = await rig.plugin()
        plain = await _tool(plugin, "remember_evergreen_fact", key="user.pet", text="Ada has a cat")
        assert plain["ok"] is True
        assert plain["fact"]["priority"] == 50
        assert plain["fact"]["source_message_id"] is None

        await _chat(plugin, "my name is Ada", "m-1")
        source_id = plugin.latest_source_message_ids[ROUTE]
        remembered = await _tool(
            plugin,
            "remember_evergreen_fact",
            key="user.name",
            text="The user is Ada",
            priority=80,
            review_after="clear",
            expires_at="2099-01-01T00:00:00Z",
            reason="first",
        )
        assert remembered["ok"] is True
        fact = remembered["fact"]
        stored = {item["key"]: item for item in await rig.facts()}
        assert list(stored) == ["user.name", "user.pet"]
        assert stored["user.name"]["fact_id"] == fact["fact_id"]
        assert stored["user.name"]["text"] == "The user is Ada"
        assert stored["user.name"]["priority"] == 80
        assert stored["user.name"]["reason"] == "first"
        assert stored["user.name"]["source_message_id"] == source_id
        assert stored["user.name"]["review_after"] is None
        assert stored["user.name"]["expires_at"] is not None
        assert (await rig.json("GET", f"/state/v1/messages/{source_id}"))["text"] == "my name is Ada"

        assert (await _tool(plugin, "review_evergreen_facts"))["facts"] == []
        revised = await _tool(
            plugin,
            "revise_evergreen_fact",
            fact_id=fact["fact_id"],
            expected_revision=fact["revision"],
            text="The user is Ada Lovelace",
            review_after="2020-01-01T00:00:00Z",
            expires_at="clear",
            reason="full name",
        )
        assert revised["ok"] is True
        assert revised["fact"]["revision"] == fact["revision"] + 1
        current = {item["key"]: item for item in await rig.facts()}["user.name"]
        assert current["text"] == "The user is Ada Lovelace"
        assert current["priority"] == 80
        assert current["expires_at"] is None
        assert current["review_due"] is True
        due = await _tool(plugin, "review_evergreen_facts")
        assert [item["key"] for item in due["facts"]] == ["user.name"]

        reprioritized = await _tool(
            plugin,
            "revise_evergreen_fact",
            fact_id=fact["fact_id"],
            expected_revision=revised["fact"]["revision"],
            text="The user is Ada Lovelace",
            priority=10,
        )
        assert reprioritized["fact"]["priority"] == 10
        assert reprioritized["fact"]["review_after"] is not None
        assert [item["key"] for item in await rig.facts()] == ["user.pet", "user.name"]

        forgotten = await _tool(
            plugin,
            "forget_evergreen_fact",
            fact_id=fact["fact_id"],
            expected_revision=reprioritized["fact"]["revision"],
            reason="asked to forget",
        )
        assert forgotten["ok"] is True
        assert forgotten["fact"]["state"] == "forgotten"
        assert [item["key"] for item in await rig.facts()] == ["user.pet"]
        history = await rig.json("GET", f"/state/v1/evergreen/facts/{fact['fact_id']}/history")
        assert [item["revision"] for item in history["revisions"]] == [1, 2, 3, 4]
        assert {item["source_message_id"] for item in history["revisions"]} == {source_id}

        everything = await _tool(
            plugin, "review_evergreen_facts", due_only=False, include_inactive=True, limit=1000
        )
        assert everything["ok"] is True
        assert sorted(item["key"] for item in everything["facts"]) == ["user.name", "user.pet"]
        one = await _tool(plugin, "review_evergreen_facts", due_only=False, include_inactive=True, limit=0)
        assert len(one["facts"]) == 1
        for index in range(100):
            await rig.json("POST", "/state/v1/evergreen/facts", json={"key": f"bulk.k{index}", "text": "x"})
        assert len(await rig.facts(limit=500)) == 101
        capped = await _tool(plugin, "review_evergreen_facts", due_only=False, limit=1000)
        assert len(capped["facts"]) == 100


async def test_memory_tools_search_and_read_stored_records(config):
    async with _running(config) as rig:
        plugin = await rig.plugin()
        for index in range(12):
            await _chat(plugin, f"lantern note item{index}", f"m-{index}", reply=f"plain answer {index}")

        found = await _tool(plugin, "search_conversation_memory", query="lantern")
        assert found["ok"] is True
        assert len(found["records"]) == 5
        for record in found["records"]:
            stored = await rig.json("GET", f"/state/v1/messages/{record['memory_id']}")
            assert record == {
                "memory_id": stored["id"],
                "time": stored["occurred_at"],
                "role": "user",
                "text": stored["text"],
            }
            assert stored["text"].startswith("lantern note item")
        clamped_high = await _tool(plugin, "search_conversation_memory", query="lantern", limit=999)
        assert len(clamped_high["records"]) == 10
        clamped_low = await _tool(plugin, "search_conversation_memory", query="lantern", limit=0)
        assert len(clamped_low["records"]) == 1
        assert (await _tool(plugin, "search_conversation_memory", query="zeppelin"))["records"] == []

        target = (await _tool(plugin, "search_conversation_memory", query="item5"))["records"][0]
        memory_id = target["memory_id"]
        alone = await _tool(plugin, "get_conversation_record", memory_id=memory_id, context_messages=-3)
        assert alone == {"ok": True, "records": [target]}
        near = await _tool(plugin, "get_conversation_record", memory_id=memory_id)
        assert [item["memory_id"] for item in near["records"]] == [memory_id - 1, memory_id, memory_id + 1]
        assert [item["role"] for item in near["records"]] == ["assistant", "user", "assistant"]
        wide = await _tool(plugin, "get_conversation_record", memory_id=memory_id, context_messages=99)
        assert wide["ok"] is True
        assert len(wide["records"]) == 21


async def test_memo_tools_add_list_and_done(config):
    async with _running(config) as rig:
        plugin = await rig.plugin()
        added = await _tool(plugin, "yumecho_add", text="buy milk")
        assert added["ok"] is True
        assert added["memo"]["status"] == "active"
        await _tool(plugin, "yumecho_add", text="ask about the interview")
        assert [memo["text"] for memo in await rig.memos()] == ["buy milk", "ask about the interview"]

        listed = await _tool(plugin, "yumecho_list")
        assert listed["ok"] is True
        assert listed["memos"] == await rig.memos()
        assert len((await _tool(plugin, "yumecho_list", limit=0))["memos"]) == 1
        assert len((await _tool(plugin, "yumecho_list", limit=999))["memos"]) == 2
        assert (await _tool(plugin, "yumecho_list", status="archived", limit=999))["memos"] == []

        note_id = added["memo"]["id"]
        calls = len(plugin.gateway.calls)
        rejected = await _tool(plugin, "yumecho_done", note_id=note_id, reason="   ")
        assert rejected == {"ok": False, "error": "reason is required"}
        assert len(plugin.gateway.calls) == calls
        assert len(await rig.memos()) == 2

        done = await _tool(plugin, "yumecho_done", note_id=note_id, reason="bought it")
        assert done["ok"] is True
        assert done["memo"]["status"] == "archived"
        assert [memo["text"] for memo in await rig.memos()] == ["ask about the interview"]
        archived = await rig.memos("archived")
        assert [(memo["id"], memo["reason"]) for memo in archived] == [(note_id, "bought it")]
        assert (await _tool(plugin, "yumecho_list", status="archived"))["memos"] == archived

        request = await _chat(plugin, "what is left to do", "m-1")
        assert "ask about the interview" in request.system_prompt
        assert "buy milk" not in request.system_prompt


async def test_tool_transport_failures_map_to_fallback_errors(config):
    async with _running(config) as rig:
        plugin = await rig.plugin()
        plugin.gateway.failures[""] = httpx.ConnectError("boom")
        for name, (fallback, arguments) in TOOL_CALLS.items():
            logger.messages.clear()
            assert await _tool(plugin, name, **arguments) == {"ok": False, "error": fallback}
            assert logger.messages == [f"[dusha] {fallback.capitalize()}: boom"]

        plugin.gateway.failures = {"/state/v1/memory/search": 503}
        assert await _tool(plugin, "search_conversation_memory", query="hello") == {
            "ok": False,
            "error": "conversation memory search failed",
            "status": 503,
        }
        calls = len(plugin.gateway.calls)
        assert await _tool(plugin, "search_conversation_memory", query="   ") == {
            "ok": False,
            "error": "query is required",
        }
        with pytest.raises(AttributeError, match="has no attribute 'strip'"):
            await _tool(plugin, "search_conversation_memory", query=123)
        with pytest.raises(AttributeError, match="has no attribute 'strip'"):
            await _tool(plugin, "remember_evergreen_fact", key="k", text="t", review_after=123)
        assert len(plugin.gateway.calls) == calls
        assert await rig.facts(include_inactive=True) == []
        assert await rig.memos() == []


async def test_tool_gateway_statuses_map_to_ok_false_with_status(config, monkeypatch):
    async with _running(config) as rig:
        plugin = await rig.plugin()
        fact = (await _tool(plugin, "remember_evergreen_fact", key="user.tea", text="Ada drinks tea"))["fact"]
        assert await _tool(plugin, "remember_evergreen_fact", key="user.tea", text="again") == {
            "ok": False,
            "error": await rig.conflict_detail("user.tea"),
            "status": 409,
        }
        stale = await _tool(
            plugin, "revise_evergreen_fact", fact_id=fact["fact_id"], expected_revision=9, text="stale"
        )
        assert (stale["ok"], stale["status"]) == (False, 409)
        assert await _tool(
            plugin, "revise_evergreen_fact", fact_id="missing", expected_revision=1, text="new"
        ) == {"ok": False, "error": "evergreen fact not found", "status": 404}
        assert await _tool(
            plugin, "forget_evergreen_fact", fact_id=fact["fact_id"], expected_revision=0, reason="bad"
        ) == {"ok": False, "error": "evergreen fact forget failed", "status": 422}
        assert await _tool(plugin, "get_conversation_record", memory_id=999) == {
            "ok": False,
            "error": "memory record not found",
            "status": 404,
        }
        assert await _tool(plugin, "yumecho_done", note_id=999, reason="done") == {
            "ok": False,
            "error": "memo not found",
            "status": 404,
        }
        blank = await _tool(plugin, "yumecho_add", text="   ")
        assert (blank["ok"], blank["status"]) == (False, 422)
        assert (await _tool(plugin, "yumecho_list", status="lost"))["status"] == 422
        assert [item["text"] for item in await rig.facts()] == ["Ada drinks tea"]
        assert await rig.memos() == []

    config.storage = StorageConfig(enabled=False)
    async with _running(config) as rig:
        plugin = await rig.plugin()
        for name, (_, arguments) in TOOL_CALLS.items():
            assert await _tool(plugin, name, **arguments) == {
                "ok": False,
                "error": STORAGE_DISABLED,
                "status": 503,
            }

    config.storage = StorageConfig()
    config.api_token_env = "E2E_ASTRBOT_TOKEN"
    monkeypatch.setenv("E2E_ASTRBOT_TOKEN", "right-secret")
    async with _running(config) as rig:
        denied = await rig.plugin(api_token="wrong-secret")
        assert await _tool(denied, "yumecho_add", text="never stored") == {
            "ok": False,
            "error": "invalid companion token",
            "status": 401,
        }
        allowed = await rig.plugin(api_token="right-secret")
        assert (await _tool(allowed, "yumecho_add", text="stored with the token"))["ok"] is True
        listed = await _tool(allowed, "yumecho_list")
        assert [memo["text"] for memo in listed["memos"]] == ["stored with the token"]


async def test_chat_turn_stores_message_injects_context_and_archives_reply(config):
    async with _running(config) as rig:
        plugin = await rig.plugin()
        await _tool(plugin, "remember_evergreen_fact", key="user.city", text="Ada lives in Turin")
        request = ProviderRequest("the amber window is open")
        await _chat(plugin, "the amber window is open", "m-1", request=request)
        assert request.system_prompt.startswith("persona\n")
        assert request.system_prompt.endswith("</companion_state>")
        assert "Ada lives in Turin" in request.system_prompt
        assert "the amber window is open" not in request.system_prompt

        user_id = plugin.latest_source_message_ids[ROUTE]
        stored = await rig.json("GET", f"/state/v1/messages/{user_id}")
        assert stored["harness"] == "astrbot"
        assert stored["external_conversation_id"] == stored["route"] == ROUTE
        assert (stored["role"], stored["text"], stored["external_id"]) == (
            "user",
            "the amber window is open",
            "user:m-1",
        )
        assert (await rig.json("GET", "/state/v1/affect"))["last_user_message_at"] is not None

        calls = list(plugin.gateway.calls)
        assert calls[-2:] == [("POST", "/state/v1/messages", 200), ("POST", "/state/v1/context", 200)]
        await plugin.add_state_context(AstrMessageEvent(message_id="m-1"), request)
        assert plugin.gateway.calls == calls

        event = AstrMessageEvent(text="the amber window is open", message_id="m-1")
        await plugin.store_response(event, LLMResponse("  I will close it at dusk  "))
        await plugin.store_response(event, LLMResponse("   "))
        archive = await rig.search("amber window dusk")
        assert [(item["role"], item["text"]) for item in archive] == [
            ("user", "the amber window is open"),
            ("assistant", "I will close it at dusk"),
        ]
        assert {item["harness"] for item in archive} == {"astrbot"}

        parts_request = ProviderRequest("and tomorrow")
        parts_request.extra_user_content_parts = []
        await _chat(plugin, "and tomorrow", "m-2", request=parts_request)
        assert parts_request.system_prompt == "persona"
        (part,) = parts_request.extra_user_content_parts
        assert "<companion_state>" in part.text
        assert "I will close it at dusk" in part.text
        assert plugin.latest_source_message_ids[ROUTE] == user_id + 2


async def test_chat_turn_keeps_context_when_message_storage_fails(config):
    async with _running(config) as rig:
        plugin = await rig.plugin()
        await _chat(plugin, "first stored turn", "m-1")
        assert ROUTE in plugin.latest_source_message_ids

        failures = [(httpx.ReadTimeout("slow decision plugin"), "slow decision plugin"), (500, "500")]
        for index, (failure, logged) in enumerate(failures):
            plugin.latest_source_message_ids[ROUTE] = 1
            plugin.gateway.failures = {"/state/v1/messages": failure}
            logger.messages.clear()
            request = await _chat(plugin, f"unstored nightjar {index}", f"m-lost-{index}")
            assert "<companion_state>" in request.system_prompt
            assert "first stored turn" in request.system_prompt
            assert plugin.latest_source_message_ids == {}
            assert len(logger.messages) == 1
            assert logger.messages[0].startswith("[dusha] Message archive failed:")
            assert logged in logger.messages[0]

        plugin.gateway.failures = {}
        assert await rig.search("unstored nightjar") == []
        saved = await _tool(plugin, "remember_evergreen_fact", key="user.bird", text="Ada likes nightjars")
        assert saved["fact"]["source_message_id"] is None


async def test_chat_turn_survives_a_failed_context_call(config):
    config.memory.injection_max_chars = 1
    async with _running(config) as rig:
        plugin = await rig.plugin()
        logger.messages.clear()
        request = await _chat(plugin, "context budget is too small", "m-1", reply="still answering")
        assert request.system_prompt == "persona"
        assert plugin.gateway.calls[:2] == [
            ("POST", "/state/v1/messages", 200),
            ("POST", "/state/v1/context", 422),
        ]
        assert len(logger.messages) == 1 and "Context unavailable" in logger.messages[0]
        stored = await rig.json("GET", f"/state/v1/messages/{plugin.latest_source_message_ids[ROUTE]}")
        assert stored["text"] == "context budget is too small"
        assert [item["text"] for item in await rig.search("budget answering")] == [
            "context budget is too small",
            "still answering",
        ]

    config.memory.injection_max_chars = 20_000
    async with _running(config) as rig:
        plugin = await rig.plugin()
        plugin.gateway.failures = {"/state/v1/context": httpx.ReadTimeout("context timed out")}
        logger.messages.clear()
        request = await _chat(plugin, "the context call timed out", "m-2")
        assert request.system_prompt == "persona"
        assert logger.messages == ["[dusha] Context unavailable: context timed out"]
        assert [item["text"] for item in await rig.search("timed out")] == ["the context call timed out"]


async def test_settings_take_schema_defaults_and_harness_names_the_stored_state(config):
    schema = json.loads((_INTEGRATION_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
    async with _running(_proactive_config(config)) as rig:
        default = await rig.plugin()
        assert default.base_url == schema["gateway_url"]["default"]
        assert default.harness == schema["harness"]["default"] == "astrbot"
        assert default.poll_interval_seconds == schema["poll_interval_seconds"]["default"]
        assert default.proactive_enabled is schema["proactive_enabled"]["default"]
        assert list(default.proactive_tool_keywords) == schema["proactive_tool_keywords"]["default"]
        assert default.headers == {}
        assert default.client.timeout == httpx.Timeout(schema["request_timeout_seconds"]["default"])
        assert (await rig.plugin(request_timeout_seconds=90)).client.timeout == httpx.Timeout(90.0)
        clamped = await rig.plugin(
            request_timeout_seconds=0, poll_interval_seconds=1, harness="  ", gateway_url="http://gateway/"
        )
        assert clamped.client.timeout == httpx.Timeout(1.0)
        assert (clamped.poll_interval_seconds, clamped.harness, clamped.base_url) == (
            5,
            "astrbot",
            "http://gateway",
        )

        await _chat(default, "stored under the default harness", "m-1", reply="default harness reply")
        renamed = await rig.plugin(harness="dusha-bot")
        await _chat(renamed, "stored under the renamed harness", "m-2", reply="renamed harness reply")
        harnesses = {item["text"]: item["harness"] for item in await rig.search("stored harness reply")}
        assert harnesses == {
            "stored under the default harness": "astrbot",
            "default harness reply": "astrbot",
            "stored under the renamed harness": "dusha-bot",
            "renamed harness reply": "dusha-bot",
        }

        _backdate(rig.app, utc_now() - timedelta(hours=4, minutes=5))
        event = (await rig.json("POST", "/state/v1/proactive/evaluate"))["event"]
        assert event["target"] == {"harness": "dusha-bot", "conversation_id": ROUTE, "route": ROUTE}
        await _poll_until(default, "/state/v1/proactive/events")
        assert default.gateway.calls[-1] == ("GET", "/state/v1/proactive/events", 200)
        assert default.context.sent == []
        await _poll_until(renamed, "/ack")
        assert renamed.context.sent == [(ROUTE, NUDGE)]
        delivered = [item for item in await rig.search("thinking") if item["text"] == NUDGE]
        assert [(item["role"], item["harness"]) for item in delivered] == [("assistant", "dusha-bot")]


async def test_proactive_event_is_delivered_acknowledged_sent_and_stored(config):
    async with _running(_proactive_config(config)) as rig:
        first = await rig.plugin()
        event = await _due_event(rig, first, "m-1")
        await _poll_until(first, "/ack")
        context = first.context
        assert context.sent == [(ROUTE, NUDGE)]
        (generation,) = context.generations
        assert generation["chat_provider_id"] == "prov"
        instruction = event["generation_instruction"]
        assert generation["prompt"] == instruction + "\n" + plugin_main.PROACTIVE_YUMECHO_NOTE
        assert generation["system_prompt"] == "persona\n" + event["context"]["injection"]
        marker = f"[proactive {event['reason']}] after {event['silence_text']} of silence"
        assert context.conversation_manager.pairs == [("cid-1", marker, NUDGE)]
        assert first.gateway.calls[-1] == ("POST", f"/state/v1/proactive/events/{event['id']}/ack", 200)

        stored = [item for item in await rig.search("thinking") if item["role"] == "assistant"]
        assert [(item["text"], item["harness"], item["route"]) for item in stored] == [
            (NUDGE, "astrbot", ROUTE)
        ]
        affect = await rig.json("GET", "/state/v1/affect")
        assert affect["unanswered_proactive"] == 1
        assert affect["last_proactive_sent_at"] is not None
        polled = await rig.json("GET", "/state/v1/proactive/events", params={"consumer": "astrbot"})
        assert polled["events"] == []

        fallback = Context(
            cid=None,
            provider_ids=("prov", "backup"),
            replies={"prov": RuntimeError("Connection error"), "backup": "  fallback text  "},
        )
        second = await rig.plugin(context=fallback)
        await _due_event(rig, second, "m-2")
        await _poll_until(second, "/ack")
        assert [item["chat_provider_id"] for item in fallback.generations] == ["prov", "backup"]
        assert fallback.sent == [(ROUTE, "fallback text")]
        assert fallback.conversation_manager.created == [ROUTE]
        assert [(pair[0], pair[2]) for pair in fallback.conversation_manager.pairs] == [
            ("cid-new", "fallback text")
        ]
        assert [item["role"] for item in await rig.search("fallback text")] == ["assistant"]
        assert (await rig.json("GET", "/state/v1/affect"))["unanswered_proactive"] == 1


async def test_proactive_failures_acknowledge_failed_and_store_nothing(config):
    failing = {
        "send fails": Context(send_result=False),
        "every provider fails": Context(
            provider_ids=("prov", "backup"),
            replies={
                "prov": RuntimeError("Connection error: prov"),
                "backup": RuntimeError("Connection error: backup"),
            },
        ),
        "empty reply": Context(replies={"prov": "   "}),
        "no persona": Context(persona=""),
    }
    async with _running(_proactive_config(config)) as rig:
        for index, (label, context) in enumerate(failing.items()):
            plugin = await rig.plugin(context=context)
            logger.messages.clear()
            event = await _due_event(rig, plugin, f"m-{index}", hours=4 + index)
            await _poll_until(plugin, "/ack")
            assert plugin.gateway.calls[-1] == (
                "POST",
                f"/state/v1/proactive/events/{event['id']}/ack",
                200,
            ), label
            assert any("Proactive delivery failed" in message for message in logger.messages), label
            assert context.conversation_manager.pairs == [], label
            assert context.sent == ([(ROUTE, NUDGE)] if label == "send fails" else []), label
            assert await rig.search("thinking") == [], label
            affect = await rig.json("GET", "/state/v1/affect")
            assert affect["unanswered_proactive"] == 0, label
            assert affect["last_proactive_sent_at"] is None, label
            polled = await rig.json("GET", "/state/v1/proactive/events", params={"consumer": "astrbot"})
            assert polled["events"] == [], label
            assert (await rig.json("POST", "/state/v1/proactive/evaluate"))["event"] is None, label
        generations = failing["every provider fails"].generations
        assert [item["chat_provider_id"] for item in generations] == ["prov", "backup"]

        other = await rig.plugin(platform_id="telegram-other")
        retry = await rig.plugin()
        await _due_event(rig, retry, "m-retry", hours=9)
        await _poll_until(other, "/ack")
        assert other.context.generations == []
        assert other.context.sent == []
        assert await rig.search("thinking") == []


AVAILABLE_TOOLS = (
    "yumecho_done",
    "remember_evergreen_fact",
    "donsetch_search",
    "Fetch_URL",
    "web_search",
    "astrbot_execute_shell",
)
PROACTIVE_TOOL_CASES = [
    ({}, ["yumecho_done", "remember_evergreen_fact", "donsetch_search", "Fetch_URL"]),
    (
        {"proactive_tool_keywords": ["Search", " "]},
        ["yumecho_done", "remember_evergreen_fact", "donsetch_search", "web_search"],
    ),
    ({"proactive_tool_keywords": []}, ["yumecho_done", "remember_evergreen_fact"]),
    (
        {"proactive_tool_keywords": "search"},
        ["yumecho_done", "remember_evergreen_fact", "donsetch_search", "Fetch_URL"],
    ),
]


@pytest.mark.parametrize(
    "settings, expected",
    PROACTIVE_TOOL_CASES,
    ids=["default keywords", "custom keywords", "empty list", "not a list"],
)
async def test_proactive_delivery_offers_gateway_tools_and_keyword_matches(settings, expected, config):
    async with _running(_proactive_config(config)) as rig:
        context = Context()
        context.get_llm_tool_manager = lambda: types.SimpleNamespace(
            get_full_tool_set=lambda: ToolSet(AVAILABLE_TOOLS)
        )
        plugin = await rig.plugin(context=context, **settings)
        await _due_event(rig, plugin, "m-1")
        await _poll_until(plugin, "/ack")
        (generation,) = context.generations
        assert [tool.name for tool in generation["tools"].tools] == expected
        assert [item["role"] for item in await rig.search("thinking")] == ["assistant"]


async def test_proactive_tool_loop_runs_gateway_tools_and_relaxes_missing_provider(config):
    async with _running(_proactive_config(config)) as rig:
        context = Context(provider_ids=("prov", "backup"), pinned="missing")
        context.get_llm_tool_manager = lambda: types.SimpleNamespace(
            get_full_tool_set=lambda: ToolSet(AVAILABLE_TOOLS)
        )
        plugin = await rig.plugin(context=context)
        memo = (await _tool(plugin, "yumecho_add", text="ask about the interview"))["memo"]
        loops = []

        async def tool_loop_agent(**kwargs):
            fallbacks = [provider.provider_config["id"] for provider in kwargs["fallback_providers"]]
            loops.append((kwargs["chat_provider_id"], fallbacks))
            if kwargs["chat_provider_id"] == "missing":
                raise ProviderNotFoundError("nope")
            assert "ask about the interview" in kwargs["system_prompt"]
            assert plugin_main.PROACTIVE_YUMECHO_NOTE in kwargs["prompt"]
            assert "yumecho_done" in [tool.name for tool in kwargs["tools"].tools]
            done = await _tool(
                plugin, "yumecho_done", kwargs["event"], note_id=memo["id"], reason="asked in the nudge"
            )
            assert done["ok"] is True
            return LLMResponse("how did the interview go")

        context.tool_loop_agent = tool_loop_agent
        await _due_event(rig, plugin, "m-1")
        await _poll_until(plugin, "/ack")
        assert loops == [("missing", ["prov", "backup"]), ("prov", ["backup"])]
        assert context.generations == []
        assert context.sent == [(ROUTE, "how did the interview go")]
        assert await rig.memos() == []
        archived = await rig.memos("archived")
        assert [(item["id"], item["reason"]) for item in archived] == [(memo["id"], "asked in the nudge")]
        assert [item["role"] for item in await rig.search("how did the interview go")] == ["assistant"]
        assert (await rig.json("GET", "/state/v1/affect"))["unanswered_proactive"] == 1


async def test_plugin_state_survives_gateway_restart(config):
    async with _running(config) as rig:
        plugin = await rig.plugin()
        await _chat(plugin, "remember the lighthouse", "m-1", reply="the lighthouse is noted")
        fact = (
            await _tool(plugin, "remember_evergreen_fact", key="user.place", text="Ada loves the lighthouse")
        )["fact"]
        memo = (await _tool(plugin, "yumecho_add", text="ask about the lighthouse trip"))["memo"]

    async with _running(config) as rig:
        plugin = await rig.plugin()
        found = await _tool(plugin, "search_conversation_memory", query="lighthouse")
        assert [(item["role"], item["text"]) for item in found["records"]] == [
            ("user", "remember the lighthouse"),
            ("assistant", "the lighthouse is noted"),
        ]
        listed = await _tool(plugin, "review_evergreen_facts", due_only=False)
        assert [(item["fact_id"], item["revision"]) for item in listed["facts"]] == [
            (fact["fact_id"], fact["revision"])
        ]
        assert [item["id"] for item in (await _tool(plugin, "yumecho_list"))["memos"]] == [memo["id"]]

        request = await _chat(plugin, "what did I ask you to keep", "m-2")
        assert "Ada loves the lighthouse" in request.system_prompt
        assert "ask about the lighthouse trip" in request.system_prompt
        assert "the lighthouse is noted" in request.system_prompt
        revised = await _tool(
            plugin,
            "revise_evergreen_fact",
            fact_id=fact["fact_id"],
            expected_revision=fact["revision"],
            text="Ada loves the old lighthouse",
        )
        assert revised["fact"]["revision"] == fact["revision"] + 1
        assert [item["text"] for item in await rig.facts()] == ["Ada loves the old lighthouse"]
