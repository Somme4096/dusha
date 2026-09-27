# SQLite memory plugin

The SQLite memory plugin is a memory-plugin entry point for the companion
gateway. It follows the same convention as decision plugins. The plugin exports
message, evergreen, index, and affect operations.

The gateway supplies the request fields described by
[`memory_plugin.py`](https://github.com/Somme4096/sophia/blob/main/src/companion_gateway/memory_plugin.py).
Each operation
receives an options dictionary and returns the corresponding result mapping.

## Install

Copy the plugin directory into the gateway's configured mods directory.

```sh
mkdir -p ~/.config/companion-gateway/mods
cp -R examples/mods/sqlite-memory ~/.config/companion-gateway/mods/sqlite-memory
```

The gateway creates an isolated `uv` environment from `pyproject.toml` and
`uv.lock` when it loads the plugin.

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
