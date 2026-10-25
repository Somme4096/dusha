# API and integration

Base URL: `http://127.0.0.1:8765`. The server serves the state API under `/state/v1/*`, the [MCP](#mcp) tools at `/mcp`, and the [commands](#commands) under `/cli/v1/*`. Model requests go through the [proxy](#proxy), which runs next to your harness.

FastAPI serves the field reference at `/docs` and `/openapi.json`. This page covers the integration flow.

## Endpoints

| Method | Path | Purpose | Statuses |
| --- | --- | --- | --- |
| GET | `/health` | Process and database status, the `companion` name, and the names of the active plugins | 200 |
| POST | `/state/v1/messages` | Store one message, update affect for a new user message | 200, 401, 422 |
| POST | `/state/v1/transcript` | Store the new messages of a Chat Completions transcript, optionally `after` a stored message id, see [conversation matching](#conversation-matching) | 200, 401, 422 |
| GET | `/state/v1/messages/{message_id}` | Read one stored message | 200, 401, 404 |
| POST | `/state/v1/memory/search` | Search the archive with adjacent context | 200, 401, 503 |
| GET | `/state/v1/memory/{message_id}` | Read a message plus neighbors | 200, 401, 404, 503 |
| GET | `/state/v1/plugins` | Report each plugin's state | 200, 401 |
| POST | `/state/v1/context` | Build the injection | 200, 401, 422 |
| GET | `/state/v1/affect` | Read affect state and advance decay | 200, 401, 503 |
| POST | `/state/v1/evergreen/facts` | Remember a fact | 200, 401, 409, 422, 503 |
| GET | `/state/v1/evergreen/facts` | List current facts | 200, 401, 503 |
| GET | `/state/v1/evergreen/facts/{fact_id}/history` | List every revision | 200, 401, 404, 503 |
| POST | `/state/v1/evergreen/facts/{fact_id}/revisions` | Revise with `expected_revision` | 200, 401, 404, 409, 422, 503 |
| POST | `/state/v1/evergreen/facts/{fact_id}/forget` | Forget with `expected_revision` | 200, 401, 404, 409, 422, 503 |
| POST | `/state/v1/memo/add` | Add one memo | 200, 401, 422, 503 |
| GET | `/state/v1/memo/list` | List active or archived memos | 200, 401, 422, 503 |
| POST | `/state/v1/memo/{note_id}/done` | Archive a memo with a reason | 200, 401, 404, 422, 503 |
| POST | `/state/v1/proactive/evaluate` | Run one evaluation pass | 200, 401, 503 |
| GET | `/state/v1/proactive/events` | Poll and lease pending events | 200, 401, 503 |
| POST | `/state/v1/proactive/events/{event_id}/ack` | Acknowledge a leased event | 200, 401, 404, 409, 503 |
| POST | `/mcp` | MCP tools over streamable HTTP | 200, 202, 400, 401, 403 |
| GET | `/cli/v1/commands` | The command list `dusha` builds its commands from | 200, 401 |
| POST | `/cli/v1/run/{command}` | Run one command | 200, 400, 401, 404, 422 |

Statuses that depend on config:

- `401` applies with [auth](guide.md#auth) on. Send the token in `X-Companion-Token`.
- `503` on a `/state/v1/*` route means its [plugin](configuration.md#plugins) is disabled or inactive. The `detail` names the plugin.

## Client flow

One exchange takes four steps. The [proxy](#proxy) runs all four for you.

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

The response carries the stored `id`, a `duplicate` flag, and `conversation_id`, plus one key per plugin
that returned a result for the message. The affect plugin returns `affect`.

- A new user message cancels unsent proactive events and updates [affect](#affect).
- `affect` holds `emotion`, `increment`, `decision_id`, and `state` when the message moved an emotion. Otherwise the key is absent.
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

The response carries `injection`, `records`, `search_hits`, and `context`. Prepend `injection` to your system prompt, or read `context` for the same data as fields: `version`, `instructions`, `session`, and `blocks`.

`injection` holds one part per [plugin](configuration.md#plugins) in `plugins` order, then the state block. With the packaged order and external plugins listed after the built-in plugins:

1. The identity text
2. `<evergreen_facts>`, a JSON list of facts
3. `<memo_notes>`, a JSON list of active memos
4. `<affect>`, the affect instruction and the Affect line as plain text
5. The blocks returned by external plugins, each with the delimiters that plugin renders
6. `<companion_state>`, a JSON object with the instructions and the session records from `memory`

`context.blocks` holds each part as data, keyed by plugin name. `identity` carries `text`, and `memory` carries the `search_hits`. A plugin that is off contributes no part and no key, and so does a part with nothing to say.

You can rename the delimiters in [prompts.json](configuration.md#promptsjson). The serializer escapes `&`, `<`, and `>` in stored text.

The injection has no overall cap. Each part has its own limit in [config.json](configuration.md#overrides-in-configjson).

### 3. Call the provider

Send the provider request yourself. Pass the same `route` on ingest as the return address for proactive messages.

### 4. Archive the assistant reply

Call `POST /state/v1/messages` with `role: "assistant"`. Assistant and tool messages leave affect untouched.

## Affect

`GET /state/v1/affect` returns `base` and `mood` for each dimension, timestamps, and `unanswered_proactive`. The read advances decay and silence drift to the current time and saves the result.

A new user message moves affect in up to two steps:

1. Each [plugin](configuration.md#plugins) that listens for `decide` proposes an emotion, deltas, or nothing. The first emotion wins. With [embedding](configuration.md#embedding) configured, semantic appraisal picks when no external plugin proposes one. The engine raises the picked dimension by its increment and reports it in the ingest `affect` field.
2. The merged `deltas` apply next. A delta can be negative, and one message can move several dimensions. The ingest `affect` field does not report these.

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

`POST /state/v1/memo/add` stores one memo. A blank `text`, or one longer than `plugins.memo.text_max_chars`, returns `422`.

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

An unknown `note_id` returns `404`. A blank `reason`, one longer than `plugins.memo.reason_max_chars`, or a memo you archived before returns `422`.

## Plugin status

`GET /state/v1/plugins` returns one object per plugin, keyed by name, in `plugins` order:

- `state`: `active`, `disabled`, `inactive` when a service it injects is missing, or `failed` when a plugin reported a runtime error via `ctx.error`
- `error`: the reason for `inactive` or `failed`, otherwise empty
- `inject` and `provides`: the services it uses and offers
- `events`: the events it listens for

`/health` lists the active plugins under `plugins`.

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
| `failed` | Returns the event to pending after `plugins.proactive.retry_delay_minutes`. |
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
| `platform_id` | `""` | AstrBot platform ID allowed to use the gateway. Run `/sid` through your Discord bot and copy its `Bot ID`. |
| `config_profile` | `""` | AstrBot config profile ID or name allowed to use the gateway. With `platform_id` also set, a chat must match both. With both empty, these settings serve no chat. |
| `api_token` | `""` | The gateway's API token, when auth is on. |
| `harness` | `astrbot` | Harness name sent to the gateway. A rename starts each chat with empty recent history. |
| `request_timeout_seconds` | `60` | Minimum 1. Message storage runs plugin hooks in the gateway process. |
| `poll_interval_seconds` | `30` | Seconds between proactive polls. Minimum 5. |
| `proactive_enabled` | `true` | |
| `proactive_tool_keywords` | `["donsetch", "fetch"]` | AstrBot tools whose names contain a keyword join proactive generation. |
| `proactive_request_hooks` | `["astrbot_plugin_opencode_go_session"]` | AstrBot plugins whose LLM request and response hooks run for proactive generation, such as a session header injector. |
| `bindings` | `[]` | Extra companions. Each entry takes every field above. |

### Several companions

Run one gateway per companion, each with its own home and port. In AstrBot, route each chat to a config profile, then add one `bindings` entry per companion with that profile in `config_profile` and that gateway in `gateway_url`. The first entry that matches a chat serves it, and the top-level settings serve chats no entry matches. Each companion polls its own gateway for proactive events and acknowledges an event as failed when AstrBot now routes its chat to another companion. Give each config profile its own persona, since proactive messages use the persona of the target chat.

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

### Behavior

On each turn the plugin:

1. Stores the user message.
2. Requests context and renders it as plain text from `context` and `records`, one line per fact, memo, or record. The active persona stays as AstrBot built it.
3. Archives the assistant reply.

The rendered text lands in two parts of the user message. Facts, memos, the affect block, external plugin blocks, and the recalled records go into a part marked provider-only, so AstrBot sends it with the request and leaves it out of the saved history. On the first turn of an AstrBot conversation the plugin also asks for recent records and saves them once as a `<previous_session>` part, the bridge from the previous session. Later turns skip recent records because AstrBot's own history holds them. AstrBot releases without provider-only parts receive the text in the system prompt instead.

If the store call fails, the plugin logs `Message archive failed` and still requests context. If the context call fails, it logs `Context unavailable` and AstrBot sends the request without an injection.

With `proactive_enabled` on, the plugin:

1. Polls one event at a time.
2. Generates a message with the route's persona, the event's context rendered the same way, and the tools above.
3. Tries each configured provider until one answers.
4. Sends the text, writes it into AstrBot's history after a `[proactive <reason>]` marker, and acknowledges `sent`.

A wrong platform, a missing persona, an empty reply, or a failed send ends in a `failed` acknowledgement, and the gateway offers the event again later.

### Troubleshooting

Check the AstrBot log for lines that begin with `[dusha]`, and confirm the gateway answers:

```sh
curl http://127.0.0.1:8765/health
```

## Proxy

`dusha proxy <name>` runs on the machine with your harness and reads the `proxy` section of the companion's `config.json`. When `<name>` has no `config.json` on that machine, the first run asks for the server's URL and token, saves them in a new file readable only by you, and starts. Later runs just start. On the server's own machine the file exists, and the proxy uses the server in it. The proxy forwards model requests to your provider and makes the server calls from the [client flow](#client-flow) itself, so a harness only needs a base URL. Keys are in [configuration.md](configuration.md#proxy).

Each entry under `proxy.upstreams` is a profile:

```json
{
  "proxy": {
    "server": {"url": "https://dusha.example.com", "token": "replace-with-the-server-api-token"},
    "upstreams": {
      "openai": {"base_url": "https://api.openai.com/v1"},
      "llama": {"base_url": "http://127.0.0.1:8080/v1"},
      "anthropic": {"base_url": "https://api.anthropic.com", "headers": {"x-api-key": "{env:ANTHROPIC_API_KEY}"}}
    }
  }
}
```

In your harness, replace the provider base URL with `http://127.0.0.1:8766/<profile>`. The proxy appends the rest of the request path to the profile's `base_url` unchanged, so any `/v1` in the path belongs to the provider. When the base URL already ends in `/v1`, a leading `v1/` in the path is dropped instead of doubled. A model request goes to the profile's `endpoints` entry for its protocol when it has one:

| Harness request | Forwarded to |
| --- | --- |
| `http://127.0.0.1:8766/openai/chat/completions` | `https://api.openai.com/v1/chat/completions` |
| `http://127.0.0.1:8766/llama/models` | `http://127.0.0.1:8080/v1/models` |
| `http://127.0.0.1:8766/anthropic/v1/messages` | `https://api.anthropic.com/v1/messages` |
| `http://127.0.0.1:8766/openai/responses` | `https://api.openai.com/v1/responses` |

The proxy reads three protocols, picked by the end of the path of a `POST`:

| Path ends in | Protocol | Context goes to | Without a user message |
| --- | --- | --- | --- |
| `chat/completions` | OpenAI Chat Completions | The latest `user` message | A system message after any leading system messages |
| `messages` | Anthropic Messages | The latest `user` message with text, after its `tool_result` blocks | The end of `system` |
| `responses` | OpenAI Responses | The latest `user` message in `input`, or `input` itself when it is a string | The end of `instructions` |

The proxy:

- Sends each transcript to `/state/v1/transcript` as Chat Completions messages, then puts the context at the start of the latest user turn as the table shows.
- Builds the context once per user message and reuses it for the tool calls that follow, so the provider's prompt cache keeps everything up to that message.
- Archives the reply, streamed or not, including streamed tool calls, and forwards the response byte for byte, including upstream errors.
- Follows `previous_response_id` on the Responses protocol, so a request that sends only new items continues its conversation. The proxy remembers recent response ids in memory, and after a restart such a request starts a new conversation.
- Forwards every other path untouched, such as `models` or `messages/count_tokens`, and so does a body without the protocol's shape. Nothing is stored for them.
- Forwards the caller's headers and query string, including `Authorization`, then sets the profile's `headers` on top. It drops the routing headers, `X-Companion-Token`, and hop-by-hop headers.
- Sends `X-Companion-Token` to the server itself. Harnesses never need the server token.
- Forwards without context when the server is down or rejects the token, and logs a warning. The turn and its reply wait in the [outbox](#outbox) until the server is back.
- Returns `404` for an unknown profile and `502` when the provider cannot be reached.

Routing headers are optional:

| Header | Default | Purpose |
| --- | --- | --- |
| `X-Conversation-Id` | Inferred | Names the conversation. See [conversation matching](#conversation-matching). |
| `X-Harness` | The profile name | Labels stored messages. |
| `X-Companion-Route` | `""` | Return address for proactive messages. |

### Conversation matching

The server keeps each transcript as a tree of messages. A message matches when its role, text, tool calls, and `tool_call_id` equal a stored one under the same parent. Other fields, such as `cache_control` or `refusal`, and the JSON spacing of tool arguments do not count.

- A transcript that extends a stored one continues that conversation. Only the new messages are stored.
- An edited or regenerated turn becomes a branch of the same conversation. The shared messages are not stored again.
- A transcript whose first message matches nothing starts a new conversation named `auto-<id>`.
- Two sessions that open with the same message share a conversation. Send `X-Conversation-Id` to keep them apart.
- History rewritten from its first message, such as a compacted session, starts a new conversation.

`GET /health` on the proxy reports `server.status` as `ok`, `unauthorized`, `unreachable`, or `http <status>`, and lists the profile names.

## MCP

`dusha serve` serves the companion's tools over MCP at `/mcp` with the streamable HTTP transport. Register it in your harness as an HTTP MCP server, and set `X-Companion-Token` when [auth](guide.md#auth) is on. The keys differ by harness, a common shape is:

```json
{
  "mcpServers": {
    "dusha": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp",
      "headers": {"X-Companion-Token": "replace-with-the-server-api-token"}
    }
  }
}
```

The server lists the tools of active plugins only:

| Tool | Plugin | Does |
| --- | --- | --- |
| `memory_search` | `memory` | Search the archive |
| `memory_get` | `memory` | Read one message with its neighbors |
| `evergreen_list` | `evergreen` | List evergreen facts |
| `evergreen_remember` | `evergreen` | Store a new fact |
| `evergreen_revise` | `evergreen` | Replace a fact at `expected_revision`. `clear_review_after` and `clear_expires_at` remove those times |
| `evergreen_forget` | `evergreen` | Forget a fact at `expected_revision` |
| `memo_add` | `memo` | Add a memo |
| `memo_list` | `memo` | List memos |
| `memo_done` | `memo` | Archive a memo |
| `affect_status` | `affect` | Read the affect state |

A tool that fails returns `isError` with the reason in `detail`. The harness reaches the server directly, so every tool fails while the server is down and nothing waits in the [outbox](#outbox).

- The server answers each request with JSON and never opens an SSE stream. `GET` and `DELETE` on `/mcp` return `405`.
- It speaks MCP `2025-11-25`, `2025-06-18`, and `2025-03-26`. Another `MCP-Protocol-Version` header returns `400`.
- A request with an `Origin` header that names no loopback host returns `403`.

## Commands

`dusha` builds every command other than `serve`, `proxy`, `list`, and `plugins` from the server, see [Commands](https://github.com/Somme4096/dusha#commands). Both routes need the token when auth is on.

- `GET /cli/v1/commands` returns `groups` (name to help), `commands`, and `plugins` (each plugin's state). Each command holds `group`, empty for a top-level command such as `health`, plus `name`, `help`, and `args`.
- `POST /cli/v1/run/<group>/<name>`, or `/cli/v1/run/<name>` for a top-level command, takes the arguments as a JSON object and returns the handler's result.

An unknown command returns `404`. An argument the command does not take, a missing one, or a value of the wrong type returns `422`. A command that fails returns `400` with the reason in `detail`.

## Outbox

The proxy keeps an outbox, `outbox.sqlite3` in the `data_dir` of the companion's home on its own machine. Transcripts wait there while the server is unreachable or answers `401`, `408`, `429`, `502`, or `504`, and the proxy sends them in order once it answers again. It retries every two seconds. A transcript the server rejects for another reason is logged and dropped.

## Versions

- The OpenAPI `version` follows the installed package version.
- `/state/v1` is the state API major version.
- `context.version` is the injection schema version, now `1`.
- An evergreen fact `revision` counts that fact's stored changes.

## See also

- [configuration.md](configuration.md): every config key.
- [guide.md](guide.md): service setup, auth, backup, troubleshooting.
