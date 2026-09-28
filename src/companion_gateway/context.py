from __future__ import annotations

from typing import Any

from .serialization import safe_json

CONTEXT_VERSION = 1


class ContextBudgetError(ValueError):
    pass


class ContextComposer:
    def __init__(
        self,
        *,
        prompts: dict[str, Any],
        budget: int,
        identity_text: str,
        identity_configured: bool,
        identity_revision: str,
        emotions_fingerprint: str,
        prompts_fingerprint: str,
        plugin_context_max_chars: int = 0,
    ) -> None:
        self.prompts = prompts
        self.budget = int(budget)
        self.identity_text = identity_text
        self.identity_configured = identity_configured
        self.identity_revision = identity_revision
        self.emotions_fingerprint = emotions_fingerprint
        self.prompts_fingerprint = prompts_fingerprint
        self.plugin_context_max_chars = int(plugin_context_max_chars)

    def _instructions_and_emotion(
        self, affect_snapshot: dict[str, Any], affect_text: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        companion = self.prompts["companion_state"]
        instructions = {"memory": companion["memory_instruction"]}
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
        self, affect_snapshot: dict[str, Any], affect_text: str, session: list[dict[str, Any]]
    ) -> str:
        companion = self.prompts["companion_state"]
        instructions, emotion = self._instructions_and_emotion(affect_snapshot, affect_text)
        payload = {"instructions": instructions, "emotion": emotion, "session": session}
        return companion["open_delimiter"] + safe_json(payload) + companion["close_delimiter"]

    def _evergreen_block(self, facts: list[dict[str, Any]]) -> str:
        if not facts:
            return ""
        evergreen = self.prompts["evergreen"]
        return evergreen["open_delimiter"] + safe_json(facts) + evergreen["close_delimiter"]

    @staticmethod
    def _plugin_block(plugin_context: dict[str, Any]) -> str:
        payload: dict[str, Any] = {
            "source": plugin_context.get("source", "memory_plugin"),
            "text": plugin_context.get("text", ""),
        }
        records = plugin_context.get("records") or []
        if records:
            payload["records"] = records
        return "<memory_context>" + safe_json(payload) + "</memory_context>"

    def _plugin_render(
        self, plugin_context: dict[str, Any], available: int
    ) -> tuple[str, dict[str, Any] | None]:
        source = plugin_context.get("source", "memory_plugin")
        text = plugin_context.get("text") or ""
        records = list(plugin_context.get("records") or [])
        if available <= 0 or (not text and not records):
            return "", None
        candidate: dict[str, Any] = {"source": source, "text": text, "records": records}
        block = self._plugin_block(candidate)
        if len(block) <= available:
            return block, candidate
        while records:
            records = records[:-1]
            candidate = {"source": source, "text": text, "records": records}
            block = self._plugin_block(candidate)
            if len(block) <= available:
                return block, candidate
        candidate = {"source": source, "text": text, "records": []}
        while text:
            overflow = len(self._plugin_block(candidate)) - available
            if overflow <= 0:
                return self._plugin_block(candidate), candidate
            text = text[: max(0, len(text) - max(1, overflow))]
            candidate = {"source": source, "text": text, "records": []}
        return "", None

    def compose(
        self,
        *,
        affect_snapshot: dict[str, Any],
        affect_text: str,
        evergreen_facts: list[dict[str, Any]],
        session_records: list[dict[str, Any]],
        plugin_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        empty_companion = self._companion_block(affect_snapshot, affect_text, [])
        mandatory_len = len(self.identity_text) + len(empty_companion)
        if mandatory_len > self.budget:
            raise ContextBudgetError(
                f"context budget {self.budget} characters is too small for mandatory "
                f"identity and companion state (requires {mandatory_len} characters)"
            )

        used_evergreen: list[dict[str, Any]] = []
        evergreen_len = 0
        for fact in evergreen_facts:
            trial_block = self._evergreen_block(used_evergreen + [fact])
            if mandatory_len + len(trial_block) <= self.budget:
                used_evergreen = used_evergreen + [fact]
                evergreen_len = len(trial_block)
            else:
                break

        used_plugin: dict[str, Any] | None = None
        plugin_block = ""
        if plugin_context and self.plugin_context_max_chars > 0:
            available = self.budget - mandatory_len - evergreen_len
            plugin_block, used_plugin = self._plugin_render(plugin_context, available)

        fixed_len = len(self.identity_text) + evergreen_len + len(plugin_block)
        session: list[dict[str, Any]] = []
        used_records: list[dict[str, Any]] = []
        for record in session_records:
            trial = self._companion_block(affect_snapshot, affect_text, session + [record])
            if fixed_len + len(trial) <= self.budget:
                session.append(record)
                used_records.append(record)
            else:
                break

        companion_block = self._companion_block(affect_snapshot, affect_text, session)
        injection = (
            self.identity_text
            + self._evergreen_block(used_evergreen)
            + plugin_block
            + companion_block
        )
        if len(injection) > self.budget:
            raise ContextBudgetError(
                f"context budget {self.budget} characters exceeded while composing "
                f"(requires {len(injection)} characters)"
            )

        instructions, emotion = self._instructions_and_emotion(affect_snapshot, affect_text)
        memory: dict[str, Any] = {"evergreen": used_evergreen, "session": used_records}
        if used_plugin is not None:
            memory["plugin_context"] = used_plugin
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
            "records": used_records,
            "evergreen_facts": used_evergreen,
            "plugin_context": used_plugin,
            "context": context,
        }
