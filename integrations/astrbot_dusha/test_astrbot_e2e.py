from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import sys
import types
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import httpx

from dusha import api
from dusha.config import ExternalPluginConfig, PluginEntry
from dusha.timeutil import isoformat, utc_now

_INTEGRATION_DIR = Path(__file__).resolve().parent
PLATFORM = "telegram-dusha"
ROUTE = "telegram-dusha:FriendMessage:user-1"
GATEWAY_URL = "http://127.0.0.1:8765"
NUDGE = "thinking of you"


# Stubs stand in for the external AstrBot framework and its LLM provider, the server runs for real.
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
    def __init__(self, completion_text="", role="assistant"):
        self.completion_text, self.role = completion_text, role


class ProviderRequest:
    def __init__(self, prompt="", system_prompt="", session_id=""):
        self.prompt, self.system_prompt, self.session_id = prompt, system_prompt, session_id


class TextPart:
    def __init__(self, text):
        self.text = text


class _Segment:
    def __init__(self, content):
        self.content = content


logger = types.SimpleNamespace(messages=[])
logger.warning = lambda message: logger.messages.append(message)

_STUBS = {
    "astrbot": {},
    "astrbot.api": {"logger": logger},
    "astrbot.api.star": {"Star": Star, "Context": object},
    "astrbot.api.event": {"MessageChain": MessageChain},
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


class _ConfigManager:
    def __init__(self, profiles):
        self.profiles = profiles

    def get_conf_info(self, umo):
        conf_id, name = self.profiles.get(umo, ("default", "default"))
        return {"id": conf_id, "name": name, "path": f"{conf_id}.json"}


class Context:
    def __init__(
        self,
        *,
        cid="cid-1",
        persona="persona",
        send_result=True,
        replies=None,
        provider_ids=(),
        pinned="",
        profiles=None,
    ):
        self.astrbot_config_mgr = _ConfigManager(profiles or {})
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


def _companion(**overrides):
    return {
        "label": "Main",
        "gateway_url": GATEWAY_URL,
        "platform_id": PLATFORM,
        "config_profile": "",
        "harness": "astrbot",
        "api_token": "",
        "poll_interval_seconds": 30,
        "proactive_enabled": True,
        "request_timeout_seconds": 60.0,
        **overrides,
    }


class _Rig:
    def __init__(self, app):
        self.app = app
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.plugins = []

    async def plugin(self, *, context=None, companions=None, apps=None):
        entries = [_companion()] if companions is None else copy.deepcopy(companions)
        plugin = plugin_main.DushaPlugin(
            context=context or Context(), config=AstrBotConfig(companions=entries)
        )
        plugin.gateways = {}
        for binding in plugin.bindings:
            app = (apps or {}).get(binding.base_url, self.app)
            gateway = plugin.gateways.setdefault(binding.base_url, _Gateway(app))
            timeout = binding.client.timeout
            await binding.client.aclose()
            binding.client = httpx.AsyncClient(transport=gateway, timeout=timeout)
        plugin.gateway = next(iter(plugin.gateways.values()), None) or _Gateway(self.app)
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


async def _say(rig, text, route=ROUTE, harness="astrbot"):
    return await rig.json(
        "POST",
        "/state/v1/messages",
        json={
            "harness": harness,
            "conversation_id": route,
            "route": route,
            "role": "user",
            "content": text,
        },
    )


def _backdate(app, past):
    with app.state.service.database.connect() as db:
        db.execute(
            "UPDATE affect_state SET last_user_message_at=?, last_updated_at=? WHERE id=1",
            (isoformat(past), isoformat(past)),
        )


async def _due_event(rig, text, route=ROUTE, harness="astrbot", hours=4):
    await _say(rig, text, route=route, harness=harness)
    _backdate(rig.app, utc_now() - timedelta(hours=hours, minutes=5))
    event = (await rig.json("POST", "/state/v1/proactive/evaluate"))["event"]
    assert event is not None
    return event


async def _poll_until(plugin, suffix, gateways=None):
    await plugin.initialize()
    async with asyncio.timeout(10):
        for gateway in gateways or [plugin.gateway]:
            while not any(path.endswith(suffix) for _, path, _ in gateway.calls):
                await asyncio.sleep(0.01)
    await plugin.terminate()


def _proactive_config(config):
    config.plugin("proactive").poll_interval_seconds = 3600
    config.plugin("proactive").cooldown_minutes = 0
    config.plugin("proactive").max_unanswered = 10
    config.plugin("proactive").max_per_day = 20
    return config


def test_the_plugin_registers_no_chat_hooks_or_gateway_tools():
    for name in ("GATEWAY_TOOL_NAMES", "PROACTIVE_YUMECHO_NOTE", "SETTING_DEFAULTS"):
        assert hasattr(plugin_main, name) is (name == "SETTING_DEFAULTS")
    for name in (
        "add_state_context",
        "store_response",
        "remember_evergreen_fact",
        "yumecho_add",
        "search_conversation_memory",
        "_run_tool_call",
        "_run_tool_loop",
        "_run_plugin_hooks",
    ):
        assert not hasattr(plugin_main.DushaPlugin, name)


async def test_companion_settings_take_schema_defaults(config):
    schema = json.loads((_INTEGRATION_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
    items = schema["companions"]["templates"]["companion"]["items"]
    assert list(schema) == ["companions"]
    assert schema["companions"]["default"] == []
    assert list(items)[0] == "label"
    assert set(items) == {
        "label",
        "gateway_url",
        "platform_id",
        "config_profile",
        "harness",
        "api_token",
        "poll_interval_seconds",
        "proactive_enabled",
        "request_timeout_seconds",
    }
    async with _running(_proactive_config(config)) as rig:
        default = (await rig.plugin(companions=[{"platform_id": PLATFORM}])).bindings[0]
        assert default.label == items["label"]["default"]
        assert default.base_url == items["gateway_url"]["default"] == GATEWAY_URL
        assert default.harness == items["harness"]["default"] == "astrbot"
        assert default.poll_interval_seconds == items["poll_interval_seconds"]["default"]
        assert default.proactive_enabled is items["proactive_enabled"]["default"]
        assert default.headers == {}
        assert default.client.timeout == httpx.Timeout(items["request_timeout_seconds"]["default"])

        longer = (await rig.plugin(companions=[_companion(request_timeout_seconds=90)])).bindings[0]
        assert longer.client.timeout == httpx.Timeout(90.0)

        clamped = (
            await rig.plugin(
                companions=[
                    _companion(
                        label="  Jardín  ",
                        gateway_url="http://gateway/",
                        harness="  ",
                        api_token="tok",
                        poll_interval_seconds=1,
                        request_timeout_seconds=0,
                    )
                ]
            )
        ).bindings[0]
        assert clamped.client.timeout == httpx.Timeout(1.0)
        assert (clamped.poll_interval_seconds, clamped.harness, clamped.base_url) == (
            5,
            "astrbot",
            "http://gateway",
        )
        assert (clamped.label, clamped.headers) == ("Jardín", {"X-Companion-Token": "tok"})


async def test_a_companion_without_a_platform_or_profile_is_disabled(config):
    async with _running(config) as rig:
        plugin = await rig.plugin(companions=[_companion(platform_id="", config_profile="")])
        assert plugin.bindings == []
        logger.messages.clear()
        await plugin.initialize()
        assert logger.messages == [
            "[dusha] No companion sets platform_id or config_profile. Routing is disabled"
        ]
        assert plugin.gateway.calls == []


async def test_proactive_event_is_delivered_acknowledged_sent_and_stored(config):
    async with _running(_proactive_config(config)) as rig:
        first = await rig.plugin()
        event = await _due_event(rig, "hello from turn m-1")
        await _poll_until(first, "/ack")
        context = first.context
        assert context.sent == [(ROUTE, NUDGE)]
        (generation,) = context.generations
        assert generation["chat_provider_id"] == "prov"
        assert generation["prompt"] == event["generation_instruction"]
        assert generation["system_prompt"].startswith("persona\n<affect>\n")
        assert "<companion_state>\n" in generation["system_prompt"]
        assert " user] hello from turn m-1" in generation["system_prompt"]
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
        polled = await rig.json(
            "GET", "/state/v1/proactive/events", params={"consumer": "astrbot", "harness": "astrbot"}
        )
        assert polled["events"] == []

        fallback = Context(
            cid=None,
            provider_ids=("prov", "backup"),
            replies={"prov": RuntimeError("Connection error"), "backup": "  fallback text  "},
        )
        second = await rig.plugin(context=fallback)
        await _due_event(rig, "hello from turn m-2")
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
            event = await _due_event(rig, f"hello from turn m-{index}", hours=4 + index)
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

        other = await rig.plugin(companions=[_companion(platform_id="telegram-other")])
        retry = await rig.plugin()
        await _due_event(rig, "hello from turn m-retry", hours=9)
        await _poll_until(other, "/ack")
        assert other.context.generations == []
        assert other.context.sent == []
        assert retry.context.sent == []
        assert await rig.search("thinking") == []


ALICE_URL = "http://alice:8765"
BRUNO_URL = "http://bruno:8765"
ALICE_ROUTE = "telegram-dusha:FriendMessage:alice-fan"
BRUNO_ROUTE = "telegram-dusha:FriendMessage:bruno-fan"
STRANGER_ROUTE = "telegram-dusha:FriendMessage:stranger"
DISCORD_BRUNO_ROUTE = "discord-dusha:FriendMessage:bruno-fan"
COMPANIONS = [
    {
        "label": "Alice",
        "gateway_url": ALICE_URL,
        "platform_id": PLATFORM,
        "config_profile": "Alice profile",
        "harness": "alice-bot",
    },
    {
        "label": "Bruno",
        "gateway_url": BRUNO_URL + "/",
        "platform_id": PLATFORM,
        "config_profile": "conf-b",
        "api_token": "bruno-secret",
        "harness": "bruno-bot",
    },
]


def _companion_config(config, tmp_path, name, token=""):
    companion = copy.deepcopy(_proactive_config(config))
    companion.home = companion.data_dir = tmp_path / name
    companion.data_dir.mkdir()
    companion.api_token = token
    return companion


async def test_each_companion_polls_and_delivers_its_own_events(config, tmp_path):
    alice_config = _companion_config(config, tmp_path, "alice")
    bruno_config = _companion_config(config, tmp_path, "bruno", token="bruno-secret")
    profiles = {
        ALICE_ROUTE: ("conf-a", "Alice profile"),
        BRUNO_ROUTE: ("conf-b", "Bruno profile"),
        DISCORD_BRUNO_ROUTE: ("conf-b", "Bruno profile"),
    }
    async with _running(alice_config) as alice, _running(bruno_config) as bruno:
        apps = {ALICE_URL: alice.app, BRUNO_URL: bruno.app}
        bruno.http.headers["X-Companion-Token"] = "bruno-secret"
        context = Context(profiles=dict(profiles))
        plugin = await alice.plugin(context=context, companions=COMPANIONS, apps=apps)
        assert [(item.label, item.base_url, item.harness) for item in plugin.bindings] == [
            ("Alice", ALICE_URL, "alice-bot"),
            ("Bruno", BRUNO_URL, "bruno-bot"),
        ]
        assert plugin._binding(PLATFORM, ALICE_ROUTE).label == "Alice"
        assert plugin._binding(PLATFORM, BRUNO_ROUTE).label == "Bruno"
        for route in (STRANGER_ROUTE, DISCORD_BRUNO_ROUTE):
            assert plugin._binding(route.partition(":")[0], route) is None

        await _say(alice, "water the ferns", route=ALICE_ROUTE, harness="alice-bot")
        await _say(bruno, "tune the cello", route=BRUNO_ROUTE, harness="bruno-bot")
        for rig in (alice, bruno):
            _backdate(rig.app, utc_now() - timedelta(hours=4, minutes=5))
            assert (await rig.json("POST", "/state/v1/proactive/evaluate"))["event"] is not None
        await _poll_until(plugin, "/ack", list(plugin.gateways.values()))
        assert sorted(context.sent) == [(ALICE_ROUTE, NUDGE), (BRUNO_ROUTE, NUDGE)]
        for rig, harness, route in (
            (alice, "alice-bot", ALICE_ROUTE),
            (bruno, "bruno-bot", BRUNO_ROUTE),
        ):
            stored = [item for item in await rig.search("thinking") if item["role"] == "assistant"]
            assert [(item["harness"], item["route"]) for item in stored] == [(harness, route)]
        assert [item["chat_provider_id"] for item in context.generations] == ["prov", "prov"]

        rerouted = Context(profiles={**profiles, ALICE_ROUTE: ("conf-b", "Bruno profile")})
        moved = await alice.plugin(context=rerouted, companions=COMPANIONS, apps=apps)
        _backdate(alice.app, utc_now() - timedelta(hours=8, minutes=5))
        event = (await alice.json("POST", "/state/v1/proactive/evaluate"))["event"]
        assert event["target"]["route"] == ALICE_ROUTE
        logger.messages.clear()
        await _poll_until(moved, "/ack", [moved.gateways[ALICE_URL]])
        assert rerouted.sent == [] and rerouted.generations == []
        assert logger.messages == [
            "[dusha] Proactive delivery failed: event target is not routed to this companion"
        ]
        assert moved.gateways[ALICE_URL].calls[-1] == (
            "POST",
            f"/state/v1/proactive/events/{event['id']}/ack",
            200,
        )
        assert (await alice.json("GET", "/state/v1/affect"))["unanswered_proactive"] == 1


PROBE_SOURCE = """\
from dusha.hooks import Block


def apply(ctx, config):
    ctx.on("context", lambda request: on_context(ctx, request))


def on_context(ctx, request):
    text = "Recalled lines\\n- PROBE_LINE " + request.query
    data = {"source": ctx.name, "text": text, "records": []}
    return Block(ctx.name, "<plugin_context>\\n" + text + "\\n</plugin_context>", data)
"""
PROBE_MANIFEST = (
    '[project]\nname = "probe"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n\n'
    '[tool.dusha]\nentry = "probe"\ninject = []\n'
)


async def test_external_plugin_block_text_is_injected_as_given(tmp_path, config):
    suite = tmp_path / "plugins" / "probe"
    suite.mkdir(parents=True)
    (suite / "probe.py").write_text(PROBE_SOURCE, encoding="utf-8")
    (suite / "pyproject.toml").write_text(PROBE_MANIFEST, encoding="utf-8")
    config.plugins_dir = str(tmp_path / "plugins")
    config.plugins["probe"] = PluginEntry(ExternalPluginConfig(options={}))
    async with _running(_proactive_config(config)) as rig:
        plugin = await rig.plugin()
        await _due_event(rig, "the ivory gate")
        await _poll_until(plugin, "/ack")
        (generation,) = plugin.context.generations
        assert "<plugin_context>\nRecalled lines\n- PROBE_LINE " in generation["system_prompt"]
        assert '{"source"' not in generation["system_prompt"]
