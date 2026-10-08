# DeepSeek harness Dusha adapter

Python adapter that connects a DeepSeek harness to a running Dusha gateway.
It archives user and assistant turns, injects companion state into prompts,
and exposes seven OpenCode-parity tools.

Proactive delivery is intentionally absent. This adapter has no poll loop, no
event acknowledgement, and no proactive settings. See the astrbot integration
if you need proactive follow-up.

## Install

1. Start the Dusha gateway first, for example `dusha serve`, then confirm it
   answers `curl http://127.0.0.1:8765/health`.
2. Copy this directory into your harness runtime so `deepseek_harness_dusha`
   is importable.
3. Install the one runtime dependency with `pip install httpx`.

## Settings

`DeepSeekHarnessConfig` is a dataclass with these fields.

| Field | Default | Meaning |
| --- | --- | --- |
| `gateway_url` | `http://127.0.0.1:8765` | Base URL of the Dusha gateway. |
| `api_token` | `""` | Value sent as `X-Companion-Token` when non-empty. |
| `harness` | `deepseek-harness` | Harness name recorded on every message. |
| `route` | `deepseek-harness` | Routing label recorded on every message. |
| `request_timeout_seconds` | `60` | HTTP timeout, clamped to at least 1 second. |
| `auto_inject` | `True` | When false, `handle_turn` returns no injection. |

## Turn flow

```python
from deepseek_harness_dusha import DeepSeekHarnessAdapter, DeepSeekHarnessConfig

adapter = DeepSeekHarnessAdapter(DeepSeekHarnessConfig(api_token="secret"))
injection = await adapter.handle_turn("hello there", "conversation-1")
# Append injection to the system prompt when it is non-empty.

await adapter.archive_reply("conversation-1", "hello back")
await adapter.aclose()
```

1. `handle_turn` ingests the user message best effort, then builds context.
   It returns the injection string, or an empty string when disabled, when the
   prompt is empty, when the prompt already contains `<companion_state>`, or
   when the gateway is unreachable.
2. Append the returned injection to the system prompt you send to the model.
3. `archive_reply` stores the assistant reply best effort. It never raises.

Messages are deduplicated through a stable FNV-1a `external_id` derived from
the conversation. Pass the same `message_id` twice and the gateway reports the
second ingest as a duplicate.

## Tools

All seven tools return a JSON string. Success is `{"ok": true, ...}` and failure
is `{"ok": false, "error": "...", "status": ...}`.

| Tool | Gateway call |
| --- | --- |
| `memory_search(query, limit, context_messages)` | `POST /state/v1/memory/search` |
| `context_build(query, conversation_id, include_recent)` | `POST /state/v1/context` |
| `affect_status()` | `GET /state/v1/affect` |
| `evergreen_remember(key, text, priority, reason, review_after, expires_at)` | `POST /state/v1/evergreen/facts` |
| `evergreen_list(include_inactive, due_only, limit)` | `GET /state/v1/evergreen/facts` |
| `evergreen_revise(fact_id, expected_revision, text, priority, reason, review_after, expires_at)` | `POST /state/v1/evergreen/facts/{fact_id}/revisions` |
| `evergreen_forget(fact_id, expected_revision, reason)` | `POST /state/v1/evergreen/facts/{fact_id}/forget` |

Time fields accept an ISO 8601 string. The strings `clear`, `none`, and `null`
clear the field, and an omitted field leaves it unchanged. That matches the
revise and forget semantics of the gateway.

## Harness naming

Keep the `harness` value stable. The gateway keys a conversation by harness and
conversation id, so renaming the harness starts a new conversation and hides
the old history from search and context.

## Troubleshooting

- Run `curl http://127.0.0.1:8765/health` to confirm the gateway is reachable.
- A `401` result means the token is wrong. Set `api_token` to the same value
  the gateway reads from its `api_token_env` environment variable.
- A `422` result means the request body is invalid. Check required tool
  arguments before calling.
