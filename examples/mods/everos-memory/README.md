# EverOS memory plugin

The plugin directory identifier is `everos_memory`. Configure it under
`memory_plugin.module`. See the [Sophia project README](https://github.com/Somme4096/sophia)
for public API descriptions.

## Install

Copy the plugin directory into the gateway's configured mods directory.

```sh
mkdir -p ~/.config/dusha/sophia/mods
cp -R examples/mods/everos-memory ~/.config/dusha/sophia/mods/everos_memory
```

The gateway creates an isolated `uv` environment from `pyproject.toml` and
`uv.lock` when it loads the plugin.

```json
{
  "memory_plugin": {
    "module": "everos_memory",
    "mods_dir": "mods",
    "timeout_seconds": 15,
    "options": {
      "everos": {
        "url": "http://127.0.0.1:8000",
        "app_id": "sophia",
        "project_id": "default",
        "instance_namespace": "sophia-local",
        "user_sender_id": "user",
        "assistant_sender_id": "assistant",
        "timeout_seconds": 5,
        "flush_after_ingest": true
      }
    }
  }
}
```

Set `memory_plugin.timeout_seconds` above `options.everos.timeout_seconds` with
enough margin for the gateway to handle a timed-out sidecar request. The
example uses 15 seconds for the plugin and 5 seconds for EverOS.

## What the plugin owns

The gateway owns message and evergreen storage in its built-in SQLite store.
The plugin owns only the EverOS sidecar delivery path and four extension
tables in the same database file: `everos_sessions`, `everos_session_map`,
`everos_outbox`, and `everos_flush_state`. It never creates or migrates core
message, fact, affect, or proactive tables.

The gateway calls `ingest_messages` with bounded batches of committed user and
assistant messages. The plugin maps each `(harness, external conversation id)`
pair to an EverOS session, inserts the eligible rows into `everos_outbox`, and
returns the highest received message id. Insertion is idempotent through the
unique `message_id` column, so a replay from zero deduplicates pending and
delivered rows while old pending rows still drain. Messages keep their core
ids. Only `user` and `assistant` messages with positive millisecond timestamps
are queued, and two messages that share a timestamp get a bumped one.

A sidecar outage never fails the plugin acknowledgement. The gateway acks the
batch after the outbox commit, and the scheduler retries delivery later. Each
backfill makes one delivery or flush attempt. The retry queue prioritizes rows
with fewer attempts, so later rows can deliver while a permanently rejected row
remains pending.

## Context injection

The gateway calls `inject_context` with a query, a scope, a character budget,
and the harness plus external conversation id. The plugin resolves the exact
`(harness, conversation id)` pair to an EverOS session, then searches
`POST /api/v2/memory/search` with `method: "keyword"` and an exact session
filter. It returns short source-labeled derived episodes with their episode id
and timestamp when EverOS provides them. It ignores the unbounded
`unprocessed_messages` list and never invents message ids. When the mapping,
the sidecar, or the keyword results are absent, the plugin returns empty
context and the gateway keeps its core recent and evergreen context.

## Flush behavior

`flush_after_ingest` defaults to `false`. Set it to `true` to send one deferred
add during Sophia ingest, then let the scheduler flush it through EverOS.
The flush runs on a background thread so a slow sidecar never blocks the
plugin RPC; completion lands in `everos_flush_state` and surfaces through
`status`. At most one flush runs per session at a time.
`flush_timeout_seconds` (default 300) bounds the background flush HTTP call
independently of the RPC `timeout_seconds`, so slow LLM extraction can finish.
EverOS must run with a configured LLM for extraction. `buffered` acknowledges
the deferred add. `processed` acknowledges the flush, not indexing completion.
Use the [EverOS setup and public API documentation](https://github.com/EverMind-AI/EverOS)
to configure the server and inspect episodes.

## Phrase patterns

Configure `phrase_patterns` in the memory plugin options to apply affect deltas
when an incoming user message contains a configured phrase. Matching is case
insensitive.

```json
{
  "memory_plugin": {
    "options": {
      "phrase_patterns": [
        {"pattern": "I love you", "deltas": {"elation": 0.3, "intimacy": 0.2}},
        {"pattern": "I hate you", "deltas": {"irritability": 0.3, "dejection": 0.2}}
      ]
    }
  }
}
```
