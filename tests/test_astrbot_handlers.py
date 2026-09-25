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
MessageChain = type("MessageChain", (), {})
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
                 "astrbot.core", "astrbot.core.config", "astrbot.core.config.astrbot_config")
        modules = {name: types.ModuleType(name) for name in names}
        modules["astrbot.api.star"].Star, modules["astrbot.api.star"].Context = Star, Context
        modules["astrbot.api"].logger = logger
        modules["astrbot.api.event"].AstrMessageEvent = AstrMessageEvent
        modules["astrbot.api.event"].MessageChain = MessageChain
        modules["astrbot.api.event"].filter = filter_registry
        modules["astrbot.api.provider"].LLMResponse = LLMResponse
        modules["astrbot.api.provider"].ProviderRequest = ProviderRequest
        modules["astrbot.core.config.astrbot_config"].AstrBotConfig = AstrBotConfig
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
    (lambda p, e: p.search_conversation_memory(e, query=123), AttributeError),
    (lambda p, e: p.remember_evergreen_fact(e, key="k", text="t", review_after=123), AttributeError),
]


@pytest.mark.parametrize("call, expected", VALIDATION,
                         ids=["blank query", "search query", "review_after"])
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


def test_tool_decorators_register_the_six_gateway_tools():
    assert list(filter_registry.tools) == list(_load_main().GATEWAY_TOOL_NAMES) == [
        "remember_evergreen_fact", "revise_evergreen_fact",
        "forget_evergreen_fact", "review_evergreen_facts", "search_conversation_memory",
        "get_conversation_record",
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