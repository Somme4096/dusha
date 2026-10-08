"""DeepSeek harness adapter for the Dusha gateway."""

from __future__ import annotations

import logging
from typing import Any

from .client import DushaClient
from .config import DeepSeekHarnessConfig
from .tools import ToolsetMixin

logger = logging.getLogger("deepseek-harness-dusha")

_STATE_MARKER = "<companion_state>"


def external_id(conversation_id: str, message_id: str, text: str) -> str:
    source = f"{conversation_id}:{message_id}" if message_id else f"{conversation_id}:{text}"
    value = 0x811C9DC5
    for char in source:
        value ^= ord(char)
        value = (value * 0x01000193) & 0xFFFFFFFF
    return f"deepseek-harness:{value:x}"


class DeepSeekHarnessAdapter(ToolsetMixin):
    def __init__(self, config: DeepSeekHarnessConfig, *, client: Any = None) -> None:
        self.config = config
        self.client = client or DushaClient(config)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def ingest_user_message(self, *, conversation_id: str, text: str, message_id: str = "") -> Any:
        return await self.client.ingest_message(
            harness=self.config.harness,
            conversation_id=conversation_id,
            role="user",
            content=text,
            route=self.config.route,
            external_id=external_id(conversation_id, message_id, text),
        )

    async def handle_turn(
        self,
        prompt: str,
        conversation_id: str,
        *,
        message_id: str = "",
        existing_context: str = "",
    ) -> str:
        if not self.config.auto_inject:
            return ""
        if not isinstance(prompt, str) or not prompt.strip():
            return ""
        if _STATE_MARKER in existing_context:
            return ""
        exclude: list[int] = []
        try:
            stored = await self.ingest_user_message(
                conversation_id=conversation_id, text=prompt, message_id=message_id
            )
            stored_id = stored.get("id") if isinstance(stored, dict) else None
            if isinstance(stored_id, int):
                exclude.append(stored_id)
        except Exception as error:
            logger.warning("[deepseek-harness] message archive failed: %s", error)
        try:
            response = await self.client.build_context(
                harness=self.config.harness,
                conversation_id=conversation_id,
                query=prompt,
                exclude_message_ids=exclude,
                include_recent=True,
            )
        except Exception as error:
            logger.warning("[deepseek-harness] context unavailable: %s", error)
            return ""
        injection = response.get("injection") if isinstance(response, dict) else None
        return injection.strip() if isinstance(injection, str) else ""

    async def archive_reply(self, conversation_id: str, text: str, *, message_id: str = "") -> None:
        if not isinstance(text, str) or not text.strip():
            return
        try:
            await self.client.ingest_message(
                harness=self.config.harness,
                conversation_id=conversation_id,
                role="assistant",
                content=text,
                route=self.config.route,
                external_id=external_id(conversation_id, message_id, text),
            )
        except Exception as error:
            logger.warning("[deepseek-harness] response archive failed: %s", error)
