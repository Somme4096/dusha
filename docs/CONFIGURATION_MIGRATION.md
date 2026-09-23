# Configuration migration

Phase 1 moves every emotional definition, default, table value, and tuning knob out of Python and into a packaged `emotions.json`, and moves non-emotional runtime defaults into a packaged `defaults.json`. Runtime configuration becomes JSON (`config.json`). Legacy `config.yaml` still loads with a deprecation warning.

This document covers the new layout, the resolution order, path rules, the migration command, and validation. It does not cover the decision module design (see `docs/DECISION_MODULE_DESIGN.md`).

## Files

- `config.json`: the user runtime configuration. Strict JSON, no comments (no JSONC), duplicate keys and unknown fields are rejected.
- `config.example.json`: a usable starting point at the repository root. The `emotions.path` is empty, which selects the packaged emotional defaults.
- `src/companion_gateway/resources/emotions.json`: the authoritative emotional data shipped with the package. It owns the 16 dimensions (`neutral`, `floor`, `tau`), `label_deltas`, `contact_deltas`, `soothing_deltas`, `negative_labels`, `soothing_labels`, `negative_dimensions`, silence caps and rates, the dejection gate, the recent-labels limit, the negative follow-up minutes, the follow-up expiration hours, `proactive_sent_deltas`, the impact scale, the mood-follow gain bounds, the prompt selection thresholds, `label_patterns`, the affect knob defaults, and the proactive emotional thresholds.
- `src/companion_gateway/resources/defaults.json`: the packaged defaults for non-emotional runtime settings (host, port, timezone, memory, evergreen, upstream, proactive scheduling). Dataclass field defaults in `src/companion_gateway/config.py` read from this file, so no default value is duplicated in Python. The packaged defaults are validated at import with the same schema rules used for user config.

Emotional algorithms stay in code (`src/companion_gateway/affect.py` and `src/companion_gateway/proactive.py`); every emotional value they use comes from the resolved `emotions.json` snapshot. The affect engine reads all affect knobs from the snapshot; the proactive engine reads the emotional thresholds from the snapshot.

## Resolution order

1. Explicit `--config PATH` argument. A missing explicit file is an error.
2. `COMPANION_GATEWAY_CONFIG` environment variable. A missing file is an error.
3. `config.json` in the current directory.
4. `config.yaml` in the current directory (legacy, loads with a `DeprecationWarning`).
5. Packaged defaults. An absent implicit config file is valid.

When both `config.json` and `config.yaml` exist, `config.json` wins. They are not merged.

## Emotional value precedence

Effective emotional values resolve once, in this order:

1. Packaged `emotions.json` defaults.
2. A custom `emotions.json` referenced by `emotions.path`, if configured (version-pinned when `expected_version` is set).
3. Explicit configuration overrides: affect knobs and proactive thresholds provided in the config file or passed to `AffectConfig` / `ProactiveConfig` at construction.

An explicitly-provided override always wins, including a value that equals the packaged default. Config fields carry an `UNSET` sentinel until filled, so resolution never guesses whether a value came from the caller or from a default.

Runtime behavior:

- The affect engine (`AffectEngine`) reads every affect knob (`mood_follow_hours`, `mood_return_hours`, `habituation_window_minutes`, `habituation_factor`, `silence_*_per_hour`, `classification_fallback_seconds`) from the resolved snapshot.
- The proactive engine (`ProactiveEngine`) reads `longing_threshold` and `fear_threshold` from the resolved snapshot.
- `dimensions` and `label_patterns` override maps merge over the snapshot. Unknown dimension or label names, and unknown keys inside a dimension override, are errors.

The snapshot is resolved once at engine construction. Mutating config objects after engine construction does not change an already-created engine; this is a point-in-time snapshot contract. `AffectEngine.emotions_fingerprint` is the fingerprint of the effective snapshot at assembly time (the proactive engine folds its resolved thresholds into the snapshot and recomputes the fingerprint). It is not updated by later config mutation.

## Strict parsing

JSON config is parsed strictly:

- Duplicate keys are rejected.
- Unknown fields are rejected with the offending field path.
- `NaN` and `Infinity` constants are rejected.
- Field types are checked (integers, floats, strings, booleans, objects, arrays). A boolean is not accepted where a number is expected.

Legacy YAML is validated the same way (duplicate keys, unknown fields, types). Errors are raised, not silently ignored.

## Path rules

- `emotions.path` in the `emotions` section resolves relative to the directory of the config file that references it. Absolute paths are used as-is. An empty string selects the packaged defaults.
- `data_dir` differs by format:
  - In legacy YAML, `data_dir` keeps its historical meaning: a relative value is resolved against the current working directory.
  - In JSON, `data_dir` resolves relative to the directory of the config file, consistent with `emotions.path`.

The migration command converts relative paths to absolute values so the effective meaning is preserved regardless of where the destination file is placed.

## emotions.json

`emotions.json` is versioned (`schema_version` and `emotion_version`) and validated at load. A stable canonical fingerprint is computed over the effective snapshot and exposed as `AffectEngine.emotions_fingerprint`. The fingerprint reflects every applied override (custom file and explicit config), not just the raw packaged default.

`value_range` is constrained to `[0, 1]` in this preserve-behavior phase. The delta math assumes a unit range, so arbitrary ranges are rejected rather than applied incorrectly.

Validation rules, each reported with an actionable field path:

- All numeric values are finite; booleans are not numbers.
- `value_range` must be exactly `[0, 1]`.
- `floor <= neutral`, and both sit inside `value_range`.
- `tau`, `mood_follow_hours`, `mood_return_hours`, `habituation_window_minutes`, `classification_fallback_seconds`, `recent_labels_limit`, `negative_follow_up_minutes`, and `follow_up_expiration_hours` are positive.
- `impact_scale` and `mood_follow_gain.factor` are positive; `mood_follow_gain.max >= mood_follow_gain.min >= 0`.
- `habituation_factor` is in `(0, 1]`.
- Silence rates and caps are not negative.
- Every dimension referenced by deltas, categories, silence caps, or `proactive_sent_deltas` exists.
- Every `label_patterns` key is a known label; every `negative_labels` or `soothing_labels` entry is a known label.
- `prompt.top_n` is a positive integer and `level_high >= level_elevated`.
- Proactive thresholds sit inside `value_range`.

An override map under `affect.dimensions` or `affect.label_patterns` merges over the resolved snapshot and is validated the same way. An override that references an unknown dimension or label, or that carries an unknown key, is an error. Programmatic affect-knob and proactive-threshold overrides are validated with the same bounds at resolution.

## Using a custom emotions.json

```json
{
  "emotions": {
    "path": "deploy/emotions.json",
    "expected_version": "0.1.0"
  }
}
```

`expected_version` pins the `emotion_version` of the referenced file. A mismatch is an error. Omitting `expected_version` allows any valid file.

## Migrating a legacy config

`companion-gateway migrate-config SOURCE DESTINATION [--force]` converts a legacy YAML (or an existing JSON) config to `config.json`.

- The source is validated before writing; an invalid source is an error.
- `data_dir` (legacy YAML, CWD-relative) and `emotions.path` (config-file-relative) are emitted as absolute paths so the destination keeps the same effective meaning even when it lives elsewhere.
- The destination is not overwritten unless `--force` is passed.
- Secret handling: values in `api_key_env` and `api_token_env` must be environment variable names, not literal credentials. Strings that look like embedded credentials (URL userinfo, bearer tokens, secret prefixes, private keys) are refused. Config files should never contain literal secrets.
- The command writes no database data and does not modify the source.

Example:

```sh
companion-gateway migrate-config config.yaml config.json
```

## Backward compatibility

- Direct `AppConfig()` / `AffectConfig()` / `ProactiveConfig()` construction and field mutation keep working. Defaults now come from the packaged JSON.
- The existing nested YAML embedding test path keeps working with a deprecation warning.
- The 16 dimension names, default values, and label order are unchanged.
- The `affect event` CLI subcommand no longer restricts labels to the packaged set; the loaded engine validates labels against its effective snapshot.
- `DEFAULT_LABEL_PATTERNS` was removed from `config.py`; it had no consumers. `LABEL_DELTAS` in `affect.py` remains as a packaged-default view for backward-compatible inspection.
- Database schema is unchanged in this phase; the stored affect state uses the same dimension values.