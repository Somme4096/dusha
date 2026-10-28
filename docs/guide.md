# Dusha guide

## Run as a systemd service

Replace `<name>` with your companion's name.

1. Install the CLI. The unit expects the binary at `~/.local/bin/dusha`.

```sh
uv tool install dusha
dusha --help
```

2. Install the unit. The first start creates the companion home and its `config.json`.

```sh
mkdir -p ~/.config/dusha/<name> ~/.config/systemd/user
curl -o ~/.config/systemd/user/dusha@.service \
  https://raw.githubusercontent.com/Somme4096/dusha/main/deploy/dusha@.service
```

3. Add secrets, if you use an embedding key. The config stores variable names and this file stores the values.

```sh
touch ~/.config/dusha/<name>/.env
chmod 600 ~/.config/dusha/<name>/.env
${EDITOR:-vi} ~/.config/dusha/<name>/.env
```

```ini
EMBEDDING_API_KEY=replace-me
```

The unit sets `PATH` to `~/.local/bin:/usr/local/bin:/usr/bin:/bin`. Add a `PATH=` line to this file when `uv` lives elsewhere.

4. Start the service and check it.

```sh
systemctl --user daemon-reload
systemctl --user enable --now dusha@<name>
```

Give it a little healthcheck~

```sh
systemctl --user status dusha@<name>
curl http://127.0.0.1:8765/health # `/health` should return ok.
```

The unit is a template. `dusha@<name>` serves the home `~/.config/dusha/<name>/`.

## Auth

Auth is off by default, and the service binds to localhost. To turn auth on, put a token in the config:

```json
{
  "api_token": "replace-with-a-long-random-value"
}
```

`openssl rand -hex 32` makes a good one (I hope you already know it). The config now holds a secret. `dusha serve` writes it with mode `600`, so keep that mode and keep the file out of version control.

Send the same value in the `X-Companion-Token` header:

```sh
curl -X POST http://127.0.0.1:8765/state/v1/context \
  -H 'Content-Type: application/json' \
  -H "X-Companion-Token: replace-with-a-long-random-value" \
  -d '{"query":"Hello"}'
```

The [proxy](api.md#proxy) sends the header for your harnesses, and the other `dusha` commands send it for you. On another machine, the proxy's first run asks for the token and saves it in `proxy.server.token`. On the same machine, both read `api_token` themselves. An [MCP](api.md#mcp) harness reaches the server directly, so put the header in its MCP config.

With auth on:

- Every `/state/v1/*`, `/cli/v1/*`, and `/mcp` route needs the header. A wrong token returns `401`.
- `/health` stays public.
- `/docs`, `/redoc`, and `/openapi.json` are off.
- A new token needs a restart.

To reach the service from another device, put it behind a TLS reverse proxy, use Tailscale or stay on a private network. Do not expose the plain HTTP port. (I know it's common sense, just consider yourself warned)

## Backup and restore

```sh
dusha backup /path/to/backups/state-$(date +%F).sqlite3
```

The command runs on the server and writes the backup to that path on the server's machine. It is safe while the service runs. Do not copy `state.sqlite3` by hand during writes. (Again, common sense.)

Back up your own files too: `config.json`, the identity, emotions, and prompts files, and the plugins directory.

To restore:

1. Stop the service.
2. Place the backup in the data directory as `state.sqlite3`.
3. Start the service.

## Plugins

```sh
dusha plugins
dusha memory reindex
```

`plugins` prints each plugin's `state`, `error`, `inject`, `provides`, and `events`. Each plugin adds its own commands, such as `memory`, `evergreen`, `memo`, `affect`, and `proactive`. `memory reindex` rebuilds the built-in search index. Plugin commands run on the server, so it must be up. `dusha --help` lists them, from the last list it saw while the server is down.

### Plugin API
> [!NOTE]
> This adds detail to the [Plugin](https://github.com/Somme4096/dusha#plugin) table in the README.

`inject` also takes `api`, `cli`, and `mcp` when the plugin registers routes, commands, or tools. 

| Hook | Receives |
| --- | --- |
| `message` | The stored row: `id`, `conversation_id`, `role`, `text`, `content`, `external_id`, `occurred_at`, `ingested_at`, `harness`, `external_conversation_id`, `route`. An exception is logged and the message is not redelivered. |
| `context` | `query`, `harness`, `conversation_id`, `exclude_message_ids`, and `include_recent`. A returned `Block` carries `name`, `text`, `data`, `session` records, and an `instruction` for the state block. |
| `decide` | `message`, `emotions` (the resolved dimensions), `state` (the affect snapshot), and `instruction`. |
| `<event>` | The arguments passed to `emit` or `collect`. Custom events stay in the gateway process, so they reach every active plugin, including the emitter's own listeners. |

`ctx.data_dir` is `plugins/<plugin>` under `data_dir`. `ctx.collect` keeps each plugin's first non-null result.

| Service | Methods |
| --- | --- |
| `messages` | `get`, `recent`, `search`, `context`, `after` |
| `companion` | `ingest_message`, `ingest_transcript`, `build_context`, `latest_route` |
| `affect` | `status` |
| `evergreen` | `remember`, `revise`, `forget`, `list_current`, `history` |
| `memo` | `add`, `list`, `done` |
| `proactive` | `evaluate`, `poll`, `acknowledge` |

Services from `ctx.provide` are plain Python objects. `deltas` apply after the increment, and dimension names come from your `emotions.json`.
One thing to pay attention to is that a dependency conflict in plugin(whether internal or external) makes dusha unable to start gateway, since they share one Python environment.

## Migrating

### prompts.json schema 3 to 4

Since 1.1.21, prompt text lives under `plugins`, keyed by the plugin that reads it. A schema 3 overlay still loads with a startup warning until 2.0.21. Only an overlay set by `prompts.path` needs this, the packaged text moves on its own.

| Schema 3 | Schema 4 |
| --- | --- |
| `affect_presentation` | `plugins.affect.presentation` |
| `affect` | `plugins.affect.block` |
| `decision_instruction` | `plugins.affect.decision_instruction` |
| `proactive_framing` | `plugins.proactive.framing` |
| `proactive_generation_instruction` | `plugins.proactive.generation_instruction` |
| `evergreen` | `plugins.evergreen` |
| `context_blocks.memo_open_delimiter`, `context_blocks.memo_close_delimiter` | `plugins.memo.open_delimiter`, `plugins.memo.close_delimiter` |
| `context_blocks.plugin_open_delimiter`, `context_blocks.plugin_close_delimiter` | Removed. Nothing read them. |
| `companion_state.memory_instruction` | `plugins.memory.instruction` |

`companion_state` keeps `open_delimiter` and `close_delimiter`.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Startup fails on a missing config file | The selected home needs a `config.json`. Run `dusha list` to see your companions. |
| Startup fails on a missing identity file | Fix `plugins.identity.path` or remove the key. The file must be UTF-8. |
| Startup fails on an empty `plugins.identity.path` | Remove the key to use `identity.md`, or set `plugins.identity.disabled` to `true`. |
| Startup fails on an unknown `features`, `memory`, `evergreen`, `memo`, `affect`, `proactive`, `emotions`, or `identity_prompt` field, or on a section from an earlier release | Move the section under [`plugins`](configuration.md#plugins). A `features` entry set to `false` becomes `"disabled": true`, entries of the earlier section become external plugins, its directory key becomes `plugins_dir`, `emotions` moves to `plugins.affect.emotions`, and `identity_prompt` becomes `plugins.identity`. |
| Startup fails on an unknown `api_token_env` field | Rename it to `api_token` and put the token value there. |
| Startup fails on an unknown `memory_plugin` or `decision` field | Move the external plugin under [`plugins`](configuration.md#external-plugins) and `decision.increment` to `plugins.affect.increment`. |
| Requests return `401` | Send `X-Companion-Token` with the token value. |
| A command fails with `cannot reach the server` | Start `dusha serve <name>`, or check `proxy.server.url` and `port` in the companion's `config.json`. |
| A plugin's command ends in `No such command` | The plugin is disabled or inactive, see `dusha plugins`. |
| `auth login` fails with `cannot load the models.dev catalog` | The machine is offline and has no cached catalog. Connect once, or add the profile to `proxy.upstreams` by hand. |
| The provider rejects the harness's key | With a `key` in the profile, the proxy sends that key instead. Check it with `dusha proxy <name> auth status` and log in again. |
| `dusha proxy <name>` asks for the server again | That name has no saved `proxy.server.url`. Pick the server once and it is saved. Check the name with `dusha list`. |
| Startup fails on `upstream` or `api_openai` moved to `proxy.upstreams` | Delete them and add a [profile](configuration.md#proxy) under `proxy.upstreams` instead. Clients that used `/v1/...` on the server now use `http://127.0.0.1:8766/<profile>`. |
| The proxy returns `502` | The provider is unreachable or timed out. Read the `detail`, then check the profile's `base_url`. |
| The proxy forwards but nothing is stored | `GET /health` on the proxy shows `server.status`. Check `proxy.server.url` and `proxy.server.token`. |
| Memory, fact, memo, affect, or proactive routes return `503` | The `detail` names the plugin. Remove its `"disabled": true`, or turn on the plugin it needs, and restart. Your stored rows are intact. |
| Startup fails on an external plugin import or `apply` error | The traceback names the plugin directory and the failing module. Check that directory, its `[tool.dusha]` table in `pyproject.toml`, and the traceback. |
| An external plugin stopped working after an upgrade | External plugins define `apply(ctx, config)` and name it in `[tool.dusha]`. See the [Plugin API](#plugin-api). |
| Duplicate proactive events or write errors | Two processes share one `state.sqlite3`. Stop one. |

## See also

- [configuration.md](configuration.md): every config key.
- [api.md](api.md): HTTP endpoints and integrations.
