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
        "timeout_seconds": 5
      }
    }
  }
}
```

This is a local SQLite-authoritative archival mirror. Sophia search remains
local. Only `user` and `assistant` messages are mirrored. Tool messages are not
mirrored. If the local EverOS server is unavailable, delivery is queued in
SQLite. Each backfill makes one delivery attempt. Both stores contain plaintext
and need separate backups.

Use the [EverOS setup documentation](https://github.com/EverMind-AI/EverOS) to
configure the server. Running EverOS requires LLM settings, but deferred writes
do not invoke the LLM. This plugin does not make the server credential-free.

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
