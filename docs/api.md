# Dusha API and integration

The gateway stores raw conversation memory, persistent affect, evergreen facts, and proactive delivery decisions in one SQLite file. It does not own the persona or the model. You send it messages, ask for a context injection, then call your provider yourself or let the optional proxy call it for you. Setting `storage.enabled` to `false` keeps the file but closes the message, memory, context, evergreen, and memo routes.

Base URL: `http://127.0.0.1:8765`. FastAPI serves the live schema at `/docs` and `/openapi.json` while auth is off. Setting `api_token_env` disables both.

The state API lives under `/state/v1/*`. The optional OpenAI-compatible proxy lives under `/v1/*` while `api_openai.enabled` is `true`. `/health` reports process and index status.

## Endpoints at a glance

| Method | Path | Purpose | Statuses |
| --- | --- | --- | --- |
| GET | `/health` | Process, database, and index status, plus `companion`, the name of the companion home this process serves | 200 |
| POST | `/state/v1/messages` | Store one message, update affect for a new user message | 200, 401, 422, 503 |
| GET | `/state/v1/messages/{message_id}` | Read one stored message | 200, 401, 404, 503 |
| POST | `/state/v1/memory/search` | Search the archive with adjacent context | 200, 401, 503 |
| GET | `/state/v1/memory/{message_id}` | Read a message plus neighbors | 200, 401, 404, 503 |
| GET | `/state/v1/memory/index` | Report index status from the memory plugin or the built-in index | 200, 401, 503 |
| POST | `/state/v1/context` | Build the provider-neutral injection | 200, 401, 422, 503 |
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
| GET | `/v1/models` | Proxy a model list to `upstream.base_url` | 401, 503, upstream |
| POST | `/v1/chat/completions` | Proxy chat completions and ingest the transcript | 401, 422, 503, upstream |

The `401` entries apply while `api_token_env` is set.

With `storage.enabled` set to `false`, the message, memory, context, evergreen, and memo routes return `503 {"detail":"built-in message storage is disabled"}`. `POST /v1/chat/completions` returns the same `503` because the proxy stores the transcript. Affect, proactive, and health routes stay available, and `POST /state/v1/proactive/evaluate` answers `{"event": null}` until storage returns. Re-enabling storage restores the routes, and the stored rows remain on disk.

Setting `api_openai.enabled` to `false` removes both `/v1/*` routes. Requests to them return `404` and the OpenAPI document omits them. Both proxy routes also return `503` while `upstream.base_url` is empty.

`/docs` and `/openapi.json` carry request models, response schemas, and error shapes. Treat this page as the integration guide and the OpenAPI document as the field reference. JSON blocks below parse as written.

## Auth and boundaries

Set `api_token_env` to an environment variable name to protect the data and model routes. Each `/state/v1/*` request, plus `/v1/models` and `/v1/chat/completions`, then needs the variable's value in `X-Companion-Token`. A missing or wrong token returns `401 {"detail":"invalid companion token"}`. An empty `api_token_env` leaves the API open, which suits a trusted network. The gateway reads the variable once at startup, so a new value needs a restart.

Auth covers `/state/v1/*`, `/v1/models`, and `/v1/chat/completions`. `/health` stays public. With auth on, the gateway disables `/docs`, `/redoc`, and `/openapi.json` to keep the public surface small. With auth off it serves them as usual.

The proxy picks its upstream credential per request. A nonempty value in the environment variable named by `upstream.api_key_env` makes the proxy send `Bearer <value>` and ignore the caller `Authorization` header. Otherwise the proxy forwards the caller `Authorization` header. The configured key wins.

## Client flow

A full exchange runs four steps. The proxy performs all four when you point a client at `/v1/chat/completions` with the routing headers.

### 1. Ingest the user message

`POST /state/v1/messages` stores one canonical message. `content` accepts a plain string or OpenAI-style structured content. A new user message marks user contact, cancels unsent proactive events, and runs the two affect steps described under [Affect](#affect). Assistant and tool messages store as usual.

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

The response returns the stored `id`, a `duplicate` flag, the internal `conversation_id`, the message `sha256`, and `affect`. For a new user message where the decision plugin or semantic appraisal picked a dimension, `affect` holds `emotion`, `increment`, `decision_id`, and the updated `state`. That `state` includes phrase deltas applied on the same turn. `affect` is `null` for assistant and tool messages, for duplicates, for a turn where neither source picked a dimension, and for a turn where phrase deltas alone moved the state. The request model forbids unknown fields, so a supplied `affect_label` returns `422`.

Replaying the same `external_id` inside the same `(harness, conversation_id)` stores one message and returns `duplicate: true` with the original `id`, without running the affect steps again. That scope is the deduplication the API performs. No global idempotency layer exists.

### 2. Build context

`POST /state/v1/context` returns the injection string plus structured metadata. It performs no ingestion and no provider call, so it works without upstream credentials.

```json
{
  "harness": "my-harness",
  "conversation_id": "stable-conversation-id",
  "query": "Remember the amber window.",
  "exclude_message_ids": [1],
  "include_recent": false
}
```

Pass `include_recent: true`, the default, when the gateway should add recent messages to the injection. Pass `false` when your provider conversation holds recent history and you want recalled records alone. `exclude_message_ids` removes messages you have sent.

The response carries `injection`, `affect`, `evergreen_facts`, `records`, `search_hits`, and `context`. The gateway builds `injection` from up to five parts, in this order:

1. The raw identity text, when you configured one.
2. The evergreen block, a JSON list of the facts that fit.
3. The memory plugin block, when an enabled memory plugin returned context.
4. The memo block, a JSON list of active memos with `id`, `text`, and `created_at`.
5. The companion state block, one JSON object holding the instructions, the emotion values, and the session records.

Parts 2 to 5 sit between delimiters from `prompts.json`. The `evergreen` and `companion_state` slots hold their own pairs, and the `context_blocks` slot holds the pairs for the memo block and the memory plugin block. The packaged opening delimiters are `<evergreen_facts>`, `<memory_context>`, `<memo_notes>`, and `<companion_state>`. Stored text cannot break the delimiters because the serializer escapes `&`, `<`, and `>`.

`context` mirrors the same data as fields: `version`, `identity`, `instructions`, `memory`, and `emotion`. `memory` holds `evergreen`, `session`, `memo_notes`, and, when the plugin block made it in, `plugin_context`. A harness that wants fields reads `context`. A harness that wants one string prepends `injection` to its system prompt.

The whole injection must fit `memory.injection_max_chars`. The gateway keeps the identity and the companion state block whole. If those two exceed the budget, the endpoint returns `422` with a detail naming the required size. The optional content then claims the remaining space in a fixed order: evergreen facts, memos, the memory plugin block, and last the session records. Each list stops at the first item that does not fit. Memos count against the budget, up to `memo.max_items` notes and `memo.max_chars` characters for the whole memo block (defaults 10 and 2,000). The gateway shrinks the memory plugin block to the space left, dropping its records before cutting its text, and `memory.plugin_context_max_chars` caps that block before the budget applies.

### 3. Call the provider

Send the provider request with routing headers so conversations stay separate and proactive delivery stays routable:

```text
X-Conversation-Id: stable-conversation-id
X-Harness: my-harness
X-Companion-Route: opaque-return-address
```

`X-Companion-Route` is the return address the gateway uses for a proactive message.

### 4. Archive the assistant reply

Call `POST /state/v1/messages` with `role: "assistant"`. The gateway stores the reply and leaves affect untouched. Affect steps run for new user messages alone.

## Affect

`GET /state/v1/affect` returns `base` and `mood` for every dimension in the resolved `emotions.json`, timestamps, and `unanswered_proactive`. This read advances decay and silence effects to the current time, then persists the result. Reading affect changes state.

A new user message can move affect in two steps. The gateway runs them in this order.

Step one picks at most one dimension. The gateway calls the configured decision plugin (see [configuration.md](configuration.md#decision-plugins)), and the plugin returns one dimension name from the resolved `emotions.json` or abstains. With `embedding` configured, semantic appraisal picks the dimension when the decision plugin is absent, abstains, fails, or returns an unknown name (see [configuration.md](configuration.md#shared-embedding-and-semantic-affect)). The engine raises the picked dimension by that dimension's decision increment, clamps it, and writes one row to `affect_decisions`. The ingest response reports this step in `affect`. A turn without a picked dimension skips the step.

Step two applies phrase deltas. It runs with a memory plugin enabled and storage on. The gateway sends the message text to the plugin's `match_phrase` operation and applies the per-dimension deltas the plugin returns. One message can move several dimensions, and a delta can be negative. These deltas bypass the decision increment. The engine scales each delta by `impact_scale` and by the room left in the value range, then holds the result at or above the dimension floor. The gateway writes no `affect_decisions` row for phrase deltas, and a turn that moved on phrase deltas alone returns `affect: null` from ingest. A failed `match_phrase` call changes nothing. The engine skips delta names missing from `emotions.json` and logs a warning.

The gateway keeps no keyword list of its own, so phrase rules belong to the memory plugin. Labels, staging, and the agent labeling endpoints are gone.

## Evergreen facts

Evergreen facts are append-only revisions. Every change increments the fact `revision`. Revise and forget use an optimistic `expected_revision` check, so a stale value returns `409` with the current revision. The check applies to one fact at a time. The API has no global compare-and-swap layer.

Lifecycle:

1. `POST /state/v1/evergreen/facts` remembers a fact under a stable `key`. A duplicate active key returns `409`.
2. `GET /state/v1/evergreen/facts` lists current revisions ordered by priority. Query `include_inactive`, `due_only`, and `limit`.
3. `GET /state/v1/evergreen/facts/{fact_id}/history` lists every revision. A missing fact returns `404`.
4. `POST /state/v1/evergreen/facts/{fact_id}/revisions` writes a new revision and preserves history.
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

The fact object carries `fact_id`, `revision`, `state`, `effective_state`, `priority`, `review_due`, and timestamps. `state` holds `active` or `forgotten`. `effective_state` accounts for the clock, so an expired fact reads `expired`. The gateway injects active, unexpired facts. It does not extract facts from conversation, merge claims, or call a model to manage them.

## Memos

Memos are short notes the agent leaves for itself, such as a loose end to pick up in a later proactive message. They belong to the whole gateway, so the routes take no harness or conversation. Each context build injects up to 10 active memos, oldest first.

`POST /state/v1/memo/add` stores one active memo:

```json
{
  "text": "Ask how the interview went."
}
```

The gateway trims `text` and returns `422` when it is blank or longer than `memo.text_max_chars` (default 4,000). The response wraps the stored row:

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

`GET /state/v1/memo/list?status=active&limit=20` returns `{"memos": [...]}` with the same row shape, oldest first. `status` accepts `active` or `archived` and defaults to `active`. A different value returns `422`. `limit` runs from 1 to 500 and defaults to 20.

`POST /state/v1/memo/{note_id}/done` archives one memo and requires a reason:

```json
{
  "reason": "Asked, the interview went well."
}
```

The response is `{"memo": {...}}` with `status` set to `archived`, the trimmed `reason`, and `archived_at`. An unknown `note_id` returns `404 {"detail":"memo not found"}`. A blank reason, a reason longer than `memo.reason_max_chars` (default 1,000), or a memo that is already archived returns `422`.

## Memory index status

`GET /state/v1/memory/index` has two response shapes. With a memory plugin enabled, the body is the object the plugin's `status` operation returns. The gateway passes it through unchecked, so its fields depend on the plugin. Without a plugin, or when the plugin's `status` call fails, the gateway answers for its built-in lexical index: `mode` and `retrieval` read `lexical`, `messages` counts stored messages, `storage_enabled`, `plugin_configured`, and `plugin_enabled` report the setup, and the chunk and embedding counters stay at zero. `/health` embeds the same object under `memory_index`.

## Proactive delivery

Proactive delivery stores a decision before sending, so a restart does not reset the daily and unanswered limits.

`POST /state/v1/proactive/evaluate` runs one pass and stores an event when one is due. It returns `{"event": null}` when nothing is due, or the stored payload.

`GET /state/v1/proactive/events?consumer=my-harness&harness=my-harness&limit=1` leases pending events for that consumer. The gateway stamps each leased event with `lease_until`. A lease that expires returns the event to pending on the next poll, so a consumer that never acknowledges sees the event again. Delivery is at-least-once.

Each payload holds `id`, `target` (`harness`, `conversation_id`, `route`), `reason`, `generation_instruction`, `generation_base`, `generation_variant`, `generation_variant_index`, `ruling_feeling` (`dimension`, `value`, `neutral`, `deviation`), `ladder_stage`, `unanswered_proactive`, `silence_minutes`, `silence_text`, `last_user_message_at`, `evaluated_at`, `context`, and `created_at`. A polled payload adds `lease_until`.

`reason` comes from `proactive.triggers` in the resolved `emotions.json`. The engine walks that list in order and copies the `reason` string of the first trigger whose dimension has reached its threshold. The packaged file ships two triggers with the reasons `fear` and `silence`, and your own file can name others. A pass where no trigger matches creates no event.

Send the message to the target route, then acknowledge:

```json
{
  "consumer": "my-harness",
  "outcome": "sent",
  "text": "the exact sent message",
  "external_id": "",
  "error": ""
}
```

`outcome` accepts `sent`, `failed`, or `release`. `sent` archives the text as an assistant message and records the send. `failed` returns the event to pending after `retry_delay_minutes`. `release` returns it at once. The same consumer must hold the lease. A different consumer gets `409`, and an unknown `event_id` gets `404`.

The engine gates each pass on silence, quiet hours, cooldown, the daily limit, the unanswered limit, and the triggers. It keeps one unsent event at a time and keys duplicates on the last user message, the local date, and the day's send count. A user reply cancels unsent events and resets the unanswered count.

## AstrBot plugin

The plugin in `integrations/astrbot_dusha` connects AstrBot to the gateway as a state service. It requires AstrBot 4.8 or later and supports Discord.

### Install

Run the gateway first. Copy the source directory into AstrBot's plugin directory:

```sh
cp -a integrations/astrbot_dusha \
  /path/to/AstrBot/data/plugins/astrbot_dusha
```

Restart AstrBot or reload the plugin from its manager. AstrBot reads `metadata.yaml` and installs the dependency from `requirements.txt` (`httpx`). Keep AstrBot's chat provider pointed at your model provider. Do not set the gateway as AstrBot's chat provider while the plugin runs.

### Settings

| Field | Default | Meaning |
| --- | --- | --- |
| `gateway_url` | `http://127.0.0.1:8765` | Gateway address reachable from AstrBot |
| `platform_id` | `""` | Exact AstrBot platform ID allowed to use the gateway |
| `api_token` | `""` | Value for `X-Companion-Token` when the gateway requires one |
| `harness` | `astrbot` | Harness name the plugin sends with messages, context requests, and proactive polls |
| `request_timeout_seconds` | `60` | Seconds to wait for one gateway response. Values below 1 clamp to 1 |
| `poll_interval_seconds` | `30` | Seconds between proactive polls. Values below 5 clamp to 5 |
| `proactive_enabled` | `true` | Poll and deliver proactive events |
| `proactive_tool_keywords` | `["donsetch", "fetch"]` | Name fragments that add AstrBot tools to proactive messages |

`_conf_schema.json` holds these defaults, and the plugin reads its fallback values from that file. A config saved under the old names `poll_seconds` and `enable_proactive` still loads. The plugin renames both keys once at startup and keeps the new key when a file carries both.

An empty `platform_id` disables routing and proactive polling. Run `/sid` through the Discord bot that should own this companion and copy its `Bot ID` into `platform_id`. The plugin ignores other adapters in the same AstrBot process and strips its gateway tools from their LLM requests.

The gateway keys each conversation by harness name plus AstrBot's session ID. Keep `harness` at `astrbot` once conversations exist. After a rename each chat opens a new conversation with empty recent history, and the plugin stops receiving proactive events queued under the old name. Older messages stay searchable.

Storing a user message can take longer than a plain read. The gateway may run the decision plugin, then the memory plugin's `match_phrase` and `ingest_messages` operations, and each has a 15 second budget by default. The 60 second default for `request_timeout_seconds` covers all three. Raise it when you raise `decision.timeout_seconds` or `memory_plugin.timeout_seconds` on the gateway.

### Tools

The plugin registers nine LLM tools when the provider supports tools:

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

The nine tools read or manage memory, facts, and memos. Tools return JSON with `ok: true` on success, or `ok: false` plus `error` and, for HTTP failures, `status`. `yumecho_done` rejects an empty `reason` before it calls the gateway. The AstrBot plugin exposes no affect tool, because the gateway updates affect during message ingest. The OpenCode plugin still offers an affect status tool, which reads `GET /state/v1/affect`. It prefixes its tools with the companion name it adopts from `/health`, for example `dusha_affect_status`, and falls back to `dusha_` when no name is set.

### Behavior

For each exchange the plugin stores the user message, then requests context with `include_recent: true` and `exclude_message_ids` set to the stored message. It adds the injection to AstrBot's current request as an extra user content part, or appends it to the system prompt on AstrBot builds without those parts. The active persona stays as AstrBot built it. A request that already carries `<companion_state>` gets no second injection. After the model responds, the plugin archives the assistant text. The gateway updates affect while it stores the user message, so the plugin sends no affect calls of its own.

A failed store call does not cancel the context call. If storing the message fails or times out, the plugin logs `Message archive failed` and still requests context, with an empty `exclude_message_ids`, so the turn keeps its injection. The gateway may finish storing after the plugin gave up, and the injected records can then repeat the current message. The evergreen tools also send no `source_message_id` on that turn. If the context call fails, the plugin logs `Context unavailable` and AstrBot sends the request without an injection.

With `proactive_enabled` on, the plugin polls `GET /state/v1/proactive/events` for one event at a time and passes the `harness` setting as both `consumer` and `harness`. For each event it checks that the route belongs to `platform_id` and loads the route's persona. The prompt is the event's `generation_instruction` plus a note that pending yumecho memos sit in context and that the model should call `yumecho_done` for one the message completes. The system prompt is the persona followed by the event's injection.

Generation can call tools. The plugin offers the nine gateway tools plus each registered AstrBot tool whose name contains one of `proactive_tool_keywords`, ignoring case. An empty list leaves the nine gateway tools. On an AstrBot build that provides `tool_loop_agent`, the plugin runs that loop on the session's current chat provider and hands it the other configured providers as fallbacks. AstrBot switches provider inside the loop, so a tool that ran does not run twice. If AstrBot cannot find the starting provider, the plugin starts the loop on the next one. On a build without the loop, the plugin calls `llm_generate` with the same tools, one provider after another, until one answers.

The plugin sends the text through `Context.send_message`, writes it into AstrBot's conversation history after a `[proactive <reason>]` marker turn, and acknowledges `sent` with the text. A wrong platform, a missing persona, an empty reply, a failed send, or a failure on the last provider ends in a `failed` acknowledgement that carries the error. The gateway offers the event again after `retry_delay_minutes`.

### Troubleshooting

Check the AstrBot log for lines beginning with `[dusha]`. Confirm the gateway answers before you change plugin settings:

```sh
curl http://127.0.0.1:8765/health
```

With `api_token_env` set on the gateway, put the same value in the plugin's `api_token` field.

## Optional OpenAI proxy

`GET /v1/models` and `POST /v1/chat/completions` forward to `upstream.base_url` and append the path. The proxy targets OpenAI-shaped bodies.

- It inserts the context system message after any leading system messages, or at the front when none exist.
- It preserves unknown request fields and per-message fields.
- It archives the transcript's user, assistant, and tool turns before the call, and the assistant reply after a successful call.
- It forwards non-streaming responses byte for byte, including upstream errors and status codes.
- It forwards streaming responses as the raw SSE byte stream.

Both routes exist while `api_openai.enabled` is `true`, the default. `/v1/models` returns `503` when `upstream.base_url` is empty. `/v1/chat/completions` returns `503` in the same case, `503` with `built-in message storage is disabled` when `storage.enabled` is `false`, and `422` when `messages` is not a list or the injection budget cannot fit. The proxy injects context and archives turns, so it needs storage. It requires the companion token when `api_token_env` is set.

The proxy forwards OpenAI chat shapes. It passes one authorization header plus content-type and rewrites nothing else. OAuth flows and OpenAI Responses-style endpoints stay out of scope. Point AstrBot at the state API through this plugin. AstrBot cannot place its changing session ID in static provider headers, so a proxy route would collapse conversations into `default`. Other harnesses can use the proxy when they can set `X-Conversation-Id`, `X-Harness`, and `X-Companion-Route`.

## Versions and revisions

- The OpenAPI `version` follows the installed package version, the `version` field in `pyproject.toml`. It labels the whole API document, and endpoints carry no version of their own.
- The `/state/v1` prefix is the state API major version.
- `context.version` (`1`) is the injection schema version.
- `context.identity.revision` is a SHA-256 of the raw identity text.
- An evergreen fact `revision` counts that fact's stored changes. `expected_revision` compares against it.
- The affect state keeps an internal `revision` counter that increments on every persisted advance. The API does not expose it.
- The database schema version is `5`. Startup upgrades a version 2, 3, or 4 database in place and rejects other versions. Version 5 added the `memo_notes` table. An upgrade from version 2 or 3 renames the retired label tables to `legacy_affect_events` and `legacy_affect_classifications`. No runtime code reads them, and the gateway applies none of the pending labels they hold.

## Related documentation

- [configuration.md](configuration.md) covers the config file, identity and emotion files, retrieval, and proactive scheduling.
- [guide.md](guide.md) covers service setup, auth and network, backup, and troubleshooting.
- [README.md](../README.md) gives the project overview and quickstart.
