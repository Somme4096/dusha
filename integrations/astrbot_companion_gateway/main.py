from __future__ import annotations

import asyncio
import contextlib
import hashlib
from typing import Any

import astrbot.api.star as star
import httpx
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.core.config.astrbot_config import AstrBotConfig


class CompanionGatewayPlugin(star.Star):
    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.base_url = str(config.get("gateway_url", "http://127.0.0.1:8765")).rstrip("/")
        self.companion_id = str(config.get("companion_id", "sophia"))
        self.poll_seconds = max(5, int(config.get("poll_seconds", 30)))
        self.enable_proactive = bool(config.get("enable_proactive", True))
        token = str(config.get("api_token", ""))
        self.headers = {"X-Companion-Token": token} if token else {}
        self.client = httpx.AsyncClient(timeout=15)
        self.poll_task: asyncio.Task[None] | None = None

    async def initialize(self) -> None:
        if self.enable_proactive and self.poll_task is None:
            self.poll_task = asyncio.create_task(self._poll_loop(), name="companion-gateway-poll")

    async def terminate(self) -> None:
        if self.poll_task:
            self.poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.poll_task
        await self.client.aclose()

    def _message_key(self, event: AstrMessageEvent, prefix: str) -> str:
        platform_id = getattr(event.message_obj, "message_id", "")
        if platform_id:
            return f"{prefix}:{platform_id}"
        raw = f"{event.unified_msg_origin}:{event.created_at}:{event.get_message_str()}"
        return f"{prefix}:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self.client.post(self.base_url + path, headers=self.headers, json=payload)
        response.raise_for_status()
        return response.json()

    @filter.on_llm_request()
    async def add_state_context(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        prompt = req.prompt or event.get_message_str()
        if not prompt or "<companion_state>" in (req.system_prompt or ""):
            return
        try:
            stored = await self._post(
                "/state/v1/messages",
                {
                    "companion_id": self.companion_id,
                    "harness": "astrbot",
                    "conversation_id": event.unified_msg_origin,
                    "route": event.unified_msg_origin,
                    "role": "user",
                    "content": prompt,
                    "external_id": self._message_key(event, "user"),
                },
            )
            context = await self._post(
                "/state/v1/context",
                {
                    "companion_id": self.companion_id,
                    "harness": "astrbot",
                    "conversation_id": event.unified_msg_origin,
                    "query": prompt,
                    "exclude_message_ids": [stored["id"]],
                },
            )
            req.system_prompt = (req.system_prompt or "") + "\n" + context["injection"]
        except Exception as error:
            logger.warning(f"[companion-gateway] Context unavailable: {error}")

    @filter.on_llm_response()
    async def store_response(self, event: AstrMessageEvent, response: LLMResponse) -> None:
        text = (response.completion_text or "").strip()
        if not text:
            return
        try:
            await self._post(
                "/state/v1/messages",
                {
                    "companion_id": self.companion_id,
                    "harness": "astrbot",
                    "conversation_id": event.unified_msg_origin,
                    "route": event.unified_msg_origin,
                    "role": "assistant",
                    "content": text,
                    "external_id": (
                        self._message_key(event, "assistant")
                        + ":"
                        + hashlib.sha256(text.encode()).hexdigest()[:12]
                    ),
                },
            )
        except Exception as error:
            logger.warning(f"[companion-gateway] Response archive failed: {error}")

    async def _poll_loop(self) -> None:
        while True:
            try:
                response = await self.client.get(
                    self.base_url + "/state/v1/proactive/events",
                    headers=self.headers,
                    params={
                        "consumer": "astrbot",
                        "companion_id": self.companion_id,
                        "harness": "astrbot",
                        "limit": 1,
                    },
                )
                response.raise_for_status()
                for event in response.json().get("events", []):
                    await self._deliver(event)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(f"[companion-gateway] Proactive poll failed: {error}")
            await asyncio.sleep(self.poll_seconds)

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

    async def _deliver(self, event: dict[str, Any]) -> None:
        route = str(event["target"]["route"])
        try:
            persona = await self._persona_prompt(route)
            if not persona:
                raise RuntimeError("no AstrBot persona is configured for this session")
            provider_id = await self.context.get_current_chat_provider_id(route)
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=str(event["generation_instruction"]),
                system_prompt=persona + "\n" + str(event["context"]["injection"]),
            )
            text = (response.completion_text or "").strip()
            if not text:
                raise RuntimeError("the provider returned an empty message")
            sent = await self.context.send_message(route, MessageChain().message(text))
            if not sent:
                raise RuntimeError("AstrBot did not find the target platform")
            await self._post(
                f"/state/v1/proactive/events/{event['id']}/ack",
                {"consumer": "astrbot", "outcome": "sent", "text": text},
            )
        except Exception as error:
            logger.warning(f"[companion-gateway] Proactive delivery failed: {error}")
            with contextlib.suppress(Exception):
                await self._post(
                    f"/state/v1/proactive/events/{event['id']}/ack",
                    {"consumer": "astrbot", "outcome": "failed", "error": str(error)},
                )
