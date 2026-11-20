#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_URL = "http://127.0.0.1:8765"
DEFAULT_HARNESS = "agy"
DEFAULT_TIMEOUT = 5.0
COMPANION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
USER_REQUEST = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", re.DOTALL)
SYSTEM_INSTRUCTIONS = re.compile(r"<system_instructions>.*?</system_instructions>", re.DOTALL)


@dataclass(frozen=True, slots=True)
class Settings:
    url: str
    token: str
    harness: str
    plugins: str
    route: str
    timeout: float


def _warn(message: str) -> None:
    print(f"[dusha] {message}", file=sys.stderr)


def _expand(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value)))


def _config_root(env: dict[str, str]) -> Path:
    base = env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "dusha"


def _discover_config(env: dict[str, str]) -> Path | None:
    if explicit := env.get("DUSHA_CONFIG", "").strip():
        return _expand(explicit)
    if home := env.get("DUSHA_HOME", "").strip():
        return _expand(home) / "config.json"
    root = _config_root(env)
    if companion := env.get("DUSHA_COMPANION", "").strip():
        if not COMPANION_NAME.fullmatch(companion):
            raise ValueError(f"invalid DUSHA_COMPANION: {companion!r}")
        return root / companion / "config.json"
    if not root.is_dir():
        return None
    found = sorted(item / "config.json" for item in root.iterdir() if (item / "config.json").is_file())
    if len(found) > 1:
        names = ", ".join(item.parent.name for item in found)
        raise ValueError(f"several companions exist ({names}), set DUSHA_COMPANION, DUSHA_HOME, or DUSHA_URL")
    return found[0] if found else None


def _connection_from_config(path: Path) -> tuple[str, str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"expected JSON object in {path}")
    host = raw.get("host", "127.0.0.1")
    host = host if isinstance(host, str) else "127.0.0.1"
    host = "127.0.0.1" if host in {"", "0.0.0.0", "::"} else host
    port = raw.get("port", 8765)
    port = port if isinstance(port, int) and not isinstance(port, bool) else 8765
    local = f"http://{f'[{host}]' if ':' in host else host}:{port}"
    proxy = raw.get("proxy") if isinstance(raw.get("proxy"), dict) else {}
    server = proxy.get("server") if isinstance(proxy.get("server"), dict) else {}
    saved_url = server.get("url") if isinstance(server.get("url"), str) else ""
    saved_token = server.get("token") if isinstance(server.get("token"), str) else ""
    url = saved_url.strip().rstrip("/") or local
    token = saved_token
    if url == local and not token and isinstance(raw.get("api_token"), str):
        token = raw["api_token"]
    return url, token


def _parse_timeout(raw: str) -> float:
    if not raw.strip():
        return DEFAULT_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT
    return value if value > 0 else DEFAULT_TIMEOUT


def _resolve_settings(env: dict[str, str]) -> Settings:
    explicit_url = env.get("DUSHA_URL", "").strip().rstrip("/")
    if explicit_url:
        url = explicit_url
        token = env.get("DUSHA_TOKEN", "")
    else:
        url, token = DEFAULT_URL, ""
        config_path = _discover_config(env)
        if config_path is not None and config_path.is_file():
            url, token = _connection_from_config(config_path)
        if explicit_token := env.get("DUSHA_TOKEN", "").strip():
            token = explicit_token
    harness = env.get("DUSHA_HARNESS", "").strip() or DEFAULT_HARNESS
    plugins = env.get("DUSHA_PLUGINS", "").strip()
    route = env.get("DUSHA_ROUTE", "").strip()
    timeout = _parse_timeout(env.get("DUSHA_TIMEOUT", ""))
    return Settings(url=url, token=token, harness=harness, plugins=plugins, route=route, timeout=timeout)


def _post_json(
    settings: Settings,
    path: str,
    payload: dict[str, Any],
    *,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if settings.token:
        headers["X-Companion-Token"] = settings.token
    if extra_headers:
        headers.update(extra_headers)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(f"{settings.url}{path}", data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=settings.timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"unexpected response from {path}")
    return data


def _parse_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            entries.append(item)
    return entries


def _resolve_full_content(transcript: Path, index: int, entry: dict[str, Any]) -> str:
    content = entry.get("content")
    text = content if isinstance(content, str) else ""
    truncated = entry.get("truncated_fields")
    if not isinstance(truncated, list) or "content" not in truncated:
        return text
    full_path = transcript.with_name("transcript_full.jsonl")
    if not full_path.is_file():
        return text
    step_index = entry.get("step_index")
    full_entries = _parse_jsonl(full_path)
    if 0 <= index < len(full_entries):
        candidate = full_entries[index]
        if step_index is None or candidate.get("step_index") == step_index:
            full_content = candidate.get("content")
            if isinstance(full_content, str):
                return full_content
    if step_index is not None:
        for candidate in full_entries:
            if candidate.get("step_index") == step_index and isinstance(candidate.get("content"), str):
                return candidate["content"]
    return text


def _extract_user_text(raw: str) -> str:
    match = USER_REQUEST.search(raw)
    text = match.group(1) if match else raw
    text = SYSTEM_INSTRUCTIONS.sub("", text)
    return text.strip()


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _latest_user_step(transcript_path: str) -> tuple[int, dict[str, Any], str] | None:
    if not transcript_path:
        return None
    path = Path(transcript_path)
    entries = _parse_jsonl(path)
    for index in range(len(entries) - 1, -1, -1):
        entry = entries[index]
        if entry.get("type") == "USER_INPUT":
            return index, entry, _resolve_full_content(path, index, entry)
    return None


def _handle_pre_invocation(event: dict[str, Any], settings: Settings) -> dict[str, Any]:
    conversation_id = str(event.get("conversationId") or "").strip() or "default"
    user_text = str(event.get("lastUserInput") or "").strip()
    transcript_path = str(event.get("transcriptPath") or "").strip()
    external_id = ""
    occurred_at: str | None = None

    latest = _latest_user_step(transcript_path)
    if latest is not None:
        _, entry, raw_content = latest
        extracted = _extract_user_text(raw_content)
        if not user_text:
            user_text = extracted
        matches = bool(user_text) and (user_text in raw_content or user_text == extracted)
        if matches and _is_int(entry.get("step_index")):
            external_id = f"agy:user:{entry['step_index']}"
            created_at = entry.get("created_at")
            if isinstance(created_at, str) and created_at.strip():
                occurred_at = created_at.strip()

    if not external_id and _is_int(event.get("initialNumSteps")):
        external_id = f"agy:user:steps-{event['initialNumSteps']}"

    if not user_text:
        return {}

    ingest_body: dict[str, Any] = {
        "harness": settings.harness,
        "conversation_id": conversation_id,
        "role": "user",
        "content": user_text,
        "route": settings.route,
        "external_id": external_id,
        "occurred_at": occurred_at,
    }
    ingested = _post_json(settings, "/state/v1/messages", ingest_body)
    if ingested.get("duplicate"):
        return {}

    message_id = ingested.get("id")
    context_body: dict[str, Any] = {
        "query": user_text,
        "harness": settings.harness,
        "conversation_id": conversation_id,
        "exclude_message_ids": [message_id] if _is_int(message_id) else [],
        "include_recent": False,
    }
    headers = {"X-Companion-Plugins": settings.plugins} if settings.plugins else None
    context = _post_json(settings, "/state/v1/context", context_body, extra_headers=headers)
    injection = str(context.get("injection") or "").strip()
    if not injection:
        return {}
    return {"injectSteps": [{"systemMessage": {"systemMessage": injection}}]}


def _handle_stop(event: dict[str, Any], settings: Settings) -> dict[str, Any]:
    conversation_id = str(event.get("conversationId") or "").strip() or "default"
    transcript_path = str(event.get("transcriptPath") or "").strip()
    if not transcript_path:
        return {}
    path = Path(transcript_path)
    entries = _parse_jsonl(path)
    if not entries:
        return {}

    start = 0
    for index in range(len(entries) - 1, -1, -1):
        if entries[index].get("type") == "USER_INPUT":
            start = index + 1
            break

    for index in range(start, len(entries)):
        entry = entries[index]
        if entry.get("type") != "PLANNER_RESPONSE":
            continue
        status = entry.get("status")
        if status not in (None, "", "DONE"):
            continue
        content = _resolve_full_content(path, index, entry).strip()
        if not content:
            continue
        step_index = entry.get("step_index")
        external_id = f"agy:assistant:{step_index}" if _is_int(step_index) else f"agy:assistant:line-{index}"
        created_at = entry.get("created_at")
        occurred_at = created_at.strip() if isinstance(created_at, str) and created_at.strip() else None
        payload: dict[str, Any] = {
            "harness": settings.harness,
            "conversation_id": conversation_id,
            "role": "assistant",
            "content": content,
            "route": settings.route,
            "external_id": external_id,
            "occurred_at": occurred_at,
        }
        _post_json(settings, "/state/v1/messages", payload)
    return {}


def run(event: dict[str, Any], env: dict[str, str] | None = None) -> dict[str, Any]:
    hook_event = str(event.get("hookEventName") or "")
    if hook_event not in {"PreInvocation", "Stop"}:
        return {}
    settings = _resolve_settings(dict(os.environ) if env is None else env)
    if hook_event == "PreInvocation":
        return _handle_pre_invocation(event, settings)
    return _handle_stop(event, settings)


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        print("{}")
        return 0
    try:
        event = json.loads(raw)
        output = run(event) if isinstance(event, dict) else {}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        _warn(f"hook skipped: {error}")
        output = {}
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
