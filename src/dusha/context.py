from __future__ import annotations

from typing import Any

from .hooks import Block
from .serialization import safe_json

CONTEXT_VERSION = 2


class ContextComposer:
    def __init__(
        self,
        *,
        prompts: dict[str, Any],
        identity_text: str,
        identity_configured: bool,
        identity_revision: str,
    ) -> None:
        self.companion = prompts["companion_state"]
        self.identity_text = identity_text
        self.identity_configured = identity_configured
        self.identity_revision = identity_revision

    def compose(self, *, session_records: list[dict[str, Any]], blocks: list[Block]) -> dict[str, Any]:
        instructions = {"memory": self.companion["memory_instruction"]}
        companion = (
            self.companion["open_delimiter"]
            + safe_json({"instructions": instructions, "session": session_records})
            + self.companion["close_delimiter"]
        )
        injection = self.identity_text + "".join(block.text for block in blocks) + companion
        context = {
            "version": CONTEXT_VERSION,
            "identity": {
                "configured": self.identity_configured,
                "text": self.identity_text,
                "revision": self.identity_revision,
            },
            "instructions": instructions,
            "session": session_records,
            "blocks": {block.name: block.data for block in blocks},
        }
        return {"injection": injection, "records": session_records, "context": context}
