# SQLite memory plugin

The plugin directory identifier is `sqlite_memory`. Configure it under
`memory_plugin.module`. It exports the memory operations used by the gateway.

The gateway supplies the request fields described by
[`memory_plugin.py`](https://github.com/Somme4096/sophia/blob/main/src/companion_gateway/memory_plugin.py).
Each operation follows `fn(request, options)`. See the
[project README](https://github.com/Somme4096/sophia) for the public API.

## Install

Copy the plugin directory into the gateway's configured mods directory.

```sh
mkdir -p ~/.config/companion-gateway/mods
cp -R examples/mods/sqlite-memory ~/.config/companion-gateway/mods/sqlite_memory
```

The gateway creates an isolated `uv` environment from `pyproject.toml` and
`uv.lock` when it loads the plugin.

```json
{
  "memory_plugin": {
    "module": "sqlite_memory",
    "mods_dir": "mods",
    "timeout_seconds": 15,
    "options": {}
  }
}
```

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
