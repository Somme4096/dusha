from __future__ import annotations

import json
from datetime import datetime
from typing import Any

BRIDGE_NOTE = "Records from before this conversation."

_FACTS = ("<evergreen_facts>\n", "\n</evergreen_facts>")
_MEMOS = ("<memo_notes>\n", "\n</memo_notes>")
_AFFECT = ("<affect>\n", "\n</affect>")
_PLUGIN = ("<plugin_context>\n", "\n</plugin_context>")
_STATE = ("<companion_state>\n", "\n</companion_state>")
_BRIDGE = ("<previous_session>\n", "\n</previous_session>")

MARKERS = tuple(opening.strip() for opening, _ in (_FACTS, _MEMOS, _AFFECT, _PLUGIN, _STATE, _BRIDGE))


def _wrap(delimiters: tuple[str, str], body: str) -> str:
    return delimiters[0] + body + delimiters[1]


def _local_time(value: Any) -> str:
    try:
        return datetime.fromisoformat(str(value)).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(value)


def _indent(text: str) -> str:
    return str(text).replace("\n", "\n  ")


def render_facts(facts: list[dict[str, Any]]) -> str:
    lines = [f"- {fact['key']} [{fact['id']} r{fact['revision']}]: {_indent(fact['text'])}" for fact in facts]
    return _wrap(_FACTS, "\n".join(lines))


def render_memos(memos: list[dict[str, Any]]) -> str:
    return _wrap(_MEMOS, "\n".join(f"- #{memo['id']}: {_indent(memo['text'])}" for memo in memos))


def render_affect(block: dict[str, Any]) -> str:
    return _wrap(_AFFECT, str(block["preface"]) + "\n" + str(block["description"]))


def render_records(records: list[dict[str, Any]], companion: str) -> str:
    lines = []
    for record in records:
        who = companion if record.get("role") == "assistant" else str(record.get("role", "user"))
        stamp = _local_time(record["time"])
        lines.append(f"#{record['memory_id']} [{stamp} {who}] {_indent(record['text'])}")
    return "\n".join(lines)


def render_bridge(records: list[dict[str, Any]], instruction: str, companion: str) -> str:
    head = "\n".join(line for line in (instruction, BRIDGE_NOTE) if line)
    return _wrap(_BRIDGE, head + "\n" + render_records(records, companion))


def render_turn(context: dict[str, Any], records: list[dict[str, Any]], companion: str) -> str:
    parts: list[str] = []
    for name, data in context.get("blocks", {}).items():
        if name == "identity":
            parts.append(str(data.get("text", "")))
        elif name == "memory":
            continue
        elif name == "evergreen":
            parts.append(render_facts(data))
        elif name == "memo":
            parts.append(render_memos(data))
        elif name == "affect":
            parts.append(render_affect(data))
        elif isinstance(data, dict) and data.get("text"):
            parts.append(_wrap(_PLUGIN, str(data["text"])))
        else:
            parts.append(_wrap(_PLUGIN, json.dumps(data, ensure_ascii=False, separators=(",", ":"))))
    if records:
        instruction = str(context.get("instructions", {}).get("memory", ""))
        head = instruction + "\n" if instruction else ""
        parts.append(_wrap(_STATE, head + render_records(records, companion)))
    return "\n".join(part for part in parts if part)
