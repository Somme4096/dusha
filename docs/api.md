# API and integration

Base URL: `http://127.0.0.1:8765`. The state API lives under `/state/v1/*` and the optional OpenAI proxy under `/v1/*`.

FastAPI serves the field reference at `/docs` and `/openapi.json`. This page covers the integration flow.

## Endpoints

| Method | Path | Purpose | Statuses |
| --- | --- | --- | --- |
| GET | `/health` | Process, database, and index status, plus the `companion` name | 200 |
| POST | `/state/v1/messages` | Store one message, update affect for a new user message | 200, 401, 422, 503 |
| GET | `/state/v1/messages/{message_id}` | Read one stored message | 200, 401, 404, 503 |
| POST | `/state/v1/memory/search` | Search the archive with adjacent context | 200, 401, 503 |
| GET | `/state/v1/memory/{message_id}` | Read a message plus neighbors | 200, 401, 404, 503 |
| GET | `/state/v1/memory/index` | Report index status | 200, 401, 503 |
| POST | `/state/v1/context` | Build the injection | 200, 401, 422, 503 |
| GET | `/state/v1/affect` | Read affect state and advance decay | 200, 401 |
| POST | `/state/v1/evergreen/facts` | Remember a fact | 200, 401, 409, 422, 503 |
| GET | `/state/v1/evergreen/facts` | List current facts | 200, 401, 503 |
| GET | `/state/v1/evergreen/facts/{fact_id}/history` | List every revision | 200, 401, 404, 503 |
| POST | `/state/v1/evergreen/facts/{fact_id}/revisions` | Revise with `expected_revision` | 200, 401, 404, 409, 422, 503 |
| POST | `/state/v1/evergreen/facts/{fact_id}/forget` | Forget with `expected_revision` | 200, 401, 404, 409, 422, 503 |
| POST | `/state/v1/memo/add` | Add one memo | 200, 401, 422, 503 |
| GET | `/state/v1/memo/list` | List active or archived memos | 200, 401, 422, 503 |
| POST | `/state/v1/memo/{note_id}/done` | Archive a memo with a reason | 200, 401, 404, 422, 503 |
| POST | `/state/v1/proactive/evaluate` | Run one evaluation pass | 200, 401 |
| GET | `/state/v1/proactive/events` | Poll and lease pending events | 200, 401 |
| POST | `/state/v1/proactive/events/{event_id}/ack` | Acknowledge a leased event | 200, 401, 404, 409 |
| GET | `/v1/models` | Proxy a model list | 401, 503, upstream |
| POST | `/v1/chat/completions` | Proxy chat completions and store the transcript | 401, 422, 503, upstream |
| GET | `/v1/to/{upstream}/models` | Proxy a model list from the upstream in the path | 401, 422, upstream |
| POST | `/v1/to/{upstream}/chat/completions` | Proxy chat completions to the upstream in the path | 401, 422, upstream |

Statuses that depend on config:

- `401` applies with [auth](guide.md#auth) on. Send the token in `X-Companion-Token`.
- `503` on the `/state/v1/*` routes means `storage.enabled` is `false`. Affect, proactive, and health routes stay up.
- `/v1/*` returns `404` when `api_openai.enabled` is `false` and `503` when `upstream.base_url` is empty.
- `/v1/to/*` does not need `upstream.base_url`. It returns `422` for an address that is not a valid host and path.

## Client flow

One exchange takes four steps. The [OpenAI proxy](#openai-proxy) runs all four for you.

### 1. Ingest the user message

`POST /state/v1/messages` stores one message. `content` accepts a string or OpenAI-style structured content.

```json
{
  "harness": "my-harness",
  "conversation_id": "stable-conversation-id",
  "role": "user",
  "content": "Remember the amber window.",
  "route": "opaque-return-address",
  "external_id": "msg-42",
  "occurred_at": null
}
```

The response carries the stored `id`, a `duplicate` flag, `conversation_id`, and `affect`.

- A new user message cancels unsent proactive events and updates [affect](#affect).
- `affect` holds `emotion`, `increment`, `decision_id`, and `state` when the message moved an emotion. Otherwise it is `null`.
- A repeated `external_id` in the same harness and conversation stores nothing and returns `duplicate: true` with the original `id`.
- Unknown request fields return `422`.

### 2. Build context

`POST /state/v1/context` returns the injection string plus the same data as fields. It stores nothing and calls no provider.

```json
{
  "harness": "my-harness",
  "conversation_id": "stable-conversation-id",
  "query": "Remember the amber window.",
  "exclude_message_ids": [1],
  "include_recent": false
}
```

- `include_recent` defaults to `true`. Pass `false` when your provider conversation holds the recent history.
- `exclude_message_ids` removes messages you send yourself.

The response carries `injection`, `affect`, `evergreen_facts`, `records`, `search_hits`, and `context`. Prepend `injection` to your system prompt, or read `context` for the same data as fields: `version`, `identity`, `instructions`, `memory`, and `emotion`.

`injection` holds up to five parts in this order:

1. The identity text
2. `<evergreen_facts>`, a JSON list of facts
3. `<memory_context>`, the memory plugin block
4. `<memo_notes>`, a JSON list of active memos
5. `<companion_state>`, a JSON object with the instructions, emotion values, and session records

You can rename the delimiters in [prompts.json](configuration.md#promptsjson). The serializer escapes `&`, `<`, and `>` in stored text.

The injection fits `memory.injection_max_chars`:

- The identity and the companion state block never shrink. If the two exceed the budget, the endpoint returns `422`.
- The remaining space fills in order: evergreen facts, memos, the memory plugin block, then session records.
- Each list stops at the first item that does not fit.

### 3. Call the provider

Send the provider request yourself. When you use the proxy, add the routing headers:

```text
X-Conversation-Id: stable-conversation-id
X-Harness: my-harness
X-Companion-Route: opaque-return-address
```

`X-Companion-Route` is the return address for proactive messages.

### 4. Archive the assistant reply

Call `POST /state/v1/messages` with `role: "assistant"`. Assistant and tool messages leave affect untouched.

## Affect

`GET /state/v1/affect` returns `base` and `mood` for each dimension, timestamps, and `unanswered_proactive`. The read advances decay and silence drift to the current time and saves the result.

A new user message moves affect in up to two steps:

1. The [decision plugin](configuration.md#plugins) picks one dimension or abstains. With [embedding](configuration.md#embedding) configured, semantic appraisal picks when the plugin is absent, abstains, or fails. The engine raises the picked dimension by its increment and reports it in the ingest `affect` field.
2. A memory plugin's `match_phrase` operation returns per-dimension deltas for the message text. A delta can be negative, and one message can move several dimensions. The ingest `affect` field does not report these.

## Evergreen facts

A fact keeps every revision. Revise and forget take `expected_revision`, and a stale value returns `409` with the current revision.

1. `POST /state/v1/evergreen/facts` remembers a fact under a stable `key`. A duplicate active key returns `409`.
2. `GET /state/v1/evergreen/facts` lists current facts by priority. Query `include_inactive`, `due_only`, and `limit`.
3. `GET /state/v1/evergreen/facts/{fact_id}/history` lists every revision.
4. `POST /state/v1/evergreen/facts/{fact_id}/revisions` writes a new revision.
5. `POST /state/v1/evergreen/facts/{fact_id}/forget` marks the fact forgotten and keeps its history.

Remember request:

```json
{
  "key": "user.preference.editor",
  "text": "The user prefers Helix.",
  "priority": 50,
  "source_message_id": null,
  "reason": "The user stated this preference.",
  "review_after": null,
  "expires_at": null
}
```

The fact object carries `fact_id`, `revision`, `state`, `effective_state`, `priority`, `review_due`, and timestamps. `state` is `active` or `forgotten`, and `effective_state` reads `expired` once `expires_at` passes. The gateway injects active, unexpired facts. It extracts no facts on its own.

## Memos

Memos are short notes the agent leaves for itself, such as a loose end to raise in a later proactive message. They belong to the whole companion, so the routes take no harness or conversation. Each context build injects the active memos, oldest first.

`POST /state/v1/memo/add` stores one memo. A blank `text`, or one longer than `memo.text_max_chars`, returns `422`.

```json
{
  "text": "Ask how the interview went."
}
```

Response:

```json
{
  "memo": {
    "id": 3,
    "text": "Ask how the interview went.",
    "status": "active",
    "reason": "",
    "created_at": "2026-01-01T00:00:00+00:00",
    "updated_at": "2026-01-01T00:00:00+00:00",
    "archived_at": null
  }
}
```

`GET /state/v1/memo/list?status=active&limit=20` returns `{"memos": [...]}`, oldest first. `status` accepts `active` or `archived`. `limit` runs from 1 to 500.

`POST /state/v1/memo/{note_id}/done` archives one memo and returns it with `status` set to `archived`.

```json
{
  "reason": "Asked, the interview went well."
}
```

An unknown `note_id` returns `404`. A blank `reason`, one longer than `memo.reason_max_chars`, or a memo you archived before returns `422`.

## Memory index status

`GET /state/v1/memory/index` returns the memory plugin's `status` object when a plugin runs, so its fields depend on the plugin. Without a plugin the gateway reports its built-in index, with `mode` set to `lexical` and a `messages` count. `/health` embeds the same object under `memory_index`.

## Proactive delivery

1. `POST /state/v1/proactive/evaluate` runs one pass. It returns the stored event, or `{"event": null}` when nothing is due. The service also runs this pass on its own schedule.
2. `GET /state/v1/proactive/events?consumer=my-harness&harness=my-harness&limit=1` leases pending events to that consumer.
3. Send the message to the event's `target`, then acknowledge it.

A lease that expires returns the event to pending, so delivery is at-least-once.

Each event holds:

- `id`, `created_at`, `evaluated_at`, and `lease_until` on a polled event
- `target`: `harness`, `conversation_id`, `route`
- `reason`, copied from the matching trigger in [emotions.json](configuration.md#emotionsjson)
- `generation_instruction`, `generation_base`, `generation_variant`, `generation_variant_index`
- `ruling_feeling`: `dimension`, `value`, `neutral`, `deviation`
- `ladder_stage`, `unanswered_proactive`
- `silence_minutes`, `silence_text`, `last_user_message_at`
- `context`, the response of a [context build](#2-build-context) for that conversation

Acknowledge with `POST /state/v1/proactive/events/{event_id}/ack`:

```json
{
  "consumer": "my-harness",
  "outcome": "sent",
  "text": "the exact sent message",
  "external_id": "",
  "error": ""
}
```

| `outcome` | Effect |
| --- | --- |
| `sent` | Stores the text as an assistant message and counts the send. |
| `failed` | Returns the event to pending after `proactive.retry_delay_minutes`. |
| `release` | Returns the event to pending at once. |

A consumer that does not hold the lease gets `409`. A user reply cancels unsent events and resets the unanswered count.

## AstrBot plugin

The plugin in `integrations/astrbot_dusha` connects AstrBot to the gateway. It needs AstrBot 4.8 or later and supports Discord.

### Install

Start the gateway, then copy the plugin into AstrBot's plugin directory:

```sh
cp -a integrations/astrbot_dusha \
  /path/to/AstrBot/data/plugins/astrbot_dusha
```

Restart AstrBot or reload the plugin from its manager. Keep AstrBot's chat provider pointed at your model provider, not at the gateway.

### Settings

| Field | Default | Meaning |
| --- | --- | --- |
| `gateway_url` | `http://127.0.0.1:8765` | |
| `platform_id` | `""` | AstrBot platform ID allowed to use the gateway. Empty disables the plugin. Run `/sid` through your Discord bot and copy its `Bot ID`. |
| `api_token` | `""` | The gateway's API token, when auth is on. |
| `harness` | `astrbot` | Harness name sent to the gateway. A rename starts each chat with empty recent history. |
| `request_timeout_seconds` | `60` | Minimum 1. Raise it when you raise a plugin `timeout_seconds` on the gateway. |
| `poll_interval_seconds` | `30` | Seconds between proactive polls. Minimum 5. |
| `proactive_enabled` | `true` | |
| `proactive_tool_keywords` | `["donsetch", "fetch"]` | AstrBot tools whose names contain a keyword join proactive generation. |

### Tools

| Tool | Arguments | Calls |
| --- | --- | --- |
| `search_conversation_memory` | `query`, `limit` | `POST /state/v1/memory/search` |
| `get_conversation_record` | `memory_id`, `context_messages` | `GET /state/v1/memory/{id}` |
| `remember_evergreen_fact` | `key`, `text`, `priority`, `review_after`, `expires_at`, `reason` | `POST /state/v1/evergreen/facts` |
| `revise_evergreen_fact` | `fact_id`, `expected_revision`, `text`, `priority`, `review_after`, `expires_at`, `reason` | `POST /state/v1/evergreen/facts/{id}/revisions` |
| `forget_evergreen_fact` | `fact_id`, `expected_revision`, `reason` | `POST /state/v1/evergreen/facts/{id}/forget` |
| `review_evergreen_facts` | `due_only`, `include_inactive`, `limit` | `GET /state/v1/evergreen/facts` |
| `yumecho_add` | `text` | `POST /state/v1/memo/add` |
| `yumecho_list` | `status`, `limit` | `GET /state/v1/memo/list` |
| `yumecho_done` | `note_id`, `reason` | `POST /state/v1/memo/{id}/done` |

Tools return JSON with `ok: true`, or `ok: false` plus `error` and, for HTTP failures, `status`.

The OpenCode plugin in `integrations/opencode_dusha` adds an affect status tool and prefixes its tools with the companion name from `/health`, for example `dusha_affect_status`.

### Behavior

On each turn the plugin:

1. Stores the user message.
2. Requests context and adds the injection to AstrBot's request. The active persona stays as AstrBot built it.
3. Archives the assistant reply.

If the store call fails, the plugin logs `Message archive failed` and still requests context. If the context call fails, it logs `Context unavailable` and AstrBot sends the request without an injection.

With `proactive_enabled` on, the plugin:

1. Polls one event at a time.
2. Generates a message with the route's persona, the event's injection, and the tools above.
3. Tries each configured provider until one answers.
4. Sends the text, writes it into AstrBot's history after a `[proactive <reason>]` marker, and acknowledges `sent`.

A wrong platform, a missing persona, an empty reply, or a failed send ends in a `failed` acknowledgement, and the gateway offers the event again later.

### Troubleshooting

Check the AstrBot log for lines that begin with `[dusha]`, and confirm the gateway answers:

```sh
curl http://127.0.0.1:8765/health
```

## OpenAI proxy

`GET /v1/models` and `POST /v1/chat/completions` forward OpenAI-shaped requests to `upstream.base_url`. The proxy:

- Inserts the context as a system message after any leading system messages.
- Stores the transcript's user, assistant, and tool turns before the call, and the reply after a successful call.
- Keeps unknown request fields.
- Forwards responses byte for byte, including streams and upstream errors.

Send the routing headers from [step 3](#3-call-the-provider). Without `X-Conversation-Id`, conversations collapse into `default`. AstrBot cannot set per-session headers, so use the plugin there.

The proxy sends the provider key from `upstream.api_key_env`. See [configuration.md](configuration.md#server). OAuth flows and Responses-style endpoints are out of scope.

### Passthrough

Passthrough lets the client pick the provider per request. Put the provider address in the path and use the result as the client's base URL:

```text
http://127.0.0.1:8765/v1/to/api.example.com/v1
http://127.0.0.1:8765/v1/to/http/127.0.0.1:11434/v1
```

- The address is the provider base URL without the scheme. It defaults to `https`. A leading `http/` or `https/` segment sets the scheme.
- Any host is accepted. Anyone who can call the service can relay through it, so turn on [auth](guide.md#auth) before you expose the port.
- The proxy forwards the caller's headers and query string, including `Authorization`. It drops the routing headers, `X-Companion-Token`, and hop-by-hop headers.
- The proxy never sends the `upstream.api_key_env` key on these routes. The client logs in and refreshes its own tokens.
- Context injection, transcript storage, and the `model` field work the same as on the fixed routes.

## Versions

- The OpenAPI `version` follows the installed package version.
- `/state/v1` is the state API major version.
- `context.version` is the injection schema version, now `1`.
- An evergreen fact `revision` counts that fact's stored changes.

## See also

- [configuration.md](configuration.md): every config key.
- [guide.md](guide.md): service setup, auth, backup, troubleshooting.
