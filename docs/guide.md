# Dusha operator guide

Run one gateway process per SQLite file. Install the CLI, then set up the systemd user service, auth, backup, and troubleshooting below.

## Run a durable single-instance service

Run one process against one `state.sqlite3` file. SQLite WAL allows a single writer, and the in-process schedulers assume one evaluator. Two processes on the same file race writes and duplicate scheduler events. Run one process per database. A second companion needs its own home and port.

Keep `state.sqlite3` and its `-wal` and `-shm` files on a persistent volume. A container scratch disk or an ephemeral instance disk drops all memory, affect, facts, and proactive events on restart.

Set up a `systemd --user` service. This takes about 15 minutes.

1. Install the CLI. The shipped unit runs `%h/.local/bin/dusha`, so confirm the install puts the binary there.

```sh
uv tool install --editable .
command -v dusha
```

Success check: `command -v` prints a path ending in `/dusha`.

2. Create a companion home, copy the example config into it, and install the shipped template unit. These steps name the companion `dusha`.

```sh
mkdir -p ~/.config/dusha/dusha ~/.config/systemd/user
cp config.example.json ~/.config/dusha/dusha/config.json
cp deploy/dusha@.service ~/.config/systemd/user/
```

3. Reload the user daemon so systemd picks up the unit.

```sh
systemctl --user daemon-reload
```

The unit is a template. `dusha@dusha` runs `dusha serve dusha` and reads the home `~/.config/dusha/dusha/`. A second companion needs a second home with its own `port`, then `dusha@<name>`.

4. Prepare the optional secrets file. Skip this step when you use no upstream key and no companion token. The `touch` command keeps an existing file's contents.

```sh
touch ~/.config/dusha/dusha/environment
chmod 600 ~/.config/dusha/dusha/environment
${EDITOR:-vi} ~/.config/dusha/dusha/environment
```

The file holds only the keys you need. Config stores variable names, never values.

```ini
UPSTREAM_API_KEY=replace-me
COMPANION_TOKEN=replace-with-a-long-random-value
```

5. Reload, start, and check health.

```sh
systemctl --user daemon-reload
systemctl --user enable --now dusha@dusha
systemctl --user is-active dusha@dusha
curl http://127.0.0.1:8765/health
```

Success check: `is-active` prints `active` and `/health` returns `"status":"ok"`.

Read recent logs without blocking:

```sh
journalctl --user -u dusha@dusha --no-pager -n20
```

## Auth and network

The default bind stays on localhost. To reach the service from another device, terminate TLS at a reverse proxy or keep the service on a private network. Do not expose the plaintext HTTP port.

Set `api_token_env` to the name of an environment variable holding a long random token. An empty `api_token_env` keeps local unauthenticated mode. When it names a variable, the service resolves that variable once at startup and requires every data and model request to send the same value in `X-Companion-Token`. Export the variable before you start the service. A new value needs a restart.

```json
{
  "api_token_env": "COMPANION_TOKEN"
}
```

```sh
export COMPANION_TOKEN="$(openssl rand -hex 32)"
dusha serve dusha
```

In another terminal, set `COMPANION_TOKEN` to the same value (do not generate a new one), then test:

```sh
curl -X POST http://127.0.0.1:8765/state/v1/context \
  -H 'Content-Type: application/json' \
  -H "X-Companion-Token: $COMPANION_TOKEN" \
  -d '{"query":"Hello"}'
```

Success check: the response is `200` with a `context` object, not `401`.

Auth covers every `/state/v1/*` route plus `/v1/models` and `/v1/chat/completions`. A missing or wrong token returns `401 {"detail":"invalid companion token"}`. `/health` stays public and returns process, database, and index status. It carries no token and no conversation data. The interactive docs (`/docs`, `/redoc`) and `/openapi.json` are disabled while auth is on, to keep the public surface minimal.

A configured `api_token_env` whose environment variable is missing or empty fails startup with a clear error. It never falls back to unauthenticated mode. Keep the API on a trusted network while auth is disabled.

The proxy uses a server-owned key when `upstream.api_key_env` is nonempty. It sends that value as a bearer token and drops the caller's `Authorization` header. Otherwise it forwards the caller's `Authorization` header. The companion token authorizes the caller to the gateway. The upstream credential stays separate.

## Backup, restore, and troubleshoot

Back up every user-authored file: the config file, the identity Markdown, custom prompts and emotions files, plugin directories under the mods directory, and the database.

Create a consistent live backup:

```sh
dusha backup /path/to/backups/state-$(date +%F).sqlite3
```

The command uses the SQLite online backup API and stays safe while the service writes. Do not copy `state.sqlite3` during active writes.

Restore:

1. Stop the service.
2. Place the backup database file in `data_dir`.
3. Start the service.

The service upgrades a schema version 2, 3, or 4 database to version 5 in place and rejects other versions. An upgrade from version 2 or 3 archives the retired label tables as `legacy_affect_events` and `legacy_affect_classifications` and preserves affect state. Keep a current backup before any upgrade.

Common problems:

- Startup fails with a missing config file: a selected companion home must hold `config.json`. Run `dusha list` to see the companions, then fix the name or the path.
- Startup fails with a missing identity file: the configured `identity_prompt.path` does not exist or is not valid UTF-8. Correct the file or clear the path.
- A memory plugin adds no context: the plugin is missing, failed to load, or returned an empty `inject_context`. Check the plugin log for `memory plugin context injection failed` or `failed to initialize memory plugin`, then confirm `memory_plugin.module` and the mods directory. Core recent and evergreen memory still works.
- Message, memory, context, fact, and memo routes return `503`: `storage.enabled` is `false`. Set it back to `true` and restart. The stored rows were never deleted.
- Auth fails at startup or on request: `api_token_env` names an unset or empty variable (startup error), or a data or model request lacks a matching `X-Companion-Token` header (401). Export the variable, or clear `api_token_env` only when you intend to disable auth.
- A second process causes races or duplicate events: two processes share one `state.sqlite3`. Run one process per database.

## Deployment limits

The supported model is a single local process with one SQLite file on a persistent volume, with SQLite FTS5 running in-process. A memory plugin runs as a child process with its own `uv` environment. No hosted runtime has been tested, and cloud portability is unverified. Serverless workers and ephemeral container disks lose SQLite state on sleep or restart. Treat a persistent-volume host as the baseline.

## Related documentation

- [configuration.md](configuration.md): config file, identity and emotion files, storage, retrieval, and proactive scheduling.
- [api.md](api.md): HTTP endpoints and schemas.
- [README.md](../README.md): project overview and quickstart.
- [third-party-notices.md](../third-party-notices.md): license terms.
