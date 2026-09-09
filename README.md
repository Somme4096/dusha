# Companion State Gateway

A small Python service that adds durable raw memory, persistent affect, and proactive-message events to an LLM companion. Personality stays in the harness. One SQLite file owns the companion state, so replacing AstrBot does not require a migration.

The service adapts Omemo's OpenAI-compatible proxy boundary and Drivesoid v2.0.0's affect dynamics. It has no WebUI, vector database, task broker, or generative memory pass.

## What it stores

Every message keeps its original JSON content, extracted text, timestamp, source, conversation, SHA-256 digest, and optional source message ID. SQLite FTS5 is a rebuildable index. Search results include the original hit and adjacent original messages.

Hybrid retrieval uses each message as a parent and deterministic character ranges as children. SQLite stores only child offsets and embedding vectors. A semantic hit resolves back to the complete parent and adjacent canonical messages before injection. Derived chunks and vectors can be rebuilt without changing conversation history.

Evergreen facts live in an append-only revision table in the same database. The agent creates, revises, and forgets them through explicit tools. The service injects the latest active, unexpired revisions in a fixed order. It does not extract facts, summarize conversations, merge claims, or call a model for lifecycle work.

Affect has fast `base` values and slower `mood` values for:

`vitality`, `fatigue`, `longing`, `intimacy`, `possessiveness`, `lust`, `jealousy`, `anxiety`, `protectiveness`, `fear`, `contentment`, `elation`, `seeking`, `play`, `dejection`, and `irritability`.

Supported event labels include `fear_separation`, `fear_death`, `fear_concern`, and `fear_general`. Harnesses and the CLI can submit an explicit label to bypass automatic appraisal.

Automatic affect reuses `embedding` when its URL and model are configured, independently of memory retrieval mode. Inspired by [affective-longing](https://github.com/pearthink123/affective-longing)'s embedding similarity and event-driven emotion ideas, messages are compared with short emotional prototypes using cosine similarity. Each weight is `max(0, (similarity - threshold) / (1 - threshold))`, bounded to one. Weights are normalized if their sum exceeds one; otherwise the remaining weight is neutral. The weighted sum of existing label deltas drives the existing bounded, habituated state dynamics. This remains deterministic for fixed embeddings, but no longer depends on literal word matches.

Memory retrieval and affect appraisal share an internal cosine similarity cutoff of `0.50`. Semantic affect is automatic when embeddings are configured; neither the cutoff nor an enable switch is exposed in configuration. Prototypes are cached in process; each new unlabeled user message adds one embedding request after initialization. No new dependency or database schema is required. Unrelated messages produce neutral deltas. If embeddings are missing or fail, the configurable phrase matcher is used, with the embedding failure cooldown preventing repeated requests during outages.

The strongest blended label controls habituation and follow-up scheduling; soothing is weighted across the blend. Affect events record blended nominal deltas (before habituation and state-dependent scaling) and semantic weights in the note for inspection. Similarity is a heuristic, not a calibrated emotion probability: negation and mixed intent still require evaluation against real conversations.

## Install and run

```sh
cp config.example.yaml config.yaml
mise exec -- uv tool install --editable .
export UPSTREAM_API_KEY='replace-me'
companion-gateway serve
```

The editable tool installation places `companion-gateway` in `~/.local/bin` and keeps its Python code linked to this checkout. Run the install command again after changing dependencies in `pyproject.toml`.

Check it:

```sh
curl http://127.0.0.1:8765/health
```

The OpenAI-compatible base URL is `http://127.0.0.1:8765/v1`. A generic client should send these headers so conversations remain separated and proactive delivery stays routable:

```text
X-Conversation-Id: stable-conversation-id
X-Harness: client-name
X-Companion-Route: opaque-return-address
```

The proxy accepts streaming and non-streaming chat completions. It forwards the caller's `Authorization` header unless `upstream.api_key_env` supplies a key.

## Hybrid retrieval

Set `memory.retrieval_mode` to `hybrid`, then configure an OpenAI-compatible embedding endpoint under `embedding`. The gateway appends `/embeddings` to `base_url`. It reads the API key from `api_key_env` and never stores that key in SQLite.

```yaml
memory:
  retrieval_mode: hybrid
embedding:
  base_url: http://127.0.0.1:11434/v1
  api_key_env: EMBEDDING_API_KEY
  model: your-embedding-model
  dimensions:
```

New messages receive child offsets during ingestion. The service embeds missing children in small background batches. Search combines FTS5 parent ranks and sqlite-vec child ranks through reciprocal-rank fusion. An unavailable embedding endpoint causes lexical fallback and a short retry cooldown.

Changing the endpoint, model, dimensions, or chunk settings selects a new derived index. Old vectors remain harmless until `memory index rebuild` removes all derived chunks and vectors.

Configuration uses `chunk_*` for chunk sizing, `min_*`/`max_*` for limits, and explicit units for durations. The shared provider is configured once under `embedding`. Startup automatically migrates old configuration keys, removes obsolete semantic controls, and saves the configuration once. Later starts leave an already migrated file untouched. Existing canonical values win if both names are present. YAML is reserialized during migration, so comments and formatting are not retained. HTTP fields and stored database schemas are unchanged.

## AstrBot and Discord

Install the directory `integrations/astrbot_companion_gateway` as an AstrBot plugin, set its gateway URL, and set `platform_id` to the selected Discord bot's exact AstrBot platform ID. Run `/sid` through that bot to find its `Bot ID`. An empty `platform_id` disables routing. Keep AstrBot's model provider pointed at the real upstream provider. The plugin ignores every other adapter in the same process.

The plugin polls pending events with `harness=astrbot`. It loads the active AstrBot persona, asks the configured provider for one message, sends through `Context.send_message`, and acknowledges the event. AstrBot's current Discord adapter supports that generic session path.

The plugin registers four evergreen tools: `remember_evergreen_fact`, `revise_evergreen_fact`, `forget_evergreen_fact`, and `review_evergreen_facts`. It also registers the read-only `search_conversation_memory` and `get_conversation_record` tools. Episodic message ingestion remains automatic. AstrBot keeps its recent conversation history, so the plugin asks the gateway to omit those recent records from injected memory.

Do not point AstrBot through the OpenAI proxy while the plugin is active. AstrBot cannot put its changing session ID into static provider headers, so that setup would create a second `default` conversation. Other harnesses can use the proxy when they can set the headers above.

## Proactive event API

The service evaluates silence, current drives, due follow-up events, quiet hours, cooldown, daily limit, and unanswered limit. It stores decisions before delivery, so restarts do not reset the limits or duplicate an event.

Poll and lease one event:

```sh
curl 'http://127.0.0.1:8765/state/v1/proactive/events?consumer=my-harness&harness=my-harness&limit=1'
```

After sending, acknowledge it:

```sh
curl -X POST http://127.0.0.1:8765/state/v1/proactive/events/EVENT_ID/ack \
  -H 'Content-Type: application/json' \
  -d '{"consumer":"my-harness","outcome":"sent","text":"the exact sent message"}'
```

Use `outcome: failed` to release it after the retry delay. A user reply cancels unsent events and resets the unanswered count.

## Memory and affect access

```sh
companion-gateway memory search '青い硝子'
companion-gateway memory show 42
companion-gateway memory recent
companion-gateway memory index status
companion-gateway memory index backfill --limit 256
companion-gateway memory index rebuild
companion-gateway evergreen list
companion-gateway evergreen remember user.preference.editor \
  'The user prefers Helix.' --reason 'The user stated this preference.'
companion-gateway affect show
companion-gateway affect event fear_concern \
  --note 'Travel safety concern' --follow-up-minutes 180
companion-gateway proactive evaluate
```

The CLI supports episodic inspection and evergreen audit or repair without another model call. The REST API supplies the AstrBot tools and remains available to later MCP bridges.

## Backup and service setup

Create a consistent live backup:

```sh
companion-gateway backup /path/to/backups/state-$(date +%F).sqlite3
```

For `systemd --user`:

```sh
mkdir -p ~/.config/companion-gateway ~/.config/systemd/user
cp config.example.yaml ~/.config/companion-gateway/config.yaml
cp deploy/companion-gateway.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now companion-gateway
journalctl --user -u companion-gateway -f
```

The supplied unit runs `~/.local/bin/companion-gateway`, installed by the editable uv command above. The data directory contains the only state database. Back up `state.sqlite3` with the CLI instead of copying it while writes are active.

One gateway database represents one companion. Run another gateway with a separate configuration, data directory, port, and user service when you need another companion. Databases from schema version 0 are intentionally rejected because this change has no in-place migration. Start with an empty data directory or retain the old database as a backup.

## Limits

Hybrid retrieval depends on the configured embedding model for semantic quality. The current sqlite-vec query scans stored vectors and suits a personal conversation archive. Large indexes may need an approximate nearest-neighbor adapter later.

Evergreen facts can become stale when the agent misses a correction. Expiration and review dates follow deterministic clock checks, while semantic changes require an explicit agent or operator revision.

Deterministic phrase classification cannot infer every emotional nuance. Use explicit event labels for safety-critical fear or relationship events. The service does not generate persona content or decide what the companion believes.
