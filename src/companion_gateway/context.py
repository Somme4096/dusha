"""Structured context composition: injection string and context metadata.

The injection is assembled as: raw identity text (optional), the evergreen
facts block (optional, facts selected greedily to fit), then a single JSON
companion state block with memory instruction, emotion values, and the selected
session records. The mandatory identity and companion state are never truncated;
if they cannot fit the configured budget, a ContextBudgetError is raised.
Evergreen facts and session records are selected greedily using their real
serialized sizes so the injection stays valid JSON and within budget. A final
guard raises ContextBudgetError if the composed injection ever exceeds the
budget.
"""

from __future__ import annotations

from typing import Any

from .serialization import safe_json

CONTEXT_VERSION = 1


class ContextBudgetError(ValueError):
    """Raised when mandatory context exceeds the configured injection budget."""


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
    ) -> None:
        self.prompts = prompts
        self.budget = int(budget)
        self.identity_text = identity_text
        self.identity_configured = identity_configured
        self.identity_revision = identity_revision
        self.emotions_fingerprint = emotions_fingerprint
        self.prompts_fingerprint = prompts_fingerprint

    def _instructions_and_emotion(
        self, affect_snapshot: dict[str, Any], affect_text: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Shared instructions and emotion metadata for injection and context."""
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

    def _companion_payload(
        self, affect_snapshot: dict[str, Any], affect_text: str, session: list[dict[str, Any]]
    ) -> dict[str, Any]:
        instructions, emotion = self._instructions_and_emotion(affect_snapshot, affect_text)
        return {"instructions": instructions, "emotion": emotion, "session": session}

    def _companion_block(
        self, affect_snapshot: dict[str, Any], affect_text: str, session: list[dict[str, Any]]
    ) -> str:
        companion = self.prompts["companion_state"]
        payload = self._companion_payload(affect_snapshot, affect_text, session)
        return companion["open_delimiter"] + safe_json(payload) + companion["close_delimiter"]

    def _evergreen_block(self, facts: list[dict[str, Any]]) -> str:
        if not facts:
            return ""
        evergreen = self.prompts["evergreen"]
        return evergreen["open_delimiter"] + safe_json(facts) + evergreen["close_delimiter"]

    def compose(
        self,
        *,
        affect_snapshot: dict[str, Any],
        affect_text: str,
        evergreen_facts: list[dict[str, Any]],
        session_records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        empty_companion = self._companion_block(affect_snapshot, affect_text, [])
        mandatory_len = len(self.identity_text) + len(empty_companion)
        if mandatory_len > self.budget:
            raise ContextBudgetError(
                f"context budget {self.budget} characters is too small for mandatory "
                f"identity and companion state (requires {mandatory_len} characters)"
            )

        # Evergreen facts are selected greedily against the residual after the
        # mandatory identity and empty companion block are reserved.
        used_evergreen: list[dict[str, Any]] = []
        evergreen_len = 0
        for fact in evergreen_facts:
            trial_block = self._evergreen_block(used_evergreen + [fact])
            if mandatory_len + len(trial_block) <= self.budget:
                used_evergreen = used_evergreen + [fact]
                evergreen_len = len(trial_block)
            else:
                break

        fixed_len = len(self.identity_text) + evergreen_len
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
        injection = self.identity_text + self._evergreen_block(used_evergreen) + companion_block
        if len(injection) > self.budget:
            raise ContextBudgetError(
                f"context budget {self.budget} characters exceeded while composing "
                f"(requires {len(injection)} characters)"
            )

        instructions, emotion = self._instructions_and_emotion(affect_snapshot, affect_text)
        context = {
            "version": CONTEXT_VERSION,
            "identity": {
                "configured": self.identity_configured,
                "text": self.identity_text,
                "revision": self.identity_revision,
            },
            "instructions": instructions,
            "memory": {"evergreen": used_evergreen, "session": used_records},
            "emotion": emotion,
        }
        return {
            "injection": injection,
            "records": used_records,
            "evergreen_facts": used_evergreen,
            "context": context,
        }