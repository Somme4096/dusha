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

3. Add secrets, if you use an upstream key or an API token. The config stores variable names and this file stores the values.

```sh
touch ~/.config/dusha/dusha/environment
chmod 600 ~/.config/dusha/dusha/environment
${EDITOR:-vi} ~/.config/dusha/dusha/environment
```

```ini
UPSTREAM_API_KEY=replace-me
COMPANION_TOKEN=replace-with-a-long-random-value
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

Auth is off by default, and the service binds to localhost. To turn auth on, name an environment variable in the config:

```json
{
  "api_token_env": "COMPANION_TOKEN"
}
```

Put the token in that variable, then start the service:

```sh
export COMPANION_TOKEN="$(openssl rand -hex 32)"
dusha serve dusha
```

Send the same value in the `X-Companion-Token` header:

```sh
curl -X POST http://127.0.0.1:8765/state/v1/context \
  -H 'Content-Type: application/json' \
  -H "X-Companion-Token: $COMPANION_TOKEN" \
  -d '{"query":"Hello"}'
```

With auth on:

- Every `/state/v1/*` and `/v1/*` route needs the header. A wrong token returns `401`.
- `/health` stays public.
- `/docs`, `/redoc`, and `/openapi.json` are off.
- Startup fails when the variable is unset or empty.
- A new token needs a restart.

To reach the service from another device, put a TLS reverse proxy in front of it or stay on a private network. Do not expose the plain HTTP port.

## Backup and restore

```sh
dusha backup /path/to/backups/state-$(date +%F).sqlite3
```

The command is safe while the service runs. Do not copy `state.sqlite3` by hand during writes.

Back up your own files too: `config.json`, the identity, emotions, and prompts files, and the mods directory.

To restore:

1. Stop the service.
2. Place the backup in the data directory as `state.sqlite3`.
3. Start the service.

## Memory index

```sh
dusha memory index status
dusha memory index backfill --limit 256
dusha memory index rebuild
```

`status` and `backfill` talk to the memory plugin when you run one. `rebuild` also rebuilds the built-in search index.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Startup fails on a missing config file | The selected home needs a `config.json`. Run `dusha list` to see your companions. |
| Startup fails on a missing identity file | Fix `identity_prompt.path` or clear it. The file must be UTF-8. |
| Startup fails on the API token | Export the variable `api_token_env` names, or clear the key to turn auth off. |
| Requests return `401` | Send `X-Companion-Token` with the token value. |
| Message, memory, context, fact, and memo routes return `503` | Set `storage.enabled` back to `true` and restart. Your stored rows are intact. |
| The memory plugin adds no context | Check the log for `failed to initialize memory plugin` or `memory plugin context injection failed`, then check `memory_plugin.module` and the mods directory. |
| Duplicate proactive events or write errors | Two processes share one `state.sqlite3`. Stop one. |

## See also

- [configuration.md](configuration.md): every config key.
- [api.md](api.md): HTTP endpoints and integrations.
