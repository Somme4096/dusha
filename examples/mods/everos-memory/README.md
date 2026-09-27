# EverOS memory plugin

The plugin directory identifier is `everos_memory`. Configure it under
`memory_plugin.module`. See the [Sophia project README](https://github.com/Somme4096/sophia)
for public API descriptions.

## Install

Copy the plugin directory into the gateway's configured mods directory.

```sh
mkdir -p ~/.config/companion-gateway/mods
cp -R examples/mods/everos-memory ~/.config/companion-gateway/mods/everos_memory
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

This is a local SQLite-authoritative archival mirror. Sophia search remains
local. Only `user` and `assistant` messages are mirrored. Tool messages are not
mirrored. If the local EverOS server is unavailable, delivery is queued in
SQLite. Messages with valid timestamps before the Unix epoch remain local and
are intentionally not mirrored because EverOS requires positive millisecond
timestamps. Each backfill makes one delivery attempt. The retry queue
prioritizes rows with fewer attempts, so later rows can deliver while a
permanently rejected row remains pending. Both stores contain plaintext and
need separate backups.

Use the [EverOS setup and public API documentation](https://github.com/EverMind-AI/EverOS)
to configure the server. `flush_after_ingest` defaults to `false`. Set it to
`true` to send one deferred add during Sophia ingest, then let the scheduler
flush it through EverOS. EverOS must run with a configured LLM for extraction.
`buffered` acknowledges the deferred add. `processed` acknowledges the flush,
not indexing completion. Sophia search remains local because EverOS exposes no
per-message provenance. Use EverOS keyword search through its [public API](https://github.com/EverMind-AI/EverOS)
to inspect episodes. See the [Sophia README](https://github.com/Somme4096/sophia)
for Sophia API descriptions.

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
