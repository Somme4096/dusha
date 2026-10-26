# Operator guide

Run one `dusha` process per companion and keep its data directory on a persistent disk. A second companion needs its own home and `port`. Serverless workers and ephemeral container disks lose the database on restart.

## Run as a systemd service

These steps name the companion `dusha`. Swap in your own name.

1. Install the CLI. The unit expects the binary at `~/.local/bin/dusha`.

```sh
uv tool install dusha
command -v dusha
```

2. Install the unit. The first start creates the companion home and its `config.json`.

```sh
mkdir -p ~/.config/dusha/dusha ~/.config/systemd/user
curl -o ~/.config/systemd/user/dusha@.service \
  https://raw.githubusercontent.com/Somme4096/dusha/main/deploy/dusha@.service
```

3. Add secrets, if you use an embedding key. The config stores variable names and this file stores the values.

```sh
touch ~/.config/dusha/dusha/.env
chmod 600 ~/.config/dusha/dusha/.env
${EDITOR:-vi} ~/.config/dusha/dusha/.env
```

```ini
UPSTREAM_API_KEY=replace-me
```

The unit sets `PATH` to `~/.local/bin:/usr/local/bin:/usr/bin:/bin`. Add a `PATH=` line to this file when `uv` lives elsewhere.

4. Start the service and check it.

```sh
systemctl --user daemon-reload
systemctl --user enable --now dusha@dusha
systemctl --user is-active dusha@dusha
curl http://127.0.0.1:8765/health
```

`is-active` prints `active` and `/health` returns `"status":"ok"`.

Read the logs:

```sh
journalctl --user -u dusha@dusha --no-pager -n20
```

The unit is a template. `dusha@<name>` serves the home `~/.config/dusha/<name>/`.

## Auth

Auth is off by default, and the service binds to localhost. To turn auth on, put a token in the config:

```json
{
  "api_token": "replace-with-a-long-random-value"
}
```

`openssl rand -hex 32` makes a good one. The config now holds a secret. `dusha serve` writes it with mode `600`, so keep that mode and keep the file out of version control.

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

To reach the service from another device, put a TLS reverse proxy in front of it or stay on a private network. Do not expose the plain HTTP port.

## Backup and restore

```sh
dusha backup /path/to/backups/state-$(date +%F).sqlite3
```

The command runs on the server and writes the backup to that path on the server's machine. It is safe while the service runs. Do not copy `state.sqlite3` by hand during writes.

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
| An external plugin stopped working after an upgrade | External plugins define `apply(ctx, config)` and name it in `[tool.dusha]`. See the [Plugin API](https://github.com/Somme4096/dusha#plugin-api). |
| Duplicate proactive events or write errors | Two processes share one `state.sqlite3`. Stop one. |

## See also

- [configuration.md](configuration.md): every config key.
- [api.md](api.md): HTTP endpoints and integrations.
