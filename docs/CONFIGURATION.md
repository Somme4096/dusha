# Companion State Gateway configuration

Runtime configuration is JSON-first (`config.json`). Legacy `config.yaml` still loads with a deprecation warning. See [CONFIGURATION_MIGRATION.md](CONFIGURATION_MIGRATION.md) for the migration command and strict parsing rules.

All packaged defaults are owned by two JSON resources inside the installed package (`src/companion_gateway/resources/`):

- `emotions.json` — every emotional definition and tuning value (16 dimensions with `neutral`/`floor`/`tau`, label deltas, categories, silence tuning, prompt thresholds, affect knobs, proactive thresholds).
- `defaults.json` — non-emotional runtime defaults (host, port, timezone, memory, evergreen, upstream, proactive scheduling, `identity_prompt`, `prompts`).
- `prompts.json` — every core-owned prompt and instruction text slot.

The 16 emotional dimensions and their update algorithms are unchanged. The JSON migration moved the data, not the behavior. No appraisal module or target interpolation is implemented; this is documented separately in [DECISION_MODULE_DESIGN.md](DECISION_MODULE_DESIGN.md).

## Quick start

```sh
cp config.example.json config.json
# edit config.json, then run:
companion-gateway serve
```

Config resolution order:

1. `--config PATH` argument. A missing explicit file is an error.
2. `COMPANION_GATEWAY_CONFIG` environment variable. A missing file is an error.
3. `config.json` in the current directory.
4. `config.yaml` in the current directory (legacy, deprecation warning).
5. Packaged defaults. An absent implicit config file is valid.

## Top-level sections

| Section | Type | Default | Meaning |
| --- | --- | --- | --- |
| `data_dir` | string (path) | `./data` | SQLite database directory. In JSON config, a relative path resolves against the config file directory. |
| `host` | string | `127.0.0.1` | Bind address. |
| `port` | integer | `8765` | Bind port. |
| `timezone` | string | `Asia/Taipei` | Local timezone used for quiet hours and daily limits. |
| `api_token_env` | string | `""` | Environment variable holding the companion token for `/state/v1/*`. Empty disables state auth. |
| `identity_prompt` | object | `{"path": ""}` | Raw user-authored identity Markdown file. |
| `prompts` | object | `{"path": ""}` | Prompts overlay file. |
| `emotions` | object | (optional) | Custom emotional definitions. |
| `upstream` | object | see table | OpenAI-compatible proxy upstream. |
| `memory` | object | see table | Storage and retrieval settings. |
| `evergreen` | object | see table | Evergreen fact limits. |
| `affect` | object | see table | Affect knob overrides and legacy override maps. |
| `proactive` | object | see table | Proactive scheduling and thresholds. |

Unknown top-level fields, duplicate keys, `NaN`/`Infinity`, JSONC comments, and wrong field types are rejected with an actionable error.

## Path sections

`emotions.path`, `identity_prompt.path`, and `prompts.path` resolve relative to the config file directory; absolute paths are used as-is. An empty path selects the packaged default.

- `identity_prompt.path`: a raw Markdown file supplied verbatim on every context. Configured but missing file, or invalid UTF-8, fails at startup. Blank file is allowed. The identity is never archived, inferred, or modified.
- `prompts.path`: a JSON overlay of text slots. A provided slot replaces the packaged slot completely (all keys in the slot must be present; missing keys fail validation). Omitted slots inherit the packaged defaults. Blank strings are valid replacements.
- `emotions.path`: a full custom `emotions.json`. `emotions.expected_version` pins `emotion_version`; a mismatch fails startup.

Example:

```json
{
  "data_dir": "data",
  "identity_prompt": {"path": "identity.md"},
  "prompts": {"path": "prompts.json"},
  "emotions": {"path": "emotions.json", "expected_version": "0.1.0"}
}
```

## upstream

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `base_url` | string | `""` | OpenAI-compatible base URL for the proxy. Empty disables the proxy. |
| `api_key_env` | string | `UPSTREAM_API_KEY` | Environment variable holding the upstream key. When set to a nonempty value the proxy uses it as the bearer token; otherwise it forwards the caller `Authorization`. |
| `timeout_seconds` | number | `120.0` | Upstream request timeout. |

## memory

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `recent_messages` | integer | `8` | Recent messages considered for context. |
| `search_hits` | integer | `4` | Retrieval hits considered. |
| `context_messages` | integer | `1` | Adjacent messages returned per hit. |
| `injection_max_chars` | integer | `12000` | Total context injection budget. |
| `retrieval_mode` | string | `lexical` | `lexical` or `hybrid`. |
| `child_chars` | integer | `800` | Derived chunk size. |
| `child_overlap_chars` | integer | `120` | Derived chunk overlap. |
| `lexical_candidates` | integer | `24` | Lexical candidate count. |
| `semantic_candidates` | integer | `24` | Semantic candidate count. |
| `rrf_k` | integer | `60` | Reciprocal-rank fusion constant. |
| `semantic_min_similarity` | number | `0.3` | Minimum semantic similarity. |
| `embedding` | object | see table | Embedding endpoint. |

`embedding` keys: `base_url`, `api_key_env` (default `EMBEDDING_API_KEY`), `model`, `dimensions` (integer or null), `timeout_seconds` (default `10.0`), `batch_size` (default `32`), `backfill_interval_seconds` (default `10`), `failure_cooldown_seconds` (default `60`).

## evergreen

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | boolean | `true` | Whether facts are injected. |
| `max_items` | integer | `32` | Maximum facts injected. |
| `max_chars` | integer | `4000` | Maximum evergreen block characters. |

## affect

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `emotions_path` | string | `""` | Internal path resolved from `emotions.path`; do not set directly. |
| `expected_emotion_version` | string | `""` | Internal pin from `emotions.expected_version`; do not set directly. |
| `mood_follow_hours` | number | `12.0` | Mood follows base time constant. |
| `mood_return_hours` | number | `72.0` | Mood returns to neutral time constant. |
| `habituation_window_minutes` | integer | `15` | Repeat-label window. |
| `habituation_factor` | number | `0.7` | Per-repeat scale factor, in `(0, 1]`. |
| `silence_longing_per_hour` | number | `0.04` | Silence-driven longing growth. |
| `silence_anxiety_per_hour` | number | `0.02` | Silence-driven anxiety growth. |
| `silence_seeking_per_hour` | number | `0.02` | Silence-driven seeking growth. |
| `classification_fallback_seconds` | integer | `120` | Automatic keyword fallback delay. |
| `dimensions` | object | `{}` | Legacy per-dimension overrides (`neutral`, `floor`, `tau`). Unknown dimensions or keys are errors. |
| `label_patterns` | object | `{}` | Legacy label phrase overrides. Unknown labels are errors. |

The emotional defaults live in `emotions.json`, not in this section. Affect knobs and proactive thresholds provided here or in a custom `emotions.json` are validated with the same bounds as the packaged defaults.

## proactive

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | boolean | `true` | Whether evaluation runs. |
| `poll_interval_seconds` | integer | `60` | Scheduler cadence. |
| `minimum_silence_minutes` | integer | `180` | Minimum silence before a proactive message. |
| `longing_threshold` | number | `0.48` | Longing gate (emotional, from `emotions.json`). |
| `fear_threshold` | number | `0.55` | Fear gate (emotional, from `emotions.json`). |
| `cooldown_minutes` | integer | `360` | Delay after a sent proactive message. |
| `max_per_day` | integer | `2` | Daily proactive limit. |
| `max_unanswered` | integer | `2` | Unanswered limit. |
| `quiet_start_hour` | integer | `1` | Quiet window start (local). |
| `quiet_end_hour` | integer | `8` | Quiet window end (local). |
| `lease_seconds` | integer | `120` | Poll lease duration. |
| `failed_retry_minutes` | integer | `15` | Retry delay after a failed send. |

## Custom overrides and the snapshot contract

Effective emotional values resolve in this order at each engine assembly: packaged `emotions.json` defaults, then a custom file from `emotions.path`, then explicit config overrides. An explicitly provided override always wins, including a value equal to a packaged default. Config fields use an `UNSET` sentinel so resolution never guesses whether a value came from the caller.

Mutating config objects after a service is created does not change the already-created engines. Config is a point-in-time snapshot at assembly. The `emotions` and `prompts` fingerprints exposed on the context response reflect the snapshot at assembly time and are not updated by later mutation.

## Prompts slots

`prompts.json` defines four text slots: `affect_presentation` (prefix, separator, suffix, baseline, `level_connector`, level words), `companion_state` (delimiters and instructions), `evergreen` (delimiters), and `proactive_generation_instruction`. A user prompts file may replace any slot completely. Within a provided slot, all keys must be present; omitted slots inherit the packaged defaults; unknown slots or keys are errors. The active prompt snapshot is fingerprinted for diagnostics.

## Related documentation

- [CONFIGURATION_MIGRATION.md](CONFIGURATION_MIGRATION.md) — resolution order, strict parsing, path rules, migration command.
- [API.md](API.md) — endpoint reference and examples.
- [INTEGRATION_GUIDE.md](INTEGRATION_GUIDE.md) — provider-neutral consumption patterns.
- `docs/DEPLOYMENT.md` — deployment and hosting assessment (owned separately).