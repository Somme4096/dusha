# Companion State Gateway operator guide

This guide runs from a fresh checkout to a live companion service: configuration, identity and emotion files, retrieval, deployment, migration, backup, and troubleshooting. Endpoint details live in [docs/api.md](api.md). The project overview lives in [readme.md](../readme.md). License terms live in [third-party-notices.md](../third-party-notices.md).

## Set up configuration

The service reads JSON first. `config.json` is the supported format. Legacy `config.yaml` still loads with a deprecation warning.

```sh
cp config.example.json config.json
```

`config.example.json` at the repository root is the complete, authoritative example. Packaged defaults live in `defaults.json` and `emotions.json` inside the installed package. The runtime reads those files directly, so Python duplicates no default value. Treat the example and the packaged JSON as the full field list. This guide covers the settings that change behavior.

Config lookup order:

1. `--config PATH`. A missing explicit file is an error.
2. `COMPANION_GATEWAY_CONFIG`. A missing file is an error.
3. `config.json` in the current directory.
4. `config.yaml` in the current directory (legacy, warning).
5. Packaged defaults. A missing implicit file is valid.

`config.json` wins when both files exist. The loader never merges them.

Parsing is strict. Duplicate keys, unknown fields, `NaN`, `Infinity`, JSONC comments, and wrong types raise an actionable error.

Path rules differ by format:

- JSON: `data_dir`, `emotions.path`, `identity_prompt.path`, and `prompts.path` resolve against the config file directory.
- Legacy YAML: `data_dir` resolves against the current working directory. The path sections resolve against the config file.
- Absolute paths pass through unchanged.

Core settings:

- `host`, `port`: bind address and port. Defaults `127.0.0.1` and `8765`.
- `data_dir`: directory holding `state.sqlite3`.
- `timezone`: local zone for quiet hours and daily limits.
- `api_token_env`: name of the environment variable holding the state API token. Empty disables state auth.
- `upstream`: `base_url`, `api_key_env` (default `UPSTREAM_API_KEY`), `timeout_seconds`. An empty `base_url` disables the chat proxy.

Focused example:

```json
{
  "data_dir": "~/.local/share/companion-gateway",
  "host": "127.0.0.1",
  "port": 8765,
  "timezone": "Asia/Taipei",
  "api_token_env": "COMPANION_TOKEN",
  "upstream": {
    "base_url": "https://api.openai.com/v1",
    "api_key_env": "UPSTREAM_API_KEY",
    "timeout_seconds": 120
  }
}
```

## Choose identity, emotions, and prompts

The gateway stores and serves state. It does not author, infer, or archive a persona. Configure no identity file and the context carries none. The service does not invent one.

Identity is raw Markdown you write. Point `identity_prompt.path` at it:

```json
{
  "identity_prompt": {
    "path": "identity.md"
  }
}
```

The service loads the file once at startup and prepends it verbatim to every context. A configured but missing file, or invalid UTF-8, fails startup. A blank file is allowed. The identity does not change at runtime.

Emotions use the current 16-dimension engine. Every dimension name, `neutral`, `floor`, `tau`, label delta, silence rate, and threshold lives in the packaged `emotions.json`. The engine stays deterministic and calls no model. Affect knobs (`mood_follow_hours`, `mood_return_hours`, `habituation_window_minutes`, `habituation_factor`, the silence rates, and `classification_fallback_seconds`) and the proactive emotional thresholds (`longing_threshold`, `fear_threshold`) also live there and accept config overrides.

Export the packaged files, edit the copies, and reference them:

```sh
python -c "import json, companion_gateway.emotions as e; json.dump(e.default_emotions(), open('emotions.json','w'), ensure_ascii=False, indent=2)"
python -c "import json, companion_gateway.prompts as p; d=p.default_prompts(); d.pop('schema_version'); d.pop('prompts_version'); json.dump(d, open('prompts.json','w'), ensure_ascii=False, indent=2)"
```

The emotions command writes the full snapshot, version fields included. The prompts overlay accepts text slots and rejects version metadata, so the prompts command drops `schema_version` and `prompts_version` before writing.

Reference the copies:

```json
{
  "emotions": {"path": "emotions.json", "expected_version": "0.1.0"},
  "prompts": {"path": "prompts.json"}
}
```

`emotions.expected_version` pins the file's `emotion_version`. A mismatch fails startup. Omit it to accept any valid file.

Prompts replace whole text slots. A provided slot must carry every key in that slot. A missing key fails validation. Omitted slots inherit the packaged text. Blank strings are valid replacements. The four slots are `affect_presentation`, `companion_state`, `evergreen`, and `proactive_generation_instruction`.

Value precedence resolves in this order:

1. Packaged `emotions.json`.
2. A custom file from `emotions.path`.
3. Explicit config overrides.

An explicit override wins even when it equals the packaged default. Config fields carry an `UNSET` sentinel, so resolution never guesses the source. The engine snapshots these values at construction. Mutating the config object later changes nothing.

## Retrieval and embeddings

`memory.retrieval_mode` selects the search path:

- `lexical`: SQLite FTS5 only. No text leaves the host.
- `hybrid`: FTS5 plus vector search. The service sends message chunks to your embedding endpoint.

Hybrid needs an OpenAI-compatible embedding endpoint. The service appends `/embeddings` to `base_url` and reads the key from `api_key_env`. It does not store that key in SQLite.

```json
{
  "memory": {
    "retrieval_mode": "hybrid",
    "embedding": {
      "base_url": "http://127.0.0.1:11434/v1",
      "api_key_env": "EMBEDDING_API_KEY",
      "model": "your-embedding-model",
      "dimensions": null
    }
  }
}
```

Omit `dimensions` unless your endpoint accepts that request field.

Ingestion writes child offsets. The service embeds missing children in background batches. Search fuses FTS5 parent ranks and vector child ranks. When the endpoint is down, search falls back to lexical results and retries after a cooldown.

Manage the derived index:

```sh
companion-gateway memory index status
companion-gateway memory index backfill --limit 256
companion-gateway memory index rebuild
```

Changing the endpoint, model, dimensions, or chunk sizes selects a new derived index. Old vectors stay harmless until `memory index rebuild` removes them.

`memory.recent_messages`, `memory.search_hits`, and `memory.context_messages` tune how much history and adjacent context the injection carries. `rrf_k`, `lexical_candidates`, `semantic_candidates`, and `semantic_min_similarity` tune fusion and filtering.

`memory.injection_max_chars` caps the assembled context. Identity and the companion state block never truncate. If they exceed the budget, the context endpoint returns 422. Evergreen facts and session records fill the remaining space. `evergreen.enabled` toggles the fact block; `evergreen.max_items` and `evergreen.max_chars` bound it.

## Proactive scheduling

`proactive.enabled` turns evaluation on. The scheduler runs every `proactive.poll_interval_seconds`. A message needs `proactive.minimum_silence_minutes` of silence. After a send, `cooldown_minutes` applies, `max_per_day` caps daily sends, and `max_unanswered` caps ignored messages. `quiet_start_hour` and `quiet_end_hour` block local quiet hours. The emotional gates `longing_threshold` and `fear_threshold` come from `emotions.json`. The service stores each decision before delivery, so restarts keep the limits and dedupe events.

## Run a durable single-instance service

Run one process against one `state.sqlite3` file. SQLite WAL allows a single writer, and the in-process schedulers assume one evaluator. Two processes on the same file corrupt state. One database is one companion. A second companion needs its own config, data directory, port, and service unit.

Keep `state.sqlite3` and its `-wal` and `-shm` files on a persistent volume. A container scratch disk or an ephemeral instance disk drops all memory, affect, facts, and proactive events on restart.

Set up a `systemd --user` service:

```sh
mkdir -p ~/.config/companion-gateway ~/.config/systemd/user
cp config.example.json ~/.config/companion-gateway/config.json
cp deploy/companion-gateway.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now companion-gateway
journalctl --user -u companion-gateway -f
```

The shipped unit sets `COMPANION_GATEWAY_CONFIG` to `config.yaml`. Do not edit `deploy/companion-gateway.service`. Override the setting instead:

```sh
systemctl --user edit companion-gateway
```

Add this drop-in and save:

```ini
[Service]
Environment=COMPANION_GATEWAY_CONFIG=%h/.config/companion-gateway/config.json
```

Run `systemctl --user daemon-reload` and restart the service. The drop-in loads after the unit, so its `Environment=` assignment wins and the service reads `config.json`.

Secrets belong in the environment, never in config files. The unit already loads an optional `~/.config/companion-gateway/environment` file. Put keys there and restrict permissions:

```sh
install -m 600 /dev/null ~/.config/companion-gateway/environment
```

```sh
UPSTREAM_API_KEY=replace-me
EMBEDDING_API_KEY=replace-me
COMPANION_TOKEN=replace-with-a-long-random-value
```

Config stores variable names such as `UPSTREAM_API_KEY`, `EMBEDDING_API_KEY`, and `COMPANION_TOKEN`, not the values.

Check health:

```sh
curl http://127.0.0.1:8765/health
```

## Auth and network

The default bind stays on localhost. To reach the service from another device, terminate TLS at a reverse proxy or keep the service on a private network. Do not expose the plaintext HTTP port.

Set `api_token_env` to the name of an environment variable holding a long random token. An empty `api_token_env` disables state auth. When it names a variable, the service reads that variable on each request: a missing or empty value makes every `/state/v1/*` request return 401, and a set value requires each request to send that value in `X-Companion-Token`. Export the named variable before you start the service. Keep the state API on a trusted network while auth is disabled.

`/health`, `/v1/models`, and `/v1/chat/completions` skip the companion token. Put the whole service behind edge access controls when it leaves localhost.

The proxy uses a server-owned key when `upstream.api_key_env` is nonempty. It sends that value as a bearer token and drops the caller's `Authorization` header. Otherwise it forwards the caller's header.

## Migrate a legacy YAML config

Convert a legacy file once:

```sh
companion-gateway migrate-config config.yaml config.json
```

The command validates the source before writing. It rewrites `data_dir` and path-section values as absolute paths, so the destination keeps the same meaning wherever it lives. It refuses to overwrite an existing destination unless you pass `--force`.

The command refuses literal credentials. Values in `api_key_env` and `api_token_env` must be environment variable names. Strings that resemble URL userinfo, bearer tokens, secret prefixes, or private keys stop the migration. Keep secrets in the environment.

## Backup, restore, and troubleshoot

Back up every user-authored file: the config file, the identity Markdown, custom prompts and emotions files, and the database.

Create a consistent live backup:

```sh
companion-gateway backup /path/to/backups/state-$(date +%F).sqlite3
```

The command uses the SQLite online backup API and stays safe while the service writes. Do not copy `state.sqlite3` during active writes.

Restore:

1. Stop the service.
2. Place the backup database file in `data_dir`.
3. Start the service.

The service upgrades a schema version 2 database to version 3 in place and rejects older schemas. Keep a current backup before any upgrade.

Common problems:

- Startup fails with a missing config file: `--config` and `COMPANION_GATEWAY_CONFIG` treat a missing file as an error. Fix the path or drop the explicit setting.
- Startup fails with a missing identity file: the configured `identity_prompt.path` does not exist or is not valid UTF-8. Correct the file or clear the path.
- Hybrid search returns lexical results: the embedding endpoint is unreachable or misconfigured. Check `memory.embedding.base_url`, the model, and the key variable. `memory index status` reports `cooling_down` and `last_error`.
- State endpoints return 401: the request lacks a matching `X-Companion-Token` header, or the variable named by `api_token_env` is missing or empty. Set and export the named variable before launch.
- A second process drops data: two processes share one `state.sqlite3`. Run one process per database.

## Deployment limits

The supported model is a single local process with one SQLite file on a persistent volume. FTS5 and the sqlite-vec extension run in-process. No hosted runtime has been tested, and cloud portability is unverified. Serverless workers and ephemeral container disks lose SQLite state on sleep or restart. Treat a persistent-volume host as the baseline.
