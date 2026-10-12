# Mods

A mod is a directory under the mods directory holding `README.md`, `main.py`,
`pyproject.toml`, and `uv.lock`. The gateway creates an isolated `uv`
environment from the lock file, starts `main.py` in a long-lived child process,
and calls the hooks it exports. The hook contract is in the
[project README](https://github.com/Somme4096/dusha#mod-api).

Mods run with your OS permissions and no sandbox. They see only the environment
variables listed in `env_passthrough`, so a mod must take its credentials from
`options`.

Install a mod by copying its directory into the configured mods directory and
naming it under `mods.modules`:

```sh
mkdir -p ~/.config/dusha/dusha/mods
cp -R examples/mods/system_one ~/.config/dusha/dusha/mods/system_one
cp -R examples/mods/everos-memory ~/.config/dusha/dusha/mods/everos_memory
```

```json
{
  "mods": {
    "modules": {
      "system_one": {"options": {"base_url": "http://127.0.0.1:8000"}},
      "everos_memory": {"options": {"everos": {"url": "http://127.0.0.1:8001"}}}
    }
  }
}
```

## Shipped mods

- [`system_one`](system_one/README.md) exports `decide`. It asks a llama.cpp
  server with decision-model support which emotion a user message moves.
- [`everos_memory`](everos-memory/README.md) exports every hook. It mirrors
  messages into an EverOS sidecar, injects recalled episodes into the context,
  and applies phrase deltas through `decide`.

Each mod keeps its end-to-end tests in its own `tests` directory. The project's
`uv run pytest` collects them.
