# Configuration

Each companion reads one `config.json` from its home directory. `dusha serve <name>` writes the file on the first run:

```sh
dusha serve dusha
${EDITOR:-vi} ~/.config/dusha/dusha/config.json
```

The generated file matches [config.example.json](../config.example.json) and lists every key. Omit a key to keep its default. Restart the service after an edit.

## Which file loads

A companion home is `$XDG_CONFIG_HOME/dusha/<name>/`, or `~/.config/dusha/<name>/` when `XDG_CONFIG_HOME` is unset. The service takes the first match:

1. `--config PATH`. The file's directory becomes the home.
2. `--home DIR`
3. `--companion NAME`, or the name after `dusha serve`
4. `DUSHA_HOME`
5. `DUSHA_COMPANION`
6. The single companion under the config root
7. Packaged defaults, when no companion exists

Startup fails when several companions exist and you select none, or when the selected home has no `config.json`. A name holds letters, digits, dots, dashes, and underscores.

Rules for the file:

- Plain JSON. The parser rejects comments, unknown keys, duplicate keys, wrong types, and out-of-range numbers.
- Relative paths resolve against the config file's directory.

## Server

| Key | Default | Notes |
| --- | --- | --- |
| `host` | `127.0.0.1` | |
| `port` | `8765` | |
| `data_dir` | `data` | Holds `state.sqlite3`. |
| `timezone` | `Asia/Taipei` | IANA zone for quiet hours and the daily limit. |
| `api_token_env` | `""` | Environment variable that holds the API token. Empty turns auth off. See [Auth](guide.md#auth). |
| `api_openai.enabled` | `true` | Serves the [OpenAI proxy](api.md#openai-proxy) under `/v1/*`. |
| `storage.enabled` | `true` | `false` closes the message, memory, context, evergreen, and memo routes with `503`. Stored rows stay on disk. |
| `upstream.base_url` | `""` | Provider the proxy forwards to. Empty disables the proxy. |
| `upstream.api_key_env` | `UPSTREAM_API_KEY` | Environment variable that holds the provider key. When it is unset, the proxy forwards the caller's `Authorization` header. |
| `upstream.timeout_seconds` | `120` | |
| `upstream.allowed_hosts` | `[]` | Hosts that [passthrough](api.md#passthrough) may reach. Entries are host names or `host:port`, with `*` wildcards such as `*.example.com` or `*`. Empty disables passthrough. |

## Identity, emotions, and prompts

| Key | Default | Notes |
| --- | --- | --- |
| `identity_prompt.path` | `""` | Markdown file. The service prepends it verbatim to every context. Empty means no identity. |
| `emotions.path` | `""` | Custom emotions file. Empty uses the packaged one. |
| `emotions.expected_version` | `""` | Startup fails unless the file's `emotion_version` matches. Empty accepts any version. |
| `prompts.path` | `""` | Overlay for the packaged prompt text. |

A path that points at a missing file fails startup.

Export the packaged files, then edit the copies:

```sh
python -c "import json, dusha.emotions as e; json.dump(e.default_emotions(), open('emotions.json','w'), ensure_ascii=False, indent=2)"
python -c "import json, dusha.prompts as p; d=p.default_prompts(); d.pop('schema_version'); d.pop('prompts_version'); json.dump(d, open('prompts.json','w'), ensure_ascii=False, indent=2)"
```

```json
{
  "identity_prompt": {"path": "identity.md"},
  "emotions": {"path": "emotions.json", "expected_version": "0.4.0"},
  "prompts": {"path": "prompts.json"}
}
```

### emotions.json

The file defines the emotion dimensions and the rules that name them. Rename, add, or remove dimensions as you like. The packaged file ships 16.

| Section | Controls |
| --- | --- |
| `value_range` | `min` and `max` for every value. |
| `dimensions` | One entry per dimension: `neutral`, `floor`, `tau` in hours, plus optional `description` and `increment`. |
| `decision` | `increment`, the step added to the dimension a decision picks. |
| `negative_dimensions` | Dimensions a negative delta cannot pull below the current mood. |
| `silence.rules` | Drift while the user stays silent: `dimension`, `rate_per_hour`, `cap`, optional `gate_hours`. |
| `proactive_sent_deltas` | Deltas applied after a proactive message goes out. |
| `impact_scale`, `mood_follow_gain`, `affect` | Delta strength and mood timing. |
| `prompt` | Feelings shown on the Affect line: `top_n`, `deviation_threshold`, `level_high`, `level_elevated`, `always_show`. |
| `proactive.triggers` | Ordered list of `dimension`, `threshold`, and `reason`. The first trigger at or above its threshold starts a proactive message. |
| `appraisal` | Prototype sentences for [embedding](#embedding) appraisal: `prototypes`, `min_similarity`, `fallback_label`. |

A rule that names an undefined dimension fails startup.

### Overrides in config.json

Patch single values without a custom emotions file:

```json
{
  "affect": {
    "mood_follow_hours": 12,
    "mood_return_hours": 72,
    "dimensions": {"fear": {"neutral": 0.05}},
    "silence": {"longing": {"rate_per_hour": 0.02}}
  },
  "proactive": {"thresholds": {"longing": 0.4}}
}
```

| Key | Patches |
| --- | --- |
| `affect.dimensions` | `neutral`, `floor`, `tau`, or `increment` of an existing dimension. |
| `affect.silence` | A silence rule. A new dimension name adds a rule. |
| `proactive.thresholds` | The trigger threshold for that dimension. The dimension needs a trigger in the file. |

An override beats `emotions.path`, and `emotions.path` beats the packaged file.

### prompts.json

The overlay replaces whole slots. A slot you include must carry every key of that slot. Omitted slots keep the packaged text.

| Slot | Holds |
| --- | --- |
| `affect_presentation` | Wording of the Affect line. |
| `companion_state` | Delimiters and instructions for the state block. |
| `evergreen` | Delimiters for the fact block. |
| `context_blocks` | Delimiters for the memo and memory plugin blocks. |
| `proactive_framing` | `ruling`, `ladder`, `approach`, and `context_query` for proactive messages. |
| `proactive_generation_instruction` | `base` plus `variants`. The gateway rotates one variant per send. |
| `decision_instruction` | Selection criteria sent to the decision plugin. |

Placeholders in `proactive_framing`:

- `ruling` accepts `{dimension}`, `{value}`, `{neutral}`, and `{deviation}`.
- `approach` accepts `{variant}`.
- `ladder` holds one line per unanswered count. The last line covers higher counts.
- Write a literal brace as `{{` or `}}`. An unknown placeholder fails startup.

## Memory and context

Search is lexical and uses SQLite FTS5.

| Key | Default | Notes |
| --- | --- | --- |
| `memory.recent_messages` | `8` | Recent messages in the injection. |
| `memory.search_hits` | `4` | |
| `memory.context_messages` | `1` | Neighbors added around each search hit. |
| `memory.injection_max_chars` | `12000` | Cap for the whole injection. See [Build context](api.md#2-build-context) for the fill order. |
| `memory.plugin_context_max_chars` | `3000` | Cap for the memory plugin block. |
| `evergreen.enabled` | `true` | |
| `evergreen.max_items` | `32` | |
| `evergreen.max_chars` | `4000` | |
| `memo.max_items` | `10` | Memos per injection. |
| `memo.max_chars` | `2000` | Size of the memo block. |
| `memo.text_max_chars` | `4000` | Longest memo the API accepts. |
| `memo.reason_max_chars` | `1000` | Longest archive reason the API accepts. |

## Embedding

Optional. Set `base_url` and `model` to turn on semantic appraisal. The gateway compares each user message with the `appraisal` prototypes in `emotions.json` and moves the closest emotion. Search stays lexical.

| Key | Default | Notes |
| --- | --- | --- |
| `embedding.base_url` | `""` | OpenAI-compatible endpoint. The gateway appends `/embeddings`. |
| `embedding.api_key_env` | `EMBEDDING_API_KEY` | |
| `embedding.model` | `""` | |
| `embedding.dimensions` | `null` | Sent to the endpoint when set. |
| `embedding.timeout_seconds` | `10` | |
| `embedding.batch_size` | `32` | |
| `embedding.backfill_interval_seconds` | `10` | Interval of the memory plugin's index backfill. |
| `embedding.failure_cooldown_seconds` | `60` | Pause after a failed request. |

## Plugins

A plugin is a directory under the mods directory. The [README](../README.md#plugin-api) describes the contract, and [examples/mods](../examples/mods/README.md) holds two working plugins. Plugins need `uv` on the gateway's `PATH`.

- `decision` picks the emotion each user message moves.
- `memory_plugin` adds retrieval and extra context.

Both sections take these keys:

| Key | Default | Notes |
| --- | --- | --- |
| `module` | `""` | Plugin directory name. Empty disables the plugin. |
| `options` | `{}` | Passed to the plugin as written. |
| `mods_dir` | `""` | Empty uses `mods` in the companion home. `DUSHA_MODS_DIR` overrides it. |
| `timeout_seconds` | `15` | Deadline per call. Keep it above any HTTP timeout in `options`. |
| `env_passthrough` | `[]` | Environment variables the plugin may read, such as a proxy setting. |

Each section has keys of its own:

| Key | Default | Notes |
| --- | --- | --- |
| `decision.increment` | `null` | Replaces `decision.increment` from `emotions.json`. A per-dimension `increment` in that file still wins. |
| `memory_plugin.ingest_batch_size` | `50` | 1 to 500. |
| `memory_plugin.ingest_backfill_interval_seconds` | `30` | Retry interval for messages the plugin missed. |

Plugins run as child processes with your OS permissions and no sandbox. Install plugins you wrote or reviewed. The gateway logs a failing plugin and keeps storing messages without it.

## Proactive

| Key | Default | Notes |
| --- | --- | --- |
| `proactive.enabled` | `true` | |
| `proactive.poll_interval_seconds` | `60` | |
| `proactive.min_silence_minutes` | `180` | |
| `proactive.cooldown_minutes` | `360` | Gap after a send. |
| `proactive.max_per_day` | `2` | |
| `proactive.max_unanswered` | `2` | Sends stop after this many ignored messages. |
| `proactive.quiet_start_hour` | `1` | Local hour. |
| `proactive.quiet_end_hour` | `8` | Local hour. |
| `proactive.lease_seconds` | `120` | Time a harness holds a polled event before it returns to pending. |
| `proactive.retry_delay_minutes` | `15` | Wait after a failed delivery. |

The emotional triggers live in [emotions.json](#emotionsjson). Limits survive a restart.

## See also

- [guide.md](guide.md): service setup, auth, backup, troubleshooting.
- [api.md](api.md): HTTP endpoints and integrations.
