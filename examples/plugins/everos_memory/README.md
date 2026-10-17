# EverOS memory external plugin

This external plugin mirrors conversations into an [EverOS](https://github.com/EverMind-AI/EverOS)
sidecar and injects the episodes it recalls. Install it as `everos_memory`.
The external plugin contract is in the project [README](https://github.com/Somme4096/dusha#plugin-api).

## Install

```sh
mkdir -p ~/.config/dusha/dusha/plugins
cp -R examples/plugins/everos_memory ~/.config/dusha/dusha/plugins/everos_memory
```

```json
{
  "plugins": {
    "everos_memory": {
      "timeout_seconds": 15,
      "context_max_chars": 3000,
      "options": {
        "interval_seconds": 10,
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
```

Keep the external plugin's `timeout_seconds` above `options.everos.timeout_seconds` with
margin for the gateway to handle a timed-out sidecar request. The example uses
15 seconds for the external plugin and 5 for EverOS.

## What `apply` registers

| Registration | What it does |
| --- | --- |
| `apply` | Opens the external plugin's database and validates the `everos` options. |
| `message` | Queues a `user` or `assistant` message in the outbox and tries one delivery. |
| `context` | Resolves the conversation to an EverOS session and returns recalled episodes. |
| `decide` | Returns `deltas` from the first matching `phrase_patterns` entry. |
| interval | Every `interval_seconds` (default `10`), delivers one pending outbox row or runs one due flush. |
| dispose | Closes the HTTP client. |

## What the external plugin owns

The gateway owns message and evergreen storage. The external plugin owns the EverOS
delivery path and four tables in its own database,
`plugins/everos_memory/everos.sqlite3` under the gateway's `data_dir`:
`everos_sessions`, `everos_session_map`, `everos_outbox`, and
`everos_flush_state`. It never touches the gateway's database. EverOS keeps
its own registry, so a fresh external plugin database needs no migration.

Each `(harness, external conversation id)` pair maps to one EverOS session.
Only `user` and `assistant` messages with positive millisecond timestamps are
queued, two messages sharing a timestamp get a bumped one, and insertion is
idempotent on the message id. A sidecar outage never fails the listener. The
gateway advances its delivery cursor once the `message` listener returns, and
the interval retries undelivered rows, fewest attempts first, so a permanently
rejected row never blocks later ones.

## Context

The `context` listener searches `POST /api/v2/memory/search` with `method: "keyword"` and
an exact session filter, or lists recent episodes when the query is empty. It
returns short source-labeled episodes within `max_chars`. When the mapping,
the sidecar, or the results are absent it returns nothing and the gateway keeps
its own context.

## Flush

`flush_after_ingest` defaults to `false`. On, each ingest sends one deferred
add and the interval later flushes it through EverOS on a background thread, so a
slow sidecar never blocks the listener. `flush_timeout_seconds` (default 300)
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

`tests/` holds the end-to-end suites. All but the sidecar-stub test need
`EVEROS_EXECUTABLE` pointing at an EverOS binary and skip without it.
`uv run pytest` from the project root collects them.
