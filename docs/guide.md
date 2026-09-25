# Companion State Gateway operator guide

Run one gateway process per SQLite file. Install the CLI, then set up the systemd user service, auth, backup, and troubleshooting below.

## Run a durable single-instance service

Run one process against one `state.sqlite3` file. SQLite WAL allows a single writer, and the in-process schedulers assume one evaluator. Two processes on the same file race writes and duplicate scheduler events. Run one process per database. A second companion needs its own config, data directory, port, and service unit.

Keep `state.sqlite3` and its `-wal` and `-shm` files on a persistent volume. A container scratch disk or an ephemeral instance disk drops all memory, affect, facts, and proactive events on restart.

Set up a `systemd --user` service. This takes about 15 minutes.

1. Install the CLI. The shipped unit runs `%h/.local/bin/companion-gateway`, so confirm the install puts the binary there.

```sh
uv tool install --editable .
command -v companion-gateway
```

Success check: `command -v` prints a path ending in `/companion-gateway`.

2. Copy the example config and the shipped unit.

```sh
mkdir -p ~/.config/companion-gateway ~/.config/systemd/user
cp config.example.json ~/.config/companion-gateway/config.json
cp deploy/companion-gateway.service ~/.config/systemd/user/
```

3. Reload the user daemon, then override the unit's config path. The shipped unit points `COMPANION_GATEWAY_CONFIG` at `config.yaml`, which does not exist in this setup. Do not edit `deploy/companion-gateway.service`; add a drop-in instead.

```sh
systemctl --user daemon-reload
systemctl --user edit companion-gateway
```

Add this drop-in and save:

```ini
[Service]
Environment=COMPANION_GATEWAY_CONFIG=%h/.config/companion-gateway/config.json
```

The drop-in loads after the unit, so its `Environment=` assignment wins and the service reads `config.json`.

4. Prepare the optional secrets file. Skip this step when you use no upstream key, no embedding key, and no companion token. The `touch` command keeps an existing file's contents.

```sh
touch ~/.config/companion-gateway/environment
chmod 600 ~/.config/companion-gateway/environment
${EDITOR:-vi} ~/.config/companion-gateway/environment
```

The file holds only the keys you need. Config stores variable names, never values.

```ini
UPSTREAM_API_KEY=replace-me
EMBEDDING_API_KEY=replace-me
COMPANION_TOKEN=replace-with-a-long-random-value
```

5. Reload, start, and check health.

```sh
systemctl --user daemon-reload
systemctl --user enable --now companion-gateway
systemctl --user is-active companion-gateway
curl http://127.0.0.1:8765/health
```

Success check: `is-active` prints `active` and `/health` returns `"status":"ok"`.

Read recent logs without blocking:

```sh
journalctl --user -u companion-gateway --no-pager -n20
```

## Auth and network

The default bind stays on localhost. To reach the service from another device, terminate TLS at a reverse proxy or keep the service on a private network. Do not expose the plaintext HTTP port.

Set `api_token_env` to the name of an environment variable holding a long random token. An empty `api_token_env` keeps local unauthenticated mode. When it names a variable, the service resolves that variable once at startup and requires every data and model request to send the same value in `X-Companion-Token`. Export the variable before you start the service; changing its value requires a restart.

```json
{
  "api_token_env": "COMPANION_TOKEN"
}
```

```sh
export COMPANION_TOKEN="$(openssl rand -hex 32)"
companion-gateway serve
```

In another terminal, set `COMPANION_TOKEN` to the same value (do not generate a new one), then test:

```sh
curl -X POST http://127.0.0.1:8765/state/v1/context \
  -H 'Content-Type: application/json' \
  -H "X-Companion-Token: $COMPANION_TOKEN" \
  -d '{"query":"Hello"}'
```

Success check: the response is `200` with a `context` object, not `401`.

Auth covers every `/state/v1/*` route plus `/v1/models` and `/v1/chat/completions`. A missing or wrong token returns `401 {"detail":"invalid companion token"}`. `/health` stays public and returns only process, database, and index status; it never carries the token or conversation data. The interactive docs (`/docs`, `/redoc`) and `/openapi.json` are disabled while auth is on, to keep the public surface minimal.

A configured `api_token_env` whose environment variable is missing or empty fails startup with a clear error. It never falls back to unauthenticated mode. Keep the API on a trusted network while auth is disabled.

The proxy uses a server-owned key when `upstream.api_key_env` is nonempty. It sends that value as a bearer token and drops the caller's `Authorization` header. Otherwise it forwards the caller's `Authorization` header. The companion token only authorizes the caller; it never replaces the upstream credential.

## Backup, restore, and troubleshoot

Back up every user-authored file: the config file, the identity Markdown, custom prompts and emotions files, decision plugin files under the mods directory, and the database.

Create a consistent live backup:

```sh
companion-gateway backup /path/to/backups/state-$(date +%F).sqlite3
```

The command uses the SQLite online backup API and stays safe while the service writes. Do not copy `state.sqlite3` during active writes.

Restore:

1. Stop the service.
2. Place the backup database file in `data_dir`.
3. Start the service.

The service upgrades a schema version 2 or 3 database to version 4 in place and rejects older schemas. The upgrade archives the retired label tables as `legacy_affect_events` and `legacy_affect_classifications` and preserves affect state. Keep a current backup before any upgrade.

Common problems:

- Startup fails with a missing config file: `--config` and `COMPANION_GATEWAY_CONFIG` treat a missing file as an error. Fix the path or drop the explicit setting.
- Startup fails with a missing identity file: the configured `identity_prompt.path` does not exist or is not valid UTF-8. Correct the file or clear the path.
- Hybrid search returns lexical results: the embedding endpoint is unreachable or misconfigured. Check `memory.embedding.base_url`, the model, and the key variable. `memory index status` reports `cooling_down` and `last_error`.
- Auth fails at startup or on request: `api_token_env` names an unset or empty variable (startup error), or a data or model request lacks a matching `X-Companion-Token` header (401). Export the variable, or clear `api_token_env` only when you intend to disable auth.
- A second process causes races or duplicate events: two processes share one `state.sqlite3`. Run one process per database.

## Deployment limits

The supported model is a single local process with one SQLite file on a persistent volume. FTS5 and the sqlite-vec extension run in-process. No hosted runtime has been tested, and cloud portability is unverified. Serverless workers and ephemeral container disks lose SQLite state on sleep or restart. Treat a persistent-volume host as the baseline.

## Related documentation

- [configuration.md](configuration.md): config file, identity and emotion files, retrieval, embeddings, proactive scheduling.
- [api.md](api.md): HTTP endpoints and schemas.
- [README.md](../README.md): project overview and quickstart.
- [third-party-notices.md](../third-party-notices.md): license terms.
