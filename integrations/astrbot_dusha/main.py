from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

import astrbot.api.star as star
import httpx
from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.core.config.astrbot_config import AstrBotConfig

from .rendering import render_turn
from .routing import accepts_route, platform_id_from_umo

_SCHEMA = json.loads((Path(__file__).parent / "_conf_schema.json").read_text(encoding="utf-8"))
SETTING_DEFAULTS = {
    name: field["default"] for name, field in _SCHEMA["companions"]["templates"]["companion"]["items"].items()
}


def _routable(settings: Mapping[str, Any]) -> bool:
    return any(str(settings.get(name) or "").strip() for name in ("platform_id", "config_profile"))


class Binding:
    def __init__(self, settings: Mapping[str, Any]) -> None:
        def setting(name: str) -> Any:
            return settings.get(name, SETTING_DEFAULTS[name])

        self.label = str(setting("label")).strip()
        self.base_url = str(setting("gateway_url")).rstrip("/")
        self.platform_id = str(setting("platform_id")).strip()
        self.config_profile = str(setting("config_profile")).strip()
        self.harness = str(setting("harness")).strip() or SETTING_DEFAULTS["harness"]
        self.poll_interval_seconds = max(5, int(setting("poll_interval_seconds")))
        self.proactive_enabled = bool(setting("proactive_enabled"))
        self.request_timeout_seconds = max(1.0, float(setting("request_timeout_seconds")))
        token = str(setting("api_token"))
        self.headers = {"X-Companion-Token": token} if token else {}
        self.client = httpx.AsyncClient(timeout=self.request_timeout_seconds)
        self.poll_task: asyncio.Task[None] | None = None
        self.companion_name = ""

    def accepts(self, platform_id: str, profiles: Collection[str]) -> bool:
        return accepts_route(self.platform_id, self.config_profile, platform_id, profiles)

    async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self.client.post(self.base_url + path, headers=self.headers, json=payload)
        response.raise_for_status()
        return response.json()

    async def get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        response = await self.client.get(self.base_url + path, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()


class DushaPlugin(star.Star):
    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        extra = config.get("companions", [])
        settings = [item for item in extra if isinstance(item, dict)] if isinstance(extra, list) else []
        self.bindings = [Binding(item) for item in settings if _routable(item)]

    async def initialize(self) -> None:
        if not self.bindings:
            logger.warning("[dusha] No companion sets platform_id or config_profile. Routing is disabled")
            return
        for binding in self.bindings:
            if binding.proactive_enabled and binding.poll_task is None:
                binding.poll_task = asyncio.create_task(
                    self._poll_loop(binding), name=f"dusha-poll-{binding.harness}"
                )

    async def terminate(self) -> None:
        for binding in self.bindings:
            if binding.poll_task:
                binding.poll_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await binding.poll_task
            await binding.client.aclose()

    def _profiles(self, umo: str) -> set[str]:
        manager = getattr(self.context, "astrbot_config_mgr", None)
        try:
            info = manager.get_conf_info(umo)
        except Exception:
            return set()
        return {str(info.get(key) or "").strip() for key in ("id", "name")} - {""}

    def _binding(self, platform_id: str, umo: str) -> Binding | None:
        profiles = self._profiles(umo) if any(item.config_profile for item in self.bindings) else set()
        return next((item for item in self.bindings if item.accepts(platform_id, profiles)), None)

    @staticmethod
    async def _companion_name(binding: Binding) -> str:
        if not binding.companion_name:
            try:
                health = await binding.get("/health", {})
            except Exception as error:
                logger.warning(f"[dusha] Companion name unavailable: {error}")
                return "assistant"
            binding.companion_name = str(health.get("companion") or "") or "assistant"
        return binding.companion_name

    async def _poll_loop(self, binding: Binding) -> None:
        while True:
            try:
                polled = await binding.get(
                    "/state/v1/proactive/events",
                    {"consumer": binding.harness, "harness": binding.harness, "limit": 1},
                )
                for event in polled.get("events", []):
                    await self._deliver(binding, event)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(f"[dusha] Proactive poll failed: {error}")
            await asyncio.sleep(binding.poll_interval_seconds)

    async def _persona_prompt(self, route: str) -> str:
        conversation = None
        conversation_id = await self.context.conversation_manager.get_curr_conversation_id(route)
        if conversation_id:
            conversation = await self.context.conversation_manager.get_conversation(route, conversation_id)
        if conversation and conversation.persona_id:
            persona = await self.context.persona_manager.get_persona(conversation.persona_id)
            if persona:
                return persona.system_prompt
        default = await self.context.persona_manager.get_default_persona_v3(umo=route)
        return str(default.get("prompt", "")) if default else ""

    async def _archive_proactive_history(self, route: str, text: str, event: dict[str, Any]) -> None:
        try:
            manager = getattr(self.context, "conversation_manager", None)
            if manager is None:
                return
            cid = await manager.get_curr_conversation_id(route)
            if not cid:
                new_conversation = getattr(manager, "new_conversation", None)
                if new_conversation is None:
                    return
                cid = await new_conversation(route)
                if not cid:
                    return
            try:
                from astrbot.core.agent.message import (
                    AssistantMessageSegment,
                    TextPart,
                    UserMessageSegment,
                )

                reason = str(event.get("reason", "silence") or "silence")
                silence = str(event.get("silence_text", "") or "").strip()
                marker = f"[proactive {reason}]" + (f" after {silence} of silence" if silence else "")
                user_message = UserMessageSegment(content=[TextPart(text=marker)])
                assistant_message = AssistantMessageSegment(content=[TextPart(text=text)])
            except Exception:
                reason = str(event.get("reason", "silence") or "silence")
                silence = str(event.get("silence_text", "") or "").strip()
                marker = f"[proactive {reason}]" + (f" after {silence} of silence" if silence else "")
                user_message = {"role": "user", "content": marker}  # type: ignore[assignment]
                assistant_message = {"role": "assistant", "content": text}  # type: ignore[assignment]
            await manager.add_message_pair(
                cid=cid,
                user_message=user_message,
                assistant_message=assistant_message,
            )
        except Exception as error:
            logger.warning(f"[dusha] Proactive history archive failed: {error}")

    @staticmethod
    def _provider_id(provider: Any) -> str:
        config = getattr(provider, "provider_config", None)
        candidate = config.get("id") if isinstance(config, dict) else getattr(config, "id", None)
        if not candidate:
            meta = getattr(provider, "meta", None)
            if callable(meta):
                with contextlib.suppress(Exception):
                    candidate = getattr(meta(), "id", None)
        return str(candidate).strip() if candidate else ""

    def _provider_instances(self) -> list[Any]:
        manager = getattr(self.context, "provider_manager", None)
        get_insts = getattr(manager, "get_insts", None) if manager is not None else None
        if not callable(get_insts):
            return []
        with contextlib.suppress(Exception):
            return list(get_insts() or [])
        return []

    async def _generate_with_fallback(self, prompt: str, system_prompt: str, pinned_id: str) -> LLMResponse:
        """Generate with the pinned provider, then retry each other provider in order."""
        tried: set[str] = set()

        async def _attempt(provider_id: str) -> LLMResponse:
            tried.add(provider_id)
            return await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=system_prompt,
            )

        last_error: Exception | None = None
        try:
            return await _attempt(pinned_id)
        except Exception as error:
            last_error = error
        for provider in self._provider_instances():
            candidate = self._provider_id(provider)
            if not candidate or candidate in tried:
                continue
            try:
                return await _attempt(candidate)
            except Exception as error:
                last_error = error
        if last_error is not None:
            raise last_error
        raise RuntimeError("no chat provider is available for proactive delivery")

    async def _deliver(self, binding: Binding, event: dict[str, Any]) -> None:
        route = str(event["target"]["route"])
        try:
            if self._binding(platform_id_from_umo(route), route) is not binding:
                raise RuntimeError("event target is not routed to this companion")
            persona = await self._persona_prompt(route)
            if not persona:
                raise RuntimeError("no AstrBot persona is configured for this session")
            provider_id = await self.context.get_current_chat_provider_id(route)
            context = event.get("context") or {}
            records = context.get("records") or []
            block = context.get("context") or {}
            injection = render_turn(
                block, list(records), await self._companion_name(binding)
            )
            request = ProviderRequest(
                prompt=str(event["generation_instruction"]),
                system_prompt=persona + "\n" + injection,
                session_id=route,
            )
            response = await self._generate_with_fallback(
                request.prompt or "", request.system_prompt, provider_id
            )
            text = (response.completion_text or "").strip()
            if not text:
                raise RuntimeError("the provider returned an empty message")
            sent = await self.context.send_message(route, MessageChain().message(text))
            if not sent:
                raise RuntimeError("AstrBot did not find the target platform")
            await self._archive_proactive_history(route, text, event)
            await binding.post(
                f"/state/v1/proactive/events/{event['id']}/ack",
                {"consumer": binding.harness, "outcome": "sent", "text": text},
            )
        except Exception as error:
            logger.warning(f"[dusha] Proactive delivery failed: {error}")
            with contextlib.suppress(Exception):
                await binding.post(
                    f"/state/v1/proactive/events/{event['id']}/ack",
                    {"consumer": binding.harness, "outcome": "failed", "error": str(error)},
                )
