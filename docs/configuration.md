# Companion State Gateway configuration

Copy the example and edit it:

```sh
cp config.example.json config.json
```

`config.example.json` at the repository root is the complete, authoritative example. Packaged defaults live in `defaults.json` and `emotions.json` inside the installed package. The runtime reads those files directly, so Python duplicates no default value. Treat the example and the packaged JSON as the full field list. This page explains the settings that change behavior.

## Config format, lookup, and path rules

The service reads JSON first. `config.json` is the supported format. Legacy `config.yaml` still loads with a deprecation warning.

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

## Core settings

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

## Identity, emotions, and prompts

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

- `lexical`: SQLite FTS5 only. The service sends no embedding requests.
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

## Migrate a legacy YAML config

Convert a legacy file once:

```sh
companion-gateway migrate-config config.yaml config.json
```

The command validates the source before writing. It rewrites `data_dir` and path-section values as absolute paths, so the destination keeps the same meaning wherever it lives. It refuses to overwrite an existing destination unless you pass `--force`.

The command refuses literal credentials. Values in `api_key_env` and `api_token_env` must be environment variable names. Strings that resemble URL userinfo, bearer tokens, secret prefixes, or private keys stop the migration. Keep secrets in the environment.

## Related documentation

- [guide.md](guide.md): service setup, auth and network, backup, troubleshooting.
- [api.md](api.md): HTTP endpoints and schemas.
- [README.md](../README.md): project overview and quickstart.
- [third-party-notices.md](../third-party-notices.md): license terms.
