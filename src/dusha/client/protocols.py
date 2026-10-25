from __future__ import annotations

import contextlib
import json
from typing import Any


def _prefixed(content: Any, injection: str, part: str) -> Any:
    if isinstance(content, list):
        return [{"type": part, "text": injection}, *content]
    return f"{injection}\n\n{content}" if content else injection


def _last(items: list[Any], match: Any) -> int | None:
    return next((index for index in range(len(items) - 1, -1, -1) if match(items[index])), None)


def _is(item: Any, **fields: Any) -> bool:
    return isinstance(item, dict) and all(item.get(key) == value for key, value in fields.items())


class ChatCompletions:
    kind = "chat"

    def transcript(self, body: dict[str, Any]) -> list[Any] | None:
        messages = body.get("messages")
        return messages if isinstance(messages, list) else None

    def inject(self, body: dict[str, Any], injection: str) -> dict[str, Any]:
        messages = list(body["messages"])
        index = _last(messages, lambda item: _is(item, role="user"))
        if index is None:
            position = next(
                (i for i, item in enumerate(messages) if not _is(item, role="system")), len(messages)
            )
            messages.insert(position, {"role": "system", "content": injection})
        else:
            message = messages[index]
            messages[index] = {**message, "content": _prefixed(message.get("content"), injection, "text")}
        return {**body, "messages": messages}

    def reply(self, data: Any) -> dict[str, Any] | None:
        with contextlib.suppress(Exception):
            return data["choices"][0]["message"]
        return None

    def stream_reply(self, events: list[dict[str, Any]]) -> dict[str, Any] | None:
        parts: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        for event in events:
            with contextlib.suppress(Exception):
                delta = event["choices"][0]["delta"]
                if isinstance(delta.get("content"), str):
                    parts.append(delta["content"])
                for call in delta.get("tool_calls") or []:
                    empty = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                    slot = calls.setdefault(call.get("index", 0), empty)
                    slot["id"] = call.get("id") or slot["id"]
                    slot["type"] = call.get("type") or slot["type"]
                    function = call.get("function") or {}
                    slot["function"]["name"] += function.get("name") or ""
                    slot["function"]["arguments"] += function.get("arguments") or ""
        if not parts and not calls:
            return None
        reply: dict[str, Any] = {"role": "assistant", "content": "".join(parts) or None}
        if calls:
            reply["tool_calls"] = [calls[index] for index in sorted(calls)]
        return reply

    def response_id(self, data: Any) -> str:
        return ""

    def previous(self, body: dict[str, Any]) -> str:
        return ""


def _anthropic_message(message: Any) -> list[dict[str, Any]]:
    if not isinstance(message, dict):
        return []
    role, content = message.get("role"), message.get("content")
    if role == "user" and isinstance(content, str):
        return [{"role": "user", "content": content}]
    blocks = content if isinstance(content, list) else []
    if role == "user":
        results = [block for block in blocks if _is(block, type="tool_result")]
        output = [
            {"role": "tool", "tool_call_id": block.get("tool_use_id", ""), "content": block.get("content")}
            for block in results
        ]
        rest = [block for block in blocks if not _is(block, type="tool_result")]
        return [*output, {"role": "user", "content": rest}] if rest else output
    if role == "assistant":
        if isinstance(content, str):
            blocks = [{"type": "text", "text": content}]
        calls = [
            {
                "id": block.get("id", ""),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                },
            }
            for block in blocks
            if _is(block, type="tool_use")
        ]
        reply: dict[str, Any] = {
            "role": "assistant",
            "content": [block for block in blocks if not _is(block, type="tool_use")] or None,
        }
        if calls:
            reply["tool_calls"] = calls
        return [reply]
    return []


def _asks(message: Any) -> bool:
    if not _is(message, role="user"):
        return False
    content = message.get("content")
    if isinstance(content, str):
        return True
    return isinstance(content, list) and any(not _is(block, type="tool_result") for block in content)


class AnthropicMessages:
    kind = "messages"

    def transcript(self, body: dict[str, Any]) -> list[Any] | None:
        messages = body.get("messages")
        if not isinstance(messages, list):
            return None
        return [converted for message in messages for converted in _anthropic_message(message)]

    def inject(self, body: dict[str, Any], injection: str) -> dict[str, Any]:
        messages = list(body["messages"])
        index = _last(messages, _asks)
        if index is None:
            system = body.get("system")
            if isinstance(system, list):
                return {**body, "system": [*system, {"type": "text", "text": injection}]}
            return {**body, "system": f"{system}\n\n{injection}" if system else injection}
        message = messages[index]
        content = message["content"]
        if isinstance(content, str):
            content = f"{injection}\n\n{content}" if content else injection
        else:
            lead = next((i for i, block in enumerate(content) if not _is(block, type="tool_result")), 0)
            content = [*content[:lead], {"type": "text", "text": injection}, *content[lead:]]
        messages[index] = {**message, "content": content}
        return {**body, "messages": messages}

    def reply(self, data: Any) -> dict[str, Any] | None:
        if not isinstance(data, dict) or not isinstance(data.get("content"), list):
            return None
        return _anthropic_message({"role": "assistant", "content": data["content"]})[0]

    def stream_reply(self, events: list[dict[str, Any]]) -> dict[str, Any] | None:
        blocks: dict[int, dict[str, Any]] = {}
        inputs: dict[int, str] = {}
        for event in events:
            index = event.get("index")
            if event.get("type") == "content_block_start" and isinstance(event.get("content_block"), dict):
                blocks[index] = dict(event["content_block"])
            elif event.get("type") == "content_block_delta" and index in blocks:
                block, delta = blocks[index], event.get("delta") or {}
                for kind, field in (("text_delta", "text"), ("thinking_delta", "thinking"),
                                    ("signature_delta", "signature")):
                    if delta.get("type") == kind:
                        block[field] = block.get(field, "") + str(delta.get(field, ""))
                if delta.get("type") == "input_json_delta":
                    inputs[index] = inputs.get(index, "") + str(delta.get("partial_json", ""))
        for index, raw in inputs.items():
            with contextlib.suppress(ValueError):
                blocks[index]["input"] = json.loads(raw) if raw else {}
        if not blocks:
            return None
        return self.reply({"content": [blocks[index] for index in sorted(blocks)]})

    def response_id(self, data: Any) -> str:
        return ""

    def previous(self, body: dict[str, Any]) -> str:
        return ""


def _responses_items(items: list[Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    group: dict[str, list[Any]] | None = None

    def flush() -> None:
        nonlocal group
        if group is not None:
            reply: dict[str, Any] = {"role": "assistant", "content": group["content"] or None}
            if group["calls"]:
                reply["tool_calls"] = group["calls"]
            output.append(reply)
        group = None

    for item in items:
        if not isinstance(item, dict):
            continue
        kind, role, content = item.get("type", "message"), item.get("role"), item.get("content")
        if kind == "message" and role == "user":
            flush()
            output.append({"role": "user", "content": content})
        elif kind == "message" and role == "assistant":
            group = group or {"content": [], "calls": []}
            if isinstance(content, str):
                content = [{"type": "output_text", "text": content}]
            group["content"].extend(content if isinstance(content, list) else [])
        elif kind == "function_call":
            group = group or {"content": [], "calls": []}
            group["calls"].append(
                {
                    "id": item.get("call_id", ""),
                    "type": "function",
                    "function": {"name": item.get("name", ""), "arguments": item.get("arguments", "")},
                }
            )
        elif kind == "function_call_output":
            flush()
            call = item.get("call_id", "")
            output.append({"role": "tool", "tool_call_id": call, "content": item.get("output")})
    flush()
    return output


class OpenAIResponses:
    kind = "responses"

    def transcript(self, body: dict[str, Any]) -> list[Any] | None:
        items = body.get("input")
        if isinstance(items, str):
            return [{"role": "user", "content": items}]
        return _responses_items(items) if isinstance(items, list) else None

    def inject(self, body: dict[str, Any], injection: str) -> dict[str, Any]:
        items = body["input"]
        if isinstance(items, str):
            return {**body, "input": f"{injection}\n\n{items}" if items else injection}
        index = _last(items, lambda item: _is(item, role="user") and item.get("type", "message") == "message")
        if index is None:
            instructions = body.get("instructions")
            return {**body, "instructions": f"{instructions}\n\n{injection}" if instructions else injection}
        items = list(items)
        item = items[index]
        items[index] = {**item, "content": _prefixed(item.get("content"), injection, "input_text")}
        return {**body, "input": items}

    def reply(self, data: Any) -> dict[str, Any] | None:
        if not isinstance(data, dict) or not isinstance(data.get("output"), list):
            return None
        replies = [item for item in _responses_items(data["output"]) if item["role"] == "assistant"]
        return replies[-1] if replies else None

    def stream_reply(self, events: list[dict[str, Any]]) -> dict[str, Any] | None:
        done = next((event for event in reversed(events) if event.get("type") == "response.completed"), None)
        return self.reply(done.get("response")) if done else None

    def response_id(self, data: Any) -> str:
        if isinstance(data, list):
            done = next((event for event in reversed(data) if event.get("type") == "response.completed"), {})
            data = done.get("response")
        return str(data.get("id") or "") if isinstance(data, dict) else ""

    def previous(self, body: dict[str, Any]) -> str:
        return str(body.get("previous_response_id") or "")


def protocol_for(method: str, path: str) -> ChatCompletions | AnthropicMessages | OpenAIResponses | None:
    if method != "POST":
        return None
    segments = path.rstrip("/").split("/")
    if segments[-2:] == ["chat", "completions"]:
        return ChatCompletions()
    if segments[-1] == "messages":
        return AnthropicMessages()
    if segments[-1] == "responses":
        return OpenAIResponses()
    return None


def events(lines: list[str]) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    for line in lines:
        payload = line[5:].strip() if line.startswith("data:") else ""
        if payload and payload != "[DONE]":
            with contextlib.suppress(ValueError):
                value = json.loads(payload)
                if isinstance(value, dict):
                    parsed.append(value)
    return parsed
