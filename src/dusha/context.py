from __future__ import annotations

from typing import Any

from .hooks import Block
from .serialization import safe_json

CONTEXT_VERSION = 3


class ContextComposer:
    def __init__(self, *, prompts: dict[str, Any]) -> None:
        self.companion = prompts["companion_state"]

    def compose(self, blocks: list[Block]) -> dict[str, Any]:
        session = [record for block in blocks for record in block.session]
        instructions = {block.name: block.instruction for block in blocks if block.instruction}
        companion = (
            self.companion["open_delimiter"]
            + safe_json({"instructions": instructions, "session": session})
            + self.companion["close_delimiter"]
        )
        injection = "".join(block.text for block in blocks) + companion
        context = {
            "version": CONTEXT_VERSION,
            "instructions": instructions,
            "session": session,
            "blocks": {block.name: block.data for block in blocks},
        }
        return {"injection": injection, "records": session, "context": context}
