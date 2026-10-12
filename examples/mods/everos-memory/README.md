# EverOS memory mod

This mod mirrors conversations into an [EverOS](https://github.com/EverMind-AI/EverOS)
sidecar and injects the episodes it recalls. Install it as `everos_memory`.
The hook contract is in the project [README](https://github.com/Somme4096/dusha#mod-api).

## Install

```sh
mkdir -p ~/.config/dusha/dusha/mods
cp -R examples/mods/everos-memory ~/.config/dusha/dusha/mods/everos_memory
```

```json
{
  "mods": {
    "modules": {
      "everos_memory": {
        "timeout_seconds": 15,
        "context_max_chars": 3000,
        "options": {
          "everos": {
            "url": "http://127.0.0.1:8000",
            "app_id": "dusha",
            "project_id": "default",
            "instance_namespace": "dusha-local",
            "user_sender_id": "user",
            "assistant_sender_id": "assistant",
            "timeout_seconds": 5,
            "flush_after_ingest": true
          }
        }
      }
    }
  }
}
```

Keep the mod's `timeout_seconds` above `options.everos.timeout_seconds` with
margin for the gateway to handle a timed-out sidecar request. The example uses
15 seconds for the mod and 5 for EverOS.

## Hooks

| Hook | What it does |
| --- | --- |
| `start` | Opens the extension tables and validates the `everos` options. |
| `on_message` | Queues a `user` or `assistant` message in the outbox and tries one delivery. |
| `on_context` | Resolves the conversation to an EverOS session and returns recalled episodes. |
| `decide` | Returns `deltas` from the first matching `phrase_patterns` entry. |
| `tick` | Delivers one pending outbox row or runs one due flush. |
| `status` | Reports outbox and flush counters. |
| `close` | Closes the HTTP client. |

## What the mod owns

The gateway owns message and evergreen storage. The mod owns the EverOS
delivery path and four extension tables in the same database file:
`everos_sessions`, `everos_session_map`, `everos_outbox`, and
`everos_flush_state`. It never creates or migrates core tables.

Each `(harness, external conversation id)` pair maps to one EverOS session.
Only `user` and `assistant` messages with positive millisecond timestamps are
queued, two messages sharing a timestamp get a bumped one, and insertion is
idempotent on the message id. A sidecar outage never fails the hook. The
gateway advances its delivery cursor once `on_message` returns, and `tick`
retries undelivered rows, fewest attempts first, so a permanently rejected row
never blocks later ones.

## Context

`on_context` searches `POST /api/v2/memory/search` with `method: "keyword"` and
an exact session filter, or lists recent episodes when the query is empty. It
returns short source-labeled episodes within `max_chars`. When the mapping,
the sidecar, or the results are absent it returns nothing and the gateway keeps
its own context.

## Flush

`flush_after_ingest` defaults to `false`. On, each ingest sends one deferred
add and `tick` later flushes it through EverOS on a background thread, so a
slow sidecar never blocks the hook. `flush_timeout_seconds` (default 300)
bounds that call. EverOS needs a configured LLM for extraction. `buffered`
counts acknowledged adds and `processed` counts acknowledged flushes.

## Phrase patterns

```json
{
  "phrase_patterns": [
    {"pattern": "I love you", "deltas": {"elation": 0.3, "intimacy": 0.2}},
    {"pattern": "I hate you", "deltas": {"irritability": 0.3, "dejection": 0.2}}
  ]
}
```

Matching is case insensitive. Dimension names come from your `emotions.json`.

## Tests

`tests/` holds the end-to-end suites. They need `EVEROS_EXECUTABLE` pointing at
an EverOS binary and skip without it. `uv run pytest` from the project root
collects them.
