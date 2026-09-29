# Companion State Gateway configuration

Copy the example and edit it:

```sh
cp config.example.json config.json
```

`config.example.json` at the repository root is the complete, authoritative example. Packaged defaults live in `defaults.json` and `emotions.json` inside the installed package. The runtime reads those files directly, so Python duplicates no default value. Treat the example and the packaged JSON as the full field list. This page explains the settings that change behavior.

## Config format, lookup, and path rules

The service reads JSON configuration only. `config.json` is the supported format.

Config lookup order:

1. `--config PATH`. A missing explicit file is an error.
2. `COMPANION_GATEWAY_CONFIG`. A missing file is an error.
3. `config.json` in `$XDG_CONFIG_HOME/companion-gateway/`, or `~/.config/companion-gateway/config.json` when `XDG_CONFIG_HOME` is unset.
4. `config.json` in the current directory (fallback).
5. Packaged defaults. A missing implicit file is valid.

The loader never merges configuration files.

Parsing is strict. Duplicate keys, unknown fields, `NaN`, `Infinity`, JSONC comments, and wrong types raise an actionable error.

Startup migrates legacy keys once and rewrites the file as canonical JSON. The renames are `memory.child_chars` to `memory.chunk_max_chars`, `memory.child_overlap_chars` to `memory.chunk_overlap_chars`, `proactive.minimum_silence_minutes` to `proactive.min_silence_minutes`, and `proactive.failed_retry_minutes` to `proactive.retry_delay_minutes`. A `memory.embedding` block moves to the top-level `embedding`. The loader drops `semantic_enabled` and `semantic_min_similarity`. A canonical value wins when both names appear. Later starts leave the migrated file untouched.

Path rules:

- JSON: `data_dir`, `emotions.path`, `identity_prompt.path`, `prompts.path`, and `decision.mods_dir` resolve against the config file directory.
- Absolute paths pass through unchanged.

## Core settings

- `host`, `port`: bind address and port. Defaults `127.0.0.1` and `8765`.
- `data_dir`: directory holding `state.sqlite3`.
- `timezone`: local zone for quiet hours and daily limits.
- `api_token_env`: name of the environment variable holding the shared API token. Empty keeps local unauthenticated mode.
- `api_openai.enabled`: enables the optional OpenAI-compatible HTTP routes. It defaults to `true`. Changing it requires a service restart. When disabled, those routes return 404 and are omitted from OpenAPI, while state and health routes remain available.
- `storage.enabled`: keeps messages and evergreen facts in the built-in SQLite store. It defaults to `true`. Setting it to `false` keeps existing rows on disk but makes the message, memory, context, and evergreen fact routes return `503`. The affect and proactive routes stay up, and proactive evaluation does nothing while storage is off.
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

Emotions use the current 16-dimension engine. Every dimension name, `neutral`, `floor`, `tau`, silence rate, `proactive_sent_deltas`, `impact_scale`, and threshold lives in the packaged `emotions.json`. The engine stays deterministic and calls no model. Affect knobs (`mood_follow_hours`, `mood_return_hours`, and the silence rates) and the proactive emotional thresholds (`longing_threshold`, `fear_threshold`) also live there and accept config overrides.

Export the packaged files, edit the copies, and reference them:

```sh
python -c "import json, companion_gateway.emotions as e; json.dump(e.default_emotions(), open('emotions.json','w'), ensure_ascii=False, indent=2)"
python -c "import json, companion_gateway.prompts as p; d=p.default_prompts(); d.pop('schema_version'); d.pop('prompts_version'); json.dump(d, open('prompts.json','w'), ensure_ascii=False, indent=2)"
```

The emotions command writes the full snapshot, version fields included. The prompts overlay accepts text slots and rejects version metadata, so the prompts command drops `schema_version` and `prompts_version` before writing.

Reference the copies:

```json
{
  "emotions": {"path": "emotions.json", "expected_version": "0.2.0"},
  "prompts": {"path": "prompts.json"}
}
```

`emotions.expected_version` pins the file's `emotion_version`. A mismatch fails startup. Omit it to accept any valid file.

Prompts replace whole text slots. A provided slot must carry every key in that slot. A missing key fails validation. Omitted slots inherit the packaged text. Blank strings are valid replacements. The five slots are `affect_presentation`, `companion_state`, `evergreen`, `proactive_generation_instruction`, and `decision_instruction`. `proactive_generation_instruction` is an object with `base` and `variants`. The gateway picks one variant per event by send count and appends it to the base, so you control rotation by editing the list. Each instruction also names the ruling feeling for that send, with its value and neutral point, plus an escalation stage driven by the unanswered count. The first nudge stays open, the next turns pointed, later ones go cold and short. Silence timing stays in the event fields and conversation records, not in the instruction text.

Value precedence resolves in this order:

1. Packaged `emotions.json`.
2. A custom file from `emotions.path`.
3. Explicit config overrides.

An explicit override wins even when it equals the packaged default. Config fields carry an `UNSET` sentinel, so resolution never guesses the source. The engine snapshots these values at construction. Mutating the config object later changes nothing.

## Decision plugins

On each new user message the gateway can invoke one Python decision plugin. The plugin selects at most one emotion dimension from the resolved `emotions.json`. The engine then increases that dimension by `decision.increment`, clamped to the dimension range. No plugin, or a failed or invalid decision, means no decision-driven adjustment. Contact bookkeeping and proactive scheduling still run.

Each plugin directory requires `README.md`, `main.py`, `pyproject.toml`, and `uv.lock`. Install `uv` on the gateway's executable search path. The gateway synchronizes locked dependencies into the plugin's dedicated environment during initialization. Every exported operation in `main.py` follows `fn(request, options)`:

```python
def decide(request, options):
    ...
```

The runner passes sanitized request data and options to the child plugin. Decision plugins return `{"emotion": "<dimension>"}` or `None`. Memory operation names and result schemas are documented in the [project README](https://github.com/Somme4096/sophia).

Configuration:

```json
{
  "decision": {
    "module": "my_suite",
    "options": {"endpoint": "http://127.0.0.1:8000"},
    "increment": 0.1,
    "timeout_seconds": 15,
    "mods_dir": ""
  }
}
```

- `module`: a plain plugin directory name, resolved only under the mods directory. Empty disables the plugin. A name containing a path separator or any non-identifier character is rejected.
- `options`: passed through to the plugin verbatim.
- `increment`: the bounded amount added to the selected dimension. Must be finite and in `(0, 1]`. Default `0.1`.
- `timeout_seconds`: the outer deadline for each decision process. Must be finite and positive. Default `15`. Set it above any HTTP timeout configured in `options`.
- `mods_dir`: an optional mods directory. Empty uses `<default config dir>/mods`, that is `~/.config/companion-gateway/mods`. A relative value resolves against the config file location.

Memory plugins use the same fields and an operation-based `fn(request, options)` contract. `memory_plugin.module` defaults to empty, which disables the plugin. A configured name loads that plugin directory from the mods directory:

```json
{
  "memory_plugin": {
    "module": "everos_memory",
    "mods_dir": "mods",
    "timeout_seconds": 15,
    "ingest_batch_size": 50,
    "ingest_backfill_interval_seconds": 30,
    "options": {}
  }
}
```

The gateway stores messages and facts itself. A memory plugin adds retrieval and derived context. On each new message the gateway pushes committed `user` and `assistant` rows to the plugin through `ingest_messages`, then records the returned `highest_id` in the `plugin_ingest_state` cursor. After an outage the scheduler retries from that cursor every `ingest_backfill_interval_seconds`, so catch-up is at-least-once and a failed push never blocks message ingest. When a harness builds context, the gateway calls `inject_context` with the query, the harness, the conversation id, and a `memory.plugin_context_max_chars` budget. A plugin can also answer `match_phrase`, `status`, `backfill_once`, `rebuild_chunks`, and `rebuild_index`. Operation names and result schemas live in the [project README](https://github.com/Somme4096/sophia).

The EverOS plugin shows the intended split. It owns its outbox, sidecar delivery, and episodic extraction, and it never creates or migrates the core message or fact tables. See [examples/mods/everos-memory](../examples/mods/everos-memory/README.md) and [examples/mods/README.md](../examples/mods/README.md).

`COMPANION_GATEWAY_MODS_DIR` overrides `decision.mods_dir` when set. This is the recommended way to test an isolated mods tree without touching the live configuration.

Decision plugins are trusted child processes. The runner uses the plugin's dedicated `uv` environment, with inherited OS permissions and no container or OS sandbox. Only install plugins you wrote or reviewed. A plugin that fails to load, raises, or returns a malformed result is logged and ignored. Message ingest keeps working. HTTP adapters may define explicit options such as credentials and an optional literal-IP `allowed_ips` list. When present, every request and redirect must remain on an allowed literal address.

The default `decision_instruction` asks the plugin to choose the companion's own emotional response to the message, not to classify the speaker's emotion. Replace it with the `prompts` overlay when you want different selection criteria.

## Retrieval and the memory index

Core retrieval is lexical. The gateway searches the built-in message store with SQLite FTS5. It loads no vector extension. `memory.recent_messages`, `memory.search_hits`, and `memory.context_messages` tune how much history and adjacent context the injection carries.

These fields stay valid on read for backward compatibility, and core ignores them: `memory.retrieval_mode`, `child_chars`, `child_overlap_chars`, `lexical_candidates`, `semantic_candidates`, `rrf_k`, and `semantic_min_similarity`. The loader rewrites them to canonical names on first load. One field still matters. `embedding.backfill_interval_seconds` sets the interval for the plugin index backfill loop, which runs only when a memory plugin is configured.

## Shared embedding and semantic affect

The top-level `embedding` block configures one OpenAI-compatible embedding endpoint. The gateway appends `/embeddings` to `base_url` and reads the key from `api_key_env`.

```json
{
  "embedding": {
    "base_url": "http://127.0.0.1:11434/v1",
    "api_key_env": "EMBEDDING_API_KEY",
    "model": "your-embedding-model",
    "dimensions": null
  }
}
```

A legacy `memory.embedding` block moves here on first load. When `base_url` and `model` are both set, affect appraisal compares each new unlabeled user message against short emotional prototypes with cosine similarity, and the strongest matching emotion dimension adjusts the affect state. When either is unset, appraisal is off and the decision plugin alone selects the emotion. A failed embedding request uses a short cooldown and the engine makes no semantic adjustment. Retrieval stays lexical either way.

A memory plugin contributes context through `inject_context`, capped by `memory.plugin_context_max_chars`. The gateway truncates that text to the budget and counts it against the injection total. A missing or failed plugin returns no plugin context, and the injection keeps the stored recent and evergreen content.

Manage the memory index:

```sh
companion-gateway memory index status
companion-gateway memory index backfill --limit 256
companion-gateway memory index rebuild
```

With no plugin these commands report lexical status. With a plugin they call the plugin's `status`, `backfill_once`, and `rebuild_chunks` operations, and `memory index backfill` may need the `embedding.backfill_interval_seconds` cadence to catch up. `memory index rebuild` also rebuilds the core FTS5 index.

`memory.injection_max_chars` caps the assembled context. Identity and the companion state block never truncate. If they exceed the budget, the context endpoint returns 422. Evergreen facts, plugin context, and session records fill the remaining space. `evergreen.enabled` toggles the fact block. `evergreen.max_items` and `evergreen.max_chars` bound it.

## Proactive scheduling

`proactive.enabled` turns evaluation on. The scheduler runs every `proactive.poll_interval_seconds`. A message needs `proactive.min_silence_minutes` of silence. After a send, `cooldown_minutes` applies, `max_per_day` caps daily sends, and `max_unanswered` caps ignored messages. `quiet_start_hour` and `quiet_end_hour` block local quiet hours. The emotional gates `longing_threshold` and `fear_threshold` come from `emotions.json`. The service stores each decision before delivery, so restarts keep the limits and dedupe events.

## Related documentation

- [guide.md](guide.md): service setup, auth and network, backup, troubleshooting.
- [api.md](api.md): HTTP endpoints and schemas.
- [README.md](../README.md): project overview and quickstart.
- [third-party-notices.md](../third-party-notices.md): license terms.
