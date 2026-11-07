# API and integration

The server listens on `http://127.0.0.1:8765`. It serves the state API under `/state/v1/*`, the [MCP](#mcp) tools at `/mcp`, and the [CLI routes](#cli-routes) under `/cli/v1/*`. The [proxy](#proxy) runs next to your harness on `http://127.0.0.1:8766`.

FastAPI serves the field reference at `/docs`, `/redoc`, and `/openapi.json` while [auth](guide.md#auth) is off.

| Part | Address | Used for |
| --- | --- | --- |
| Server | `http://127.0.0.1:8765` | State API, MCP, CLI routes |
| Proxy | `http://127.0.0.1:8766/<profile>` | Model requests from your harness |

## Proxy

Your harness only needs a base URL. The proxy forwards model requests to your provider, and it makes the server calls for you by following the [client flow](#client-flow). Keys are in [configuration.md](configuration.md#proxy).

### Start the proxy

Run `dusha proxy <name>` on the machine with your harness. If no server is saved for `<name>`, a run in a terminal asks which one to use:

1. Pick this machine's server, if it has one, or enter another server's URL and token.
2. The proxy checks the connection. If the check fails, it offers to try again.
3. It saves the choice to `config.json` with mode `600`, then starts.

Later runs start straight away. Outside a terminal, a missing file is asked for line by line. A server's own `config.json` without a saved server uses that server.

Check the proxy with `GET /health`. It reports `server.status` as `ok`, `unauthorized`, `unreachable`, or `http <status>`, and it lists the profile names.

### Provider commands

Each entry under `providers` is a profile:

```json
{
  "proxy": {
    "server": {"url": "https://dusha.example.com", "token": "replace-with-the-server-api-token"}
  },
  "providers": {
    "openai": {"base_url": "https://api.openai.com/v1"},
    "llama": {"base_url": "http://127.0.0.1:8080/v1"},
    "anthropic": {"base_url": "https://api.anthropic.com", "headers": {"x-api-key": "{env:ANTHROPIC_API_KEY}"}}
  }
}
```

`dusha provider add` adds a profile from the [models.dev](https://models.dev) catalog, with the provider's key, endpoints, and required headers. It edits the `config.json` of the companion you pick with `-c <name>`, or the file picked by the usual [order](configuration.md#which-file-loads). In a terminal, it opens a menu you can filter by typing. Otherwise, name the provider:

```sh
dusha -c <name> provider add deepseek
```

The command asks for any values the provider's URL needs, then for the key. Press Enter at the key prompt to store `{env:NAME}`, a reference to the provider's environment variable, instead. The profile is named after the provider unless you pass `--profile`. The file keeps mode `600`. Replacing an existing profile asks first. Restart the proxy and the server to use it.

```json
{
  "providers": {
    "opencode-go": {
      "base_url": "https://opencode.ai/inference/go/openai/v1",
      "endpoints": {"messages": "https://opencode.ai/inference/go/anthropic/v1"},
      "key": "replace-with-your-key",
      "auth": "bearer",
      "headers": {"x-opencode-session": "{conversation_id}"}
    }
  }
}
```

In the harness, add a custom OpenAI- or Anthropic-compatible provider with the base URL `http://127.0.0.1:8766/opencode-go` and any dummy key. The proxy drops the dummy key and sends its own.

- `dusha provider list` prints each profile with its key masked.
- `dusha provider remove <profile>` removes one.
- The catalog is cached in `$XDG_CACHE_HOME/dusha/models-dev.json`, which is `~/.cache` by default, for a day. `--refresh` fetches it again. Offline, `add` uses the stale copy.
- Providers that need cloud credentials rather than a key, such as Bedrock, Vertex, Azure, and Copilot, are not offered. Gemini's native protocol is not routed, but Google is offered through its OpenAI-compatible endpoint.

### Routing

Point the provider base URL in your harness at `http://127.0.0.1:8766/<profile>`. The proxy appends the rest of the request path to the profile's `base_url` unchanged, so any `/v1` in the path belongs to the provider. When `base_url` already ends in `/v1`, a leading `v1/` in the path is dropped instead of doubled. A model request goes to the profile's `endpoints` entry for its protocol when it has one.

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

For each request, the proxy:

- Sends each transcript to `/state/v1/transcript` as Chat Completions messages, then puts the context at the start of the latest user turn, as the table shows.
- Builds the context once per user message and reuses it for the tool calls that follow. The provider's prompt cache keeps everything up to that message.
- Archives the reply, streamed or not, including streamed tool calls. It forwards the response byte for byte, including upstream errors.
- Follows `previous_response_id` on the Responses protocol, so a request that sends only new items continues its conversation. The proxy remembers recent response ids in memory. After a restart, such a request starts a new conversation.
- Forwards every other path untouched, such as `models` or `messages/count_tokens`. So does a body without the protocol's shape. Nothing is stored for them.

### Headers

The proxy forwards the caller's headers and query string, including `Authorization` unless the profile has a `key`. It then sets the profile's `headers` on top. It drops the routing headers, `X-Companion-Token`, and hop-by-hop headers.

It sends `X-Companion-Token` to the server itself, so harnesses never need the server token.

Routing headers are optional:

| Header | Default | Purpose |
| --- | --- | --- |
| `X-Conversation-Id` | Inferred | Names the conversation. See [conversation matching](#conversation-matching). |
| `X-Harness` | The profile name | Labels stored messages. |
| `X-Companion-Route` | `""` | Return address for proactive messages. `-` means none. |

### Failures

If the server is down or rejects the token, the proxy forwards the request without context and logs a warning. The turn and its reply wait in the [outbox](#outbox) until the server is back.

An unknown profile returns `404`. A provider that cannot be reached returns `502`.

### Conversation matching

The server keeps each transcript as a tree of messages. A message matches when its role, text, tool calls, and `tool_call_id` equal a stored one under the same parent. Other fields, such as `cache_control` or `refusal`, and the JSON spacing of tool arguments do not count.

- A transcript that extends a stored one continues that conversation. Only the new messages are stored.
- An edited or regenerated turn becomes a branch of the same conversation. The shared messages are not stored again.
- A transcript whose first message matches nothing starts a new conversation named `auto-<id>`.
- Two sessions of one harness that open with the same message share a conversation. Send `X-Conversation-Id` to keep them apart.
- History rewritten from its first message, such as a compacted session, starts a new conversation.

### Outbox

The proxy keeps an outbox, `outbox.sqlite3`, in the `data_dir` of the companion's home on its own machine. Transcripts wait there while the server is unreachable or answers `401`, `408`, `429`, `502`, or `504`. The proxy sends them in order once the server answers again, retrying every two seconds. A transcript the server rejects for another reason is logged and dropped.

## MCP

`dusha serve` serves the companion's tools over MCP at `/mcp`, using the streamable HTTP transport. Register it in your harness as an HTTP MCP server, and set `X-Companion-Token` when [auth](guide.md#auth) is on. Harness configs differ. This is a common shape:

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

The server lists the tools of active plugins only. The built-in plugins add the tools below. An external plugin adds its own, see [Tools](https://github.com/Somme4096/dusha#tools--mcp).

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

A tool that fails returns `isError` with the reason in `detail`. The harness reaches the server directly, so every tool fails while the server is down. Nothing waits in the [outbox](#outbox) for MCP.

- The server answers each request with JSON and never opens an SSE stream. `GET` and `DELETE` on `/mcp` return `405`.
- It speaks MCP `2025-11-25`, `2025-06-18`, and `2025-03-26`. Another `MCP-Protocol-Version` header returns `400`.
- A request with an `Origin` header that names no loopback host returns `403`.

## Client flow

One exchange takes four steps. The [proxy](#proxy) runs all four for you. Follow these steps when you build your own client.

### 1. Store the user message

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

The response has the stored `id`, a `duplicate` flag, and `conversation_id`. Each plugin that returns a result adds its own key. The affect plugin's key is `affect`.

- A new user message cancels unsent proactive events and updates [affect](#affect). See [Proactive delivery](#proactive-delivery).
- `affect` holds `emotion`, `increment`, `decision_id`, and `state` when the message moved an emotion. Otherwise the key is absent.
- A repeated `external_id` in the same harness and conversation stores nothing. It returns `duplicate: true` with the original `id`.

### 2. Build context

`POST /state/v1/context` builds the injection. It stores no messages and calls no provider, but it advances affect decay.

```json
{
  "harness": "my-harness",
  "conversation_id": "stable-conversation-id",
  "query": "Remember the amber window.",
  "exclude_message_ids": [1],
  "include_recent": false
}
```

- `include_recent` defaults to `true`. Pass `false` when your provider conversation already holds the recent history.
- `exclude_message_ids` removes messages you send yourself.

The response has `injection`, `records`, `search_hits`, and `context`. Prepend `injection` to your system prompt. `context` holds the same data as fields: `version`, `instructions`, `session`, and `blocks`.

The injection has one part per [plugin](configuration.md#plugins), in `plugins` order, then the state block. With the packaged order, and external plugins listed after the built-in ones:

1. The identity text
2. `<evergreen_facts>`, a JSON list of facts
3. `<memo_notes>`, a JSON list of active memos
4. `<affect>`, the affect instruction and the Affect line as plain text
5. The blocks returned by external plugins, each with the delimiters that plugin renders
6. `<companion_state>`, a JSON object with each plugin's `instruction`, keyed by plugin name, and the session records from `memory`

A disabled plugin adds neither a part nor a key. Neither does a part with nothing to say.

`context.blocks` holds each part as data, keyed by plugin name. `identity` carries `text`, and `memory` carries the `search_hits`.

You can rename the delimiters in [prompts.json](configuration.md#promptsjson). The serializer escapes `&`, `<`, and `>` in stored text. The injection has no overall cap. Each part has its own limit in [config.json](configuration.md#overrides-in-configjson).

### 3. Call the provider

Send the request to your provider as usual. Pass the same `route` you sent on ingest, because it is the return address for proactive messages. Use `-` or an empty `route` for no return address. `-` is for clients that can't send an empty value.

### 4. Store the reply

Call `POST /state/v1/messages` with `role: "assistant"`. Assistant and tool messages leave affect untouched.

## Memory

### Archive search and read

Every stored message lands in the archive. Search it with `POST /state/v1/memory/search`:

```json
{
  "query": "amber window",
  "limit": 8,
  "context_messages": 1
}
```

`limit` sets how many hits come back and defaults to 8. `context_messages` sets how many neighbors each hit gets on each side, defaults to 1, and is capped at 10.

```json
{
  "results": [
    {
      "hit_id": 12,
      "rank": -1.4,
      "messages": [
        {"id": 11, "conversation_id": 3, "role": "user", "text": "Where did we leave off?"},
        {"id": 12, "conversation_id": 3, "role": "assistant", "text": "You mentioned the amber window."},
        {"id": 13, "conversation_id": 3, "role": "user", "text": "Yes, that one."}
      ]
    }
  ]
}
```

`GET /state/v1/memory/{message_id}?context_messages=1` returns the message and its neighbors as `messages`, in time order. `GET /state/v1/messages/{message_id}` reads one message without neighbors. An unknown id returns `404`.

### Evergreen facts

A fact keeps every revision. Revise and forget take `expected_revision`. A stale value returns `409` with the current revision.

- `POST /state/v1/evergreen/facts` remembers a fact under a stable `key`. A duplicate active key returns `409`.
- `GET /state/v1/evergreen/facts` lists current facts by priority. Query `include_inactive`, `due_only`, and `limit`.
- `GET /state/v1/evergreen/facts/{fact_id}/history` lists every revision.
- `POST /state/v1/evergreen/facts/{fact_id}/revisions` writes a new revision.
- `POST /state/v1/evergreen/facts/{fact_id}/forget` marks the fact forgotten and keeps its history.

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

The fact object has `fact_id`, `revision`, `state`, `effective_state`, `priority`, `review_due`, and timestamps. Two fields need a rule. `state` is `active` or `forgotten`. `effective_state` reads `expired` once `expires_at` passes. The server injects active, unexpired facts. It extracts no facts on its own.

### Memos

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

## Affect and proactive messages

### Affect

`GET /state/v1/affect` returns `base` and `mood` for each dimension, timestamps, and `unanswered_proactive`. The read also advances decay and silence drift to now, and it saves the result.

A new user message moves affect in up to two steps:

1. Each [plugin](configuration.md#plugins) that listens for `decide` proposes an emotion, deltas, or nothing. The first emotion wins. With [embedding](configuration.md#embedding) configured, semantic appraisal picks one when no external plugin proposes an emotion. The engine raises that dimension by its increment and reports it in the ingest `affect` field.
2. The merged `deltas` apply next. A delta can be negative, and one message can move several dimensions. The ingest `affect` field does not report these.

For example, if a `decide` plugin proposes `anger`, the engine raises anger by its increment, and the ingest response shows `affect` with that emotion. Deltas from the same plugin still apply in step 2, but the response does not show them.

### Proactive delivery

The server runs a proactive evaluation on its own schedule. A harness picks up the events in three steps:

1. `POST /state/v1/proactive/evaluate` runs one pass now. It returns the stored event, or `{"event": null}` when nothing is due.
2. `GET /state/v1/proactive/events?consumer=my-harness&harness=my-harness&limit=1` leases pending events to that consumer.
3. Send the message to the event's `target`, then acknowledge it.

A lease that expires returns the event to pending, so delivery is at-least-once. A user reply cancels unsent events and resets the unanswered count.

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

A consumer that does not hold the lease, or an unknown `outcome`, gets `409`.

## Plugins

`GET /state/v1/plugins` returns one object per plugin, keyed by name, in `plugins` order:

- `state`: `active`, `disabled`, `inactive` when a service it injects is missing, or `failed` when a plugin reported a runtime error via `ctx.error`
- `error`: the reason for `inactive` or `failed`, otherwise empty
- `inject` and `provides`: the services it uses and offers
- `events`: the events it listens for

`/health` lists the active plugins under `plugins`.

## Endpoints

With auth on, every `/state/v1`, `/cli/v1`, and `/mcp` route returns `401` without the token. `/health` stays public. Invalid input returns `422`. The table lists the other statuses. A `503` on a `/state/v1/*` route means its [plugin](configuration.md#plugins) is disabled or inactive, and the `detail` names the plugin.

| Method | Path | Purpose | Statuses |
| --- | --- | --- | --- |
| GET | `/health` | Process and database status, the `companion` name, and the names of the active plugins | 200 |
| POST | `/state/v1/messages` | Store one message, update affect for a new user message | 200 |
| POST | `/state/v1/transcript` | Store the new messages of a Chat Completions transcript, optionally `after` a stored message id. See [conversation matching](#conversation-matching). | 200 |
| GET | `/state/v1/messages/{message_id}` | Read one stored message | 200, 404 |
| POST | `/state/v1/memory/search` | Search the archive with adjacent context | 200, 503 |
| GET | `/state/v1/memory/{message_id}` | Read a message plus neighbors | 200, 404, 503 |
| GET | `/state/v1/plugins` | Report each plugin's state | 200 |
| POST | `/state/v1/context` | Build the injection | 200 |
| GET | `/state/v1/affect` | Read affect state and advance decay | 200, 503 |
| POST | `/state/v1/evergreen/facts` | Remember a fact | 200, 409, 503 |
| GET | `/state/v1/evergreen/facts` | List current facts | 200, 503 |
| GET | `/state/v1/evergreen/facts/{fact_id}/history` | List every revision | 200, 404, 503 |
| POST | `/state/v1/evergreen/facts/{fact_id}/revisions` | Revise with `expected_revision` | 200, 404, 409, 503 |
| POST | `/state/v1/evergreen/facts/{fact_id}/forget` | Forget with `expected_revision` | 200, 404, 409, 503 |
| POST | `/state/v1/memo/add` | Add one memo | 200, 503 |
| GET | `/state/v1/memo/list` | List active or archived memos | 200, 503 |
| POST | `/state/v1/memo/{note_id}/done` | Archive a memo with a reason | 200, 404, 503 |
| POST | `/state/v1/proactive/evaluate` | Run one evaluation pass | 200, 503 |
| GET | `/state/v1/proactive/events` | Poll and lease pending events | 200, 503 |
| POST | `/state/v1/proactive/events/{event_id}/ack` | Acknowledge a leased event | 200, 404, 409, 503 |
| POST | `/mcp` | MCP tools over streamable HTTP | 200, 202, 400, 403 |
| GET | `/cli/v1/commands` | The command list `dusha` builds its commands from | 200 |
| POST | `/cli/v1/run/{command}` | Run one command | 200, 400, 404 |

### CLI routes

`dusha` builds every command other than `serve`, `proxy`, `list`, and `plugins` from the server. See [Commands](https://github.com/Somme4096/dusha#commands). Both routes need the token when auth is on.

- `GET /cli/v1/commands` returns `groups` (name to help), `commands`, and `plugins` (each plugin's state). Each command has `group`, which is empty for a top-level command such as `health`, plus `name`, `help`, and `args`.
- `POST /cli/v1/run/<group>/<command>`, or `/cli/v1/run/<command>` for a top-level command, takes the arguments as a JSON object and returns the handler's result.

An unknown command returns `404`. A command that fails returns `400` with the reason in `detail`.

## Versions

- The OpenAPI `version` follows the installed package version.
- `/state/v1` is the state API major version.
- `context.version` is the injection schema version, now `3`.
- An evergreen fact `revision` counts that fact's stored changes.

### Deprecations

- `dusha proxy <name> auth login`, `status`, and `logout` still work with a warning until 2.0.21.

## See also

- [configuration.md](configuration.md): every config key.
- [guide.md](guide.md): service setup, auth, backup, troubleshooting.
- [AstrBot plugin](../integrations/astrbot_dusha/README.md): setup, settings, and troubleshooting for AstrBot.
