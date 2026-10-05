import importlib.util
import json
import sys
import types
from pathlib import Path

import httpx
import pytest

_INTEGRATION_DIR = Path(__file__).resolve().parents[1] / "integrations" / "astrbot_companion_gateway"
ROUTING_ERROR = '{"ok":false,"error":"gateway access is disabled for this platform"}'


class AstrBotConfig:
    def __init__(self, **values):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


Context = type("Context", (), {})
Star = type("Star", (), {"__init__": lambda self, context: setattr(self, "context", context)})


class MessageChain:
    def __init__(self):
        self.text = ""

    def message(self, text):
        self.text = text
        return self
LLMResponse = type("LLMResponse", (), {})
ProviderRequest = type("ProviderRequest", (), {})


class AstrMessageEvent:
    def __init__(self, platform_id="telegram-sophia", unified_msg_origin="telegram:user-1"):
        self._platform_id = platform_id
        self.unified_msg_origin = unified_msg_origin

    def get_platform_id(self):
        return self._platform_id


logger = types.SimpleNamespace(messages=[])
logger.warning = lambda message: logger.messages.append(message)


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


filter_registry = _Filter()


def _load_main():
    package = "astrbot_companion_gateway"
    main_name = f"{package}.main"
    if main_name in sys.modules:
        return sys.modules[main_name]
    if "astrbot" not in sys.modules:
        names = ("astrbot", "astrbot.api", "astrbot.api.star", "astrbot.api.event", "astrbot.api.provider",
                 "astrbot.core", "astrbot.core.config", "astrbot.core.config.astrbot_config",
                 "astrbot.core.utils", "astrbot.core.utils.astrbot_path")
        modules = {name: types.ModuleType(name) for name in names}
        modules["astrbot.api.star"].Star, modules["astrbot.api.star"].Context = Star, Context
        modules["astrbot.api"].logger = logger
        modules["astrbot.api.event"].AstrMessageEvent = AstrMessageEvent
        modules["astrbot.api.event"].MessageChain = MessageChain
        modules["astrbot.api.event"].filter = filter_registry
        modules["astrbot.api.provider"].LLMResponse = LLMResponse
        modules["astrbot.api.provider"].ProviderRequest = ProviderRequest
        modules["astrbot.core.config.astrbot_config"].AstrBotConfig = AstrBotConfig
        modules["astrbot.core.utils.astrbot_path"].get_astrbot_config_path = lambda: "/nonexistent"
        sys.modules.update(modules)
    package_module = types.ModuleType(package)
    package_module.__path__ = [str(_INTEGRATION_DIR)]
    sys.modules[package] = package_module
    spec = importlib.util.spec_from_file_location(main_name, _INTEGRATION_DIR / "main.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[main_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def plugin_factory():
    built = []

    async def _build(handler=None, platform_id="telegram-sophia"):
        plugin = _load_main().CompanionGatewayPlugin(
            context=Context(), config=AstrBotConfig(platform_id=platform_id)
        )
        if handler is not None:
            original = plugin.client
            plugin.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            await original.aclose()
        built.append(plugin)
        return plugin

    yield _build
    for plugin in built:
        await plugin.client.aclose()


OK = '{"ok":true,"id":42,"status":"ok"}'


def _generic(request):
    return httpx.Response(200, json={"id": 42, "status": "ok"})


TOOLS = [
    (lambda p, e: p.remember_evergreen_fact(e, "k", "t"), "evergreen fact save failed", _generic, OK),
    (lambda p, e: p.revise_evergreen_fact(e, "1", 1, "t"), "evergreen fact revision failed", _generic, OK),
    (lambda p, e: p.forget_evergreen_fact(e, "1", 1, "r"), "evergreen fact forget failed", _generic, OK),
    (lambda p, e: p.review_evergreen_facts(e), "evergreen fact review failed", _generic, OK),
    (lambda p, e: p.search_conversation_memory(e, query="q"), "conversation memory search failed",
     lambda r: httpx.Response(200, json={"results": []}), '{"ok":true,"records":[]}'),
    (lambda p, e: p.get_conversation_record(e, memory_id=1), "conversation record read failed",
     lambda r: httpx.Response(200, json={"messages": []}), '{"ok":true,"records":[]}'),
    (lambda p, e: p.yumecho_add(e, "note"), "memo add failed", _generic, OK),
    (lambda p, e: p.yumecho_list(e), "memo list failed",
     lambda r: httpx.Response(200, json={"memos": []}), '{"ok":true,"memos":[]}'),
    (lambda p, e: p.yumecho_done(e, note_id=1, reason="done"), "memo done failed", _generic, OK),
]


@pytest.mark.parametrize("call, fallback, handler, expected", TOOLS, ids=[t[1] for t in TOOLS])
async def test_routing_guard_and_success(call, fallback, handler, expected, plugin_factory):
    def recording(request):
        raise AssertionError("no request expected")
    rejected = await plugin_factory(handler=recording, platform_id="telegram-sophia")
    assert await call(rejected, AstrMessageEvent("discord-sophia")) == ROUTING_ERROR
    plugin = await plugin_factory(handler=handler)
    plugin.latest_source_message_ids[AstrMessageEvent().unified_msg_origin] = 7
    assert await call(plugin, AstrMessageEvent()) == expected


@pytest.mark.parametrize("call, fallback, handler, expected", TOOLS, ids=[t[1] for t in TOOLS])
async def test_error_maps_to_fallback_and_logs(call, fallback, handler, expected, plugin_factory):
    def boom(request):
        raise RuntimeError("boom")
    logger.messages.clear()
    plugin = await plugin_factory(handler=boom)
    plugin.latest_source_message_ids[AstrMessageEvent().unified_msg_origin] = 7
    assert await call(plugin, AstrMessageEvent()) == f'{{"ok":false,"error":"{fallback}"}}'
    assert logger.messages == [f"[companion-gateway] {fallback.capitalize()}: boom"]


VALIDATION = [
    (lambda p, e: p.search_conversation_memory(e, query="   "), '{"ok":false,"error":"query is required"}'),
    (lambda p, e: p.yumecho_done(e, note_id=1, reason="   "), '{"ok":false,"error":"reason is required"}'),
    (lambda p, e: p.search_conversation_memory(e, query=123), AttributeError),
    (lambda p, e: p.remember_evergreen_fact(e, key="k", text="t", review_after=123), AttributeError),
]


@pytest.mark.parametrize("call, expected", VALIDATION,
                         ids=["blank query", "blank memo reason", "search query", "review_after"])
async def test_validation_ordering(call, expected, plugin_factory):
    plugin = await plugin_factory()
    if isinstance(expected, str):
        assert await call(plugin, AstrMessageEvent()) == expected
    else:
        with pytest.raises(expected, match="has no attribute 'strip'"):
            await call(plugin, AstrMessageEvent())
        assert await call(plugin, AstrMessageEvent("discord-sophia")) == ROUTING_ERROR


async def test_http_status_error_mapping(plugin_factory):
    plugin = await plugin_factory(handler=lambda r: httpx.Response(500, json={"detail": "upstream exploded"}))
    assert await plugin.review_evergreen_facts(AstrMessageEvent()) == (
        '{"ok":false,"error":"upstream exploded","status":500}'
    )
    plugin = await plugin_factory(handler=lambda r: httpx.Response(503, text="unparseable body"))
    assert await plugin.search_conversation_memory(AstrMessageEvent(), query="hello") == (
        '{"ok":false,"error":"conversation memory search failed","status":503}'
    )


def _capture(requests, response, body=False):
    def handler(request):
        requests.append(json.loads(request.read()) if body else dict(request.url.params))
        return httpx.Response(200, json=response)
    return handler


async def test_remember_payload(plugin_factory):
    payloads = []
    plugin = await plugin_factory(handler=_capture(payloads, {"id": 1}, body=True))
    await plugin.remember_evergreen_fact(AstrMessageEvent(), key="k", text="t")
    plugin.latest_source_message_ids[AstrMessageEvent().unified_msg_origin] = 7
    await plugin.remember_evergreen_fact(AstrMessageEvent(), key="user.name", text="Ada", priority=80,
                                         review_after="clear", expires_at="2027-01-01T00:00:00Z",
                                         reason="first")
    assert payloads[0] == {"key": "k", "text": "t", "priority": 50, "reason": ""}
    assert payloads[1] == {"key": "user.name", "text": "Ada", "priority": 80, "reason": "first",
                           "source_message_id": 7, "review_after": None,
                           "expires_at": "2027-01-01T00:00:00Z"}


async def test_search_flattens_hits_and_clamps_limit(plugin_factory):
    payloads = []
    hits = [{"messages": [{"id": i, "occurred_at": f"2026-01-01T00:00:0{i - 1}Z",
                           "role": role, "text": text}]}
            for i, role, text in [(1, "user", "hello"), (2, "assistant", "hi")]]
    plugin = await plugin_factory(handler=_capture(payloads, {"results": hits}, body=True))
    result = json.loads(await plugin.search_conversation_memory(AstrMessageEvent(), query="hello", limit=999))
    assert result == {"ok": True, "records": [
        {"memory_id": 1, "time": "2026-01-01T00:00:00Z", "role": "user", "text": "hello"},
        {"memory_id": 2, "time": "2026-01-01T00:00:01Z", "role": "assistant", "text": "hi"},
    ]}
    assert payloads == [{"query": "hello", "limit": 10, "context_messages": 0}]


async def test_get_record_and_review_clamp_params(plugin_factory):
    params = []
    plugin = await plugin_factory(handler=_capture(params, {"messages": [], "facts": []}))
    await plugin.get_conversation_record(AstrMessageEvent(), memory_id=5, context_messages=99)
    await plugin.review_evergreen_facts(AstrMessageEvent(), due_only=False, include_inactive=True, limit=1000)
    assert params == [{"context_messages": "10"},
                      {"due_only": "false", "include_inactive": "true", "limit": "100"}]


async def test_yumecho_payloads_and_reason_required(plugin_factory):
    posts = []
    params = []

    def handler(request):
        if request.method == "POST":
            posts.append(json.loads(request.read()))
        else:
            params.append(dict(request.url.params))
        return httpx.Response(200, json={"memo": {"id": 1}})

    plugin = await plugin_factory(handler=handler)
    await plugin.yumecho_add(AstrMessageEvent(), "buy milk")
    await plugin.yumecho_list(AstrMessageEvent(), status="archived", limit=999)
    await plugin.yumecho_done(AstrMessageEvent(), note_id=3, reason=" bought it ")
    assert posts == [{"text": "buy milk"}, {"reason": " bought it "}]
    assert params == [{"status": "archived", "limit": "500"}]
    assert await plugin.yumecho_done(AstrMessageEvent(), note_id=3, reason="  ") == (
        '{"ok":false,"error":"reason is required"}'
    )
    assert len(posts) == 2


def test_tool_decorators_register_the_nine_gateway_tools():
    assert list(filter_registry.tools) == list(_load_main().GATEWAY_TOOL_NAMES) == [
        "remember_evergreen_fact", "revise_evergreen_fact",
        "forget_evergreen_fact", "review_evergreen_facts", "search_conversation_memory",
        "get_conversation_record",
        "yumecho_add", "yumecho_list", "yumecho_done",
    ]


def _load_routing():
    spec = importlib.util.spec_from_file_location("astrbot_gateway_routing", _INTEGRATION_DIR / "routing.py")
    assert spec and spec.loader
    routing = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(routing)
    return routing


def test_routing_helpers():
    routing = _load_routing()
    assert routing.accepts_platform("discord-sophia", "discord-sophia") is True
    assert routing.accepts_platform("discord-sophia", "discord-sophia-backup") is False
    assert routing.accepts_platform("", "discord-sophia") is False
    assert routing.platform_id_from_umo("discord-sophia:FriendMessage:user-42") == "discord-sophia"


def test_the_affect_tool_is_removed():
    import ast

    tree = ast.parse((_INTEGRATION_DIR / "main.py").read_text())
    names = {node.name for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)}
    assert "record_affect_event" not in names
    assert "record_affect_event" not in _load_main().GATEWAY_TOOL_NAMES


class _ConversationManager:
    def __init__(self, cid="cid-1"):
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
        self.pairs.append((cid, user_message, assistant_message))


class _PersonaManager:
    async def get_default_persona_v3(self, umo=None):
        return {"prompt": "persona"}


class _Provider:
    def __init__(self, provider_id):
        self.provider_config = {"id": provider_id}


class _ProviderManager:
    def __init__(self, *provider_ids):
        self.providers = [_Provider(pid) for pid in provider_ids]

    def get_insts(self):
        return list(self.providers)


def _proactive_event(route="telegram-sophia:FriendMessage:user-1"):
    return {
        "id": "evt-1",
        "target": {"route": route},
        "generation_instruction": "Say hello.",
        "context": {"injection": "injection"},
        "reason": "silence",
        "silence_text": "10 minutes",
    }


def _install_segments():
    name = "astrbot.core.agent.message"
    if name not in sys.modules:
        module = types.ModuleType(name)

        class TextPart:
            def __init__(self, text):
                self.text = text

        class UserMessageSegment:
            def __init__(self, content):
                self.content = content

        class AssistantMessageSegment:
            def __init__(self, content):
                self.content = content

        module.TextPart = TextPart
        module.UserMessageSegment = UserMessageSegment
        module.AssistantMessageSegment = AssistantMessageSegment
        sys.modules[name] = module


async def _deliver_plugin(plugin_factory, *, send_result=True, cid="cid-1", generate=None, provider_ids=None):
    _install_segments()
    posts = []

    def handler(request):
        posts.append(json.loads(request.read()))
        return httpx.Response(200, json={"ok": True})

    plugin = await plugin_factory(handler=handler, platform_id="telegram-sophia")
    manager = _ConversationManager(cid=cid)
    plugin.context.conversation_manager = manager
    plugin.context.persona_manager = _PersonaManager()

    async def provider_id(route):
        return "prov"

    async def llm_generate(**kwargs):
        plugin.generated_prompts.append(kwargs.get("prompt", ""))
        return types.SimpleNamespace(completion_text="hello proactive")

    sends = []

    async def send_message(route, chain):
        sends.append(chain.text)
        return send_result

    plugin.generated_prompts = []
    plugin.sent_messages = sends
    if provider_ids is not None:
        plugin.context.provider_manager = _ProviderManager(*provider_ids)
    plugin.context.get_current_chat_provider_id = provider_id
    plugin.context.llm_generate = generate or llm_generate
    plugin.context.send_message = send_message
    return plugin, manager, posts


async def test_proactive_delivery_archives_history_and_acks_sent(plugin_factory):
    plugin, manager, posts = await _deliver_plugin(plugin_factory)
    await plugin._deliver(_proactive_event())
    assert len(manager.pairs) == 1
    cid, user_message, assistant_message = manager.pairs[0]
    assert cid == "cid-1"
    assert assistant_message.content[0].text == "hello proactive"
    assert "proactive" in user_message.content[0].text
    assert "yumecho" in plugin.generated_prompts[0]
    assert posts == [{"consumer": "astrbot", "outcome": "sent", "text": "hello proactive"}]


async def test_proactive_send_failure_skips_history_and_acks_failed(plugin_factory):
    plugin, manager, posts = await _deliver_plugin(plugin_factory, send_result=False)
    await plugin._deliver(_proactive_event())
    assert manager.pairs == []
    assert posts[0]["outcome"] == "failed"


async def test_proactive_delivery_creates_conversation_when_missing(plugin_factory):
    plugin, manager, posts = await _deliver_plugin(plugin_factory, cid=None)
    await plugin._deliver(_proactive_event())
    assert manager.created == ["telegram-sophia:FriendMessage:user-1"]
    assert manager.pairs[0][0] == "cid-new"
    assert posts[0]["outcome"] == "sent"


async def test_proactive_delivery_falls_back_to_next_provider(plugin_factory):
    calls = []

    async def generate(**kwargs):
        calls.append(kwargs["chat_provider_id"])
        if kwargs["chat_provider_id"] == "prov":
            raise RuntimeError("Connection error")
        return types.SimpleNamespace(completion_text="fallback text")

    plugin, manager, posts = await _deliver_plugin(
        plugin_factory, generate=generate, provider_ids=["prov", "backup"]
    )
    await plugin._deliver(_proactive_event())
    assert calls == ["prov", "backup"]
    assert plugin.sent_messages == ["fallback text"]
    assert manager.pairs[0][2].content[0].text == "fallback text"
    assert posts == [{"consumer": "astrbot", "outcome": "sent", "text": "fallback text"}]


async def test_proactive_delivery_all_providers_fail_acks_failed(plugin_factory):
    calls = []

    async def generate(**kwargs):
        calls.append(kwargs["chat_provider_id"])
        raise RuntimeError(f"Connection error: {kwargs['chat_provider_id']}")

    plugin, manager, posts = await _deliver_plugin(
        plugin_factory, generate=generate, provider_ids=["prov", "backup"]
    )
    await plugin._deliver(_proactive_event())
    assert calls == ["prov", "backup"]
    assert plugin.sent_messages == []
    assert manager.pairs == []
    assert posts == [
        {"consumer": "astrbot", "outcome": "failed", "error": "Connection error: backup"}
    ]


def _install_exceptions():
    name = "astrbot.core.exceptions"
    if name not in sys.modules:
        module = types.ModuleType(name)

        class ProviderNotFoundError(Exception):
            pass

        module.ProviderNotFoundError = ProviderNotFoundError
        sys.modules[name] = module
        sys.modules["astrbot.core"].exceptions = module
    return sys.modules[name]


async def test_tool_loop_passes_other_providers_as_in_runner_fallback(plugin_factory):
    _install_exceptions()
    plugin = await plugin_factory()
    plugin.context.provider_manager = _ProviderManager("prov", "backup")
    calls = []

    async def loop(**kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(completion_text="ok")

    await plugin._run_tool_loop(
        loop, "prompt", "system", "prov", object(), object(), plugin._provider_instances()
    )
    assert calls[0]["chat_provider_id"] == "prov"
    assert [p.provider_config["id"] for p in calls[0]["fallback_providers"]] == ["backup"]


async def test_tool_loop_relaxes_missing_pinned_provider(plugin_factory):
    exceptions = _install_exceptions()
    plugin = await plugin_factory()
    plugin.context.provider_manager = _ProviderManager("prov", "backup")
    calls = []

    async def loop(**kwargs):
        calls.append(kwargs["chat_provider_id"])
        if kwargs["chat_provider_id"] == "missing":
            raise exceptions.ProviderNotFoundError("nope")
        return types.SimpleNamespace(completion_text="ok")

    await plugin._run_tool_loop(
        loop, "prompt", "system", "missing", object(), object(), plugin._provider_instances()
    )
    assert calls == ["missing", "prov"]