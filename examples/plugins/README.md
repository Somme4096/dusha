# External plugins

An external plugin lives in a directory under the plugins directory with
`pyproject.toml` and a `[tool.dusha]` table. The gateway imports it into its own
process, so it gets the same `ctx` as a built-in plugin, including `ctx.api` and
`ctx.cli`. The contract is in the
[project README](https://github.com/Somme4096/dusha#plugin-api).

External plugins run with the gateway's permissions and environment. Install only
plugins you wrote or reviewed. They use the gateway's Python environment, so extra
dependencies must be installed there and a version conflict affects the gateway.

Install an external plugin by copying its directory into the configured plugins directory and
naming it under `plugins`:

```sh
mkdir -p ~/.config/dusha/dusha/plugins
cp -R examples/plugins/system_one ~/.config/dusha/dusha/plugins/system_one
cp -R examples/plugins/everos_memory ~/.config/dusha/dusha/plugins/everos_memory
```

```json
{
  "plugins": {
    "system_one": {"options": {"base_url": "http://127.0.0.1:8000"}},
    "everos_memory": {"options": {"everos": {"url": "http://127.0.0.1:8001"}}}
  }
}
```

## Shipped external plugins

- [`system_one`](system_one/README.md) listens for `decide`. It asks a llama.cpp
  server with decision-model support which emotion a user message moves.
- [`everos_memory`](everos_memory/README.md) listens for every event. It mirrors
  messages into an EverOS sidecar, injects recalled episodes into the context,
  and applies phrase deltas through `decide`.

Each external plugin keeps its end-to-end tests in its own `tests` directory. The project's
`uv run pytest` collects them.
