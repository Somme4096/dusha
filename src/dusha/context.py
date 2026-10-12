from __future__ import annotations

from typing import Any

from .serialization import safe_json

CONTEXT_VERSION = 1


class ContextComposer:
    def __init__(
        self,
        *,
        prompts: dict[str, Any],
        identity_text: str,
        identity_configured: bool,
        identity_revision: str,
        emotions_fingerprint: str,
        prompts_fingerprint: str,
        memo_max_items: int = 0,
        memo_max_chars: int = 0,
    ) -> None:
        self.prompts = prompts
        self.identity_text = identity_text
        self.identity_configured = identity_configured
        self.identity_revision = identity_revision
        self.emotions_fingerprint = emotions_fingerprint
        self.prompts_fingerprint = prompts_fingerprint
        self.memo_max_items = int(memo_max_items)
        self.memo_max_chars = int(memo_max_chars)

    def _instructions_and_emotion(
        self, affect_snapshot: dict[str, Any] | None, affect_text: str
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        companion = self.prompts["companion_state"]
        instructions = {"memory": companion["memory_instruction"]}
        if affect_snapshot is None:
            return instructions, None
        emotion = {
            "values": {
                "base": affect_snapshot["base"],
                "mood": affect_snapshot["mood"],
            },
            "description": affect_text,
            "preface": companion["affect_instruction"],
            "fingerprints": {
                "emotions": self.emotions_fingerprint,
                "prompts": self.prompts_fingerprint,
            },
        }
        return instructions, emotion

    def _companion_block(
        self,
        affect_snapshot: dict[str, Any] | None,
        affect_text: str,
        session: list[dict[str, Any]],
    ) -> str:
        companion = self.prompts["companion_state"]
        instructions, emotion = self._instructions_and_emotion(affect_snapshot, affect_text)
        payload: dict[str, Any] = {"instructions": instructions}
        if emotion is not None:
            payload["emotion"] = emotion
        payload["session"] = session
        return companion["open_delimiter"] + safe_json(payload) + companion["close_delimiter"]

    def _evergreen_block(self, facts: list[dict[str, Any]]) -> str:
        if not facts:
            return ""
        evergreen = self.prompts["evergreen"]
        return evergreen["open_delimiter"] + safe_json(facts) + evergreen["close_delimiter"]

    def _memo_block(self, notes: list[dict[str, Any]]) -> str:
        if not notes:
            return ""
        blocks = self.prompts["context_blocks"]
        return blocks["memo_open_delimiter"] + safe_json(notes) + blocks["memo_close_delimiter"]

    def _plugin_block(self, plugin_context: dict[str, Any]) -> str:
        payload: dict[str, Any] = {
            "source": plugin_context.get("source", "memory_plugin"),
            "text": plugin_context.get("text", ""),
        }
        records = plugin_context.get("records") or []
        if records:
            payload["records"] = records
        blocks = self.prompts["context_blocks"]
        return blocks["memory_open_delimiter"] + safe_json(payload) + blocks["memory_close_delimiter"]

    def compose(
        self,
        *,
        affect_snapshot: dict[str, Any] | None,
        affect_text: str,
        evergreen_facts: list[dict[str, Any]],
        session_records: list[dict[str, Any]],
        plugin_context: dict[str, Any] | None = None,
        memo_notes: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        used_memos: list[dict[str, Any]] = []
        for note in list(memo_notes or [])[: self.memo_max_items]:
            if len(self._memo_block(used_memos + [note])) > self.memo_max_chars:
                break
            used_memos = used_memos + [note]

        plugin_block = self._plugin_block(plugin_context) if plugin_context else ""
        injection = (
            self.identity_text
            + self._evergreen_block(evergreen_facts)
            + plugin_block
            + self._memo_block(used_memos)
            + self._companion_block(affect_snapshot, affect_text, session_records)
        )

        instructions, emotion = self._instructions_and_emotion(affect_snapshot, affect_text)
        memory: dict[str, Any] = {
            "evergreen": evergreen_facts,
            "session": session_records,
            "memo_notes": used_memos,
        }
        if plugin_context:
            memory["plugin_context"] = plugin_context
        context = {
            "version": CONTEXT_VERSION,
            "identity": {
                "configured": self.identity_configured,
                "text": self.identity_text,
                "revision": self.identity_revision,
            },
            "instructions": instructions,
            "memory": memory,
            "emotion": emotion,
        }
        return {
            "injection": injection,
            "records": session_records,
            "evergreen_facts": evergreen_facts,
            "plugin_context": plugin_context,
            "context": context,
        }
