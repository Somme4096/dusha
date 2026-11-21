# Configuration

Each companion reads a single `config.json` from its home directory (`~/.config/dusha/<name>/config.json` by default). 

General rules:

- **Strict JSON**: Comments, trailing commas, unknown keys, duplicate keys, wrong value types, and out-of-range numbers fail startup.
- **Defaults**: Omit any key to keep its packaged default.
- **Relative paths**: Paths in `config.json`  resolve relative to the directory containing `config.json`.
- **Permissions**: Keep `config.json` at mode `600` whenever it stores tokens or API keys.
- **Reload after change**: Restart `dusha serve` and `dusha proxy` after editing configuration files.

## Server

Top-level keys configure `dusha serve`:

| Key | Default | Description |
| --- | --- | --- |
| `host` | `"127.0.0.1"` | Bind address for the HTTP server. Overridden by the `DUSHA_HOST`.|
| `port` | `8765` | TCP port (`1` to `65535`). |
| `data_dir` | `"data"` | Directory storing `state.sqlite3`, `outbox.sqlite3`, `cli-manifest.json`, and (optional) `plugins/<name>/`. |
| `timezone` | `"Asia/Taipei"` | IANA timezone used for plugins and message timestamp. |
| `api_token` | `""` | Shared secret token for `/state/v1/*`, `/cli/v1/*`, `/mcp`, and proxy requests. Empty disables authentication, see [guide.md](guide.md#auth). |
| `plugins_dir` | `""` | Directory containing [external plugins](#external-plugins). Empty defaults to `plugins/` inside the companion home. Overridden by `DUSHA_PLUGINS_DIR`. |
| `prompts.path` | `""` | Path to a [`prompts.json`](#promptsjson). Empty uses packaged prompts. |

## Proxy

`dusha proxy` reads the `proxy` section from the same `config.json`:

| Key | Default | Description |
| --- | --- | --- |
| `proxy.host` | `"127.0.0.1"` | Bind address for the proxy. Must be `127.0.0.1`, `::1`, or `localhost` unless a server token is configured. |
| `proxy.port` | `8766` | TCP port (`1` to `65535`). |
| `proxy.server.url` | `""` | Base URL of the Dusha server. When empty (or matching the local server in the same file), uses `host`, `port`, and `api_token` from the same `config.json`. Remote CLI commands use this connection as well. |
| `proxy.server.token` | `""` | The server's `api_token`. Sent by the proxy and CLI as `X-Companion-Token`. |
| `proxy.server.timeout_seconds` | `10` | HTTP timeout in seconds (`> 0`) for calls from the proxy or CLI to the server. |

## Providers

`providers` defines upstream model provider profiles, keyed by `<profile>`. Manage them with `dusha provider <name>` (see [guide.md](guide.md#managing-providers)) or edit `config.json` directly.

- The proxy exposes each profile at `http://127.0.0.1:8766/<profile>`.
- Server plugins can access configured profiles through the `providers` service.
- Profile names **CAN** contain letters, digits, dots, dashes, and underscores, but **CAN NOT** be `health`.

```json
{
  "providers": {
    "openai": {
      "base_url": "https://api.openai.com/v1",
      "key": "{env:OPENAI_API_KEY}"
    },
    "opencode-go": {
      "base_url": "https://opencode.ai/inference/go/openai/v1",
      "endpoints": {
        "messages": "https://opencode.ai/inference/go/anthropic/v1"
      },
      "key": "{env:OPENCODE_API_KEY}",
      "auth": "bearer",
      "headers": {
        "x-opencode-session": "{conversation_id}"
      }
    }
  }
}
```

| Key | Default | Description |
| --- | --- | --- |
| `providers.<profile>.base_url` | *(required)* | Upstream `http://` or `https://` base URL without query parameters or embedded credentials. |
| `providers.<profile>.endpoints` | `{}` | Optional per-protocol base URL overrides, keyed by `chat`, `messages`, or `responses`. Unlisted protocols use `base_url`. |
| `providers.<profile>.key` | `""` | Upstream provider API key. Supports `{env:VAR_NAME}`. The proxy strips caller credential headers and sends this key instead. |
| `providers.<profile>.auth` | `"bearer"` | Upstream authentication header format: `"bearer"` (`Authorization: Bearer <key>`) or `"x-api-key"` (`x-api-key: <key>`). |
| `providers.<profile>.headers` | `{}` | Additional HTTP headers attached to every upstream request. Values support `{env:VAR_NAME}` and `{conversation_id}`. Missing environment variables fail proxy startup. |
| `providers.<profile>.timeout_seconds` | `120` | Upstream request timeout in seconds (`> 0`). |

## Harnesses

`harnesses` define per-harness context injection, keyed by harness name. To set a harness name, see [Connect a harness](guide.md#connect-a-harness).

Harness names **CAN** contain letters, digits, dots, dashes, and underscores.

| Value | Effect |
| --- | --- |
| `["identity", "memory"]` | Inject only the listed plugins. |
| `{"include": ["identity", "memory"]}` | Same as the array form. |
| `{"exclude": ["ambient", "affect"]}` | Inject all active plugins except the listed ones. |

```json
{
  "harnesses": {
    "claude-code": ["identity", "memory", "memo"],
    "codex": {"exclude": ["ambient", "affect"]}
  }
}
```

An explicit `X-Companion-Plugins` header or `plugins` field in `POST /state/v1/context` overrides the harness rule for that single request.

## Embedding

Optional. Setting both `embedding.base_url` and `embedding.model` enables semantic affect appraisal when no external `decide` plugin selects an emotion. Archive message search remains lexical (SQLite FTS5).

| Key | Default | Description |
| --- | --- | --- |
| `embedding.base_url` | `""` | OpenAI-compatible base URL. The server appends `/embeddings`. |
| `embedding.api_key_env` | `"EMBEDDING_API_KEY"` | Environment variable name read for the Bearer token. |
| `embedding.model` | `""` | Embedding model identifier. |
| `embedding.dimensions` | `null` | Optional output vector dimension (`>= 1`) passed to the endpoint. |
| `embedding.timeout_seconds` | `10` | Request timeout in seconds (`> 0`). |
| `embedding.batch_size` | `32` | Maximum inputs per embedding request (`>= 1`). |
| `embedding.failure_cooldown_seconds` | `60` | Seconds (`>= 0`) to pause semantic appraisal after an endpoint failure. |

## Plugins

Every companion feature is a plugin configured under `plugins.<name>`. See [plugins.md](plugins.md) for details.

- Only plugins listed under `plugins` in `config.json` are loaded (both built-in and external). 
- Plugins run and inject their context blocks in the **EXACT** order they appear in `plugins`.
- Remove a plugin from `plugins` to skip loading it altogether, or set `"disabled": true` inside `plugins.<name>` (or `"plugins": {"<name>": false}`) to turn it off while keeping its settings in `config.json`. 
- Disabled plugins are excluded from context injection, their `/state/v1/<name>/*` routes return `503`, and their CLI commands are hidden, while existing database rows remain intact on disk.
- A plugin whose required `inject` service is unavailable will be automatically disabled.
  -  i.e., `proactive` requires `affect`. Without `affect`, `proactive` will be disabled even with configuration set and `"disabled": false`.

```json
{
  "plugins": {
    "memo": {"disabled": true},
    "system_one": {"options": {"base_url": "http://127.0.0.1:8000"}}
  }
}
```

### `identity`

| Key | Default | Description |
| --- | --- | --- |
| `plugins.identity.path` | *(unset)* | Path to a UTF-8 Markdown file prepended verbatim to every context build. Omit `path` to use `identity.md` in the companion home when present. Setting `"path": ""` fails startup unless `"disabled": true` is set. |

### `memory`

| Key | Default | Description |
| --- | --- | --- |
| `plugins.memory.recent_messages` | `8` | Number of recent conversation messages (`>= 0`) included in context when `include_recent` is `true`. |
| `plugins.memory.search_hits` | `4` | Maximum lexical search hits (`>= 0`) included in each context build. |
| `plugins.memory.context_messages` | `1` | Neighboring messages (`>= 0`) included before and after each search hit. |

### `evergreen`

| Key | Default | Description |
| --- | --- | --- |
| `plugins.evergreen.max_items` | `32` | Maximum active facts (`>= 0`) injected into context, ordered by priority. |
| `plugins.evergreen.max_chars` | `4000` | Maximum character budget (`>= 0`) for the `<evergreen_facts>` block. |

### `memo`

| Key | Default | Description |
| --- | --- | --- |
| `plugins.memo.max_items` | `10` | Maximum active memos (`0` to `500`) injected into context, oldest first. |
| `plugins.memo.max_chars` | `2000` | Maximum character budget (`>= 0`) for the `<memo_notes>` block. |
| `plugins.memo.text_max_chars` | `4000` | Maximum characters (`>= 1`) accepted when creating a memo. |
| `plugins.memo.reason_max_chars` | `1000` | Maximum characters (`>= 1`) accepted when archiving a memo. |

### `affect`

| Key | Default | Description |
| --- | --- | --- |
| `plugins.affect.emotions.path` | `""` | Path to a custom [`emotions.json`](#emotionsjson) file. Empty uses the packaged file. |
| `plugins.affect.emotions.expected_version` | `""` | Required `emotion_version` string in the emotions file. Empty accepts any version. |
| `plugins.affect.increment` | `null` | Overrides `decision.increment` (`> 0`). Per-dimension `increment` values still take precedence. |
| `plugins.affect.mood_follow_hours` | `12.0` | Overrides `affect.mood_follow_hours` (`> 0`). |
| `plugins.affect.mood_return_hours` | `72.0` | Overrides `affect.mood_return_hours` (`> 0`). |
| `plugins.affect.dimensions` | `{}` | Per-dimension overrides for existing dimensions (`neutral`, `floor`, `tau`, `increment`). |
| `plugins.affect.silence` | `{}` | Per-dimension overrides for `silence.rules` (`rate_per_hour`, `cap`, `gate_hours`). |

### `proactive`

| Key | Default | Description |
| --- | --- | --- |
| `plugins.proactive.poll_interval_seconds` | `60` | Background evaluation interval in seconds (`>= 1`). |
| `plugins.proactive.min_silence_minutes` | `180` | Minimum minutes (`>= 0`) since the last user message before a proactive event can trigger. |
| `plugins.proactive.cooldown_minutes` | `360` | Minimum minutes (`>= 0`) between proactive sends. |
| `plugins.proactive.max_per_day` | `2` | Maximum proactive messages (`>= 0`) sent per local calendar day in `timezone`. |
| `plugins.proactive.max_unanswered` | `2` | Maximum consecutive unanswered proactive messages (`>= 0`) before evaluation pauses. |
| `plugins.proactive.quiet_start_hour` | `1` | Local hour (`0` to `23`) when quiet hours begin. |
| `plugins.proactive.quiet_end_hour` | `8` | Local hour (`0` to `23`) when quiet hours end. |
| `plugins.proactive.lease_seconds` | `120` | Seconds (`>= 1`) a polled event stays leased to a consumer before returning to pending. |
| `plugins.proactive.retry_delay_minutes` | `15` | Minutes (`>= 0`) to wait before re-queuing an event acknowledged as `failed`. |
| `plugins.proactive.thresholds` | `{}` | Overrides trigger thresholds in `emotions.json`, keyed by dimension name. |
| `plugins.proactive.target` | `"latest"` | Target conversation for proactive events: `"latest"`, `{"harness": "<name>"}`, or `{"harness": "<name>", "conversation_id": "<id>"}`. |

### External plugins

Any key under `plugins` that is not a built-in plugin name loads the external plugin directory `<plugins_dir>/<name>/`. The directory name must start with a letter or underscore and contain only letters, digits, and underscores. See [plugins.md](plugins.md#make-your-own-plugin) for creating external plugins.

| Key | Default | Description |
| --- | --- | --- |
| `plugins.<name>.disabled` | `false` | Disable the plugin when `true`. |
| `plugins.<name>.options` | `{}` | Arbitrary JSON object passed as `config` to the plugin's `apply(ctx, config)` function. |

> [!WARNING]
> External plugins execute inside the `dusha serve` Python process with full server permissions. Only load plugins you trust.

## `emotions.json`

The default `affect` plugin loads its emotion dimensions, decay constants, silence rules, and proactive triggers from [`src/dusha/resources/emotions.json`](../src/dusha/resources/emotions.json) (16 dimensions by default).

To inspect and/or customize the full file, download the default emotions.json, and set `plugins.affect.emotions.path` in `config.json`:

```sh
curl -fsSL -O https://raw.githubusercontent.com/Somme4096/dusha/main/src/dusha/resources/emotions.json
```

| Section | Description |
| --- | --- |
| `schema_version` | Must be `3`. |
| `emotion_version` | Non-empty version string checked against `plugins.affect.emotions.expected_version`. |
| `value_range` | Global `min` and `max` bounds for all emotion values (`min < max`). |
| `dimensions` | Map of dimension names to `{neutral, floor, tau, description?, increment?}` (`floor <= neutral`, `tau > 0` in hours). |
| `decision` | `{"increment": <float>}`, the default step added when a decision selects a dimension. |
| `negative_dimensions` | Array of dimension names that negative deltas cannot push below their current `mood`. |
| `silence.rules` | Drift rules during user silence, keyed by dimension: `{rate_per_hour, cap, gate_hours?}`. |
| `proactive_sent_deltas` | Map of dimension deltas applied immediately after a proactive message is sent. |
| `impact_scale` | Positive multiplier applied to incoming `deltas`. |
| `mood_follow_gain` | `{min, max, factor}` controlling how fast `mood` tracks `base`. |
| `affect` | `{mood_follow_hours, mood_return_hours}` time constants (`> 0`). |
| `prompt` | Controls which feelings appear on the injected Affect line: `{top_n, deviation_threshold, level_high, level_elevated, always_show}`. |
| `proactive.triggers` | Ordered list of `{dimension, threshold, reason}`. The first trigger at or above `threshold` initiates a proactive event. |
| `appraisal` | Semantic appraisal settings used when [`embedding`](#embedding) is enabled: `{min_similarity, fallback_label, prototypes}`. |

### Override in `config.json`

You can patch individual parameters in `config.json` under [`plugins.affect`](#affect) and [`plugins.proactive.thresholds`](#proactive) without maintaining a separate `emotions.json` file:

```json
{
  "plugins": {
    "affect": {
      "mood_follow_hours": 12,
      "mood_return_hours": 72,
      "increment": 0.1,
      "dimensions": {"fear": {"neutral": 0.05}},
      "silence": {"longing": {"rate_per_hour": 0.02, "cap": 0.5}}
    },
    "proactive": {
      "thresholds": {"longing": 0.4}
    }
  }
}
```

### Precedence for affect overrides

Precedence from highest to lowest:

1. `config.json` overrides (`plugins.affect.*` and `plugins.proactive.thresholds`)
2. Custom file at `plugins.affect.emotions.path`
3. Packaged `emotions.json`

## `prompts.json`

All prompts and XML-style block delimiters can be configured in [`src/dusha/resources/prompts.json`](../src/dusha/resources/prompts.json). This includes both built-in plugins. External plugins are not required to follow this convention, but they are strongly encouraged to do so. To override specific slots, set `prompts.path` in `config.json` to another JSON file.

> [!NOTE]
> Prompt overrides for disabled or unconfigured plugins **CAN** remain in `prompts.json`.

You may download default prompts.json for built-in plugins as a reference:

```sh
curl -fsSL -O https://raw.githubusercontent.com/Somme4096/dusha/main/src/dusha/resources/prompts.json
```

Remove `schema_version` and `prompts_version` when using the file as an overlay, and include only the slots you want to replace:

```json
{
  "companion_state": {
    "open_delimiter": "<companion_state>\n",
    "close_delimiter": "\n</companion_state>"
  },
  "plugins": {
    "affect": {
      "decision_instruction": "Pick the single emotion the message stirs."
    },
    "memo": {
      "open_delimiter": "<memo_notes>",
      "close_delimiter": "</memo_notes>"
    }
  }
}
```

Overlay rules:

- `companion_state` and each `plugins.<name>.<slot>` are replaced as complete units.
-  Any slot included in the overlay must contain all required keys of that slot.
- Omitted slots keep their defaults.


| Slot | Description |
| --- | --- |
| `companion_state` | `{open_delimiter, close_delimiter}` wrapping the `<companion_state>` JSON block. |
| `plugins.memory.instruction` | Instruction string placed in `<companion_state>` when `memory` is active. |
| `plugins.evergreen` | `{open_delimiter, close_delimiter}` wrapping the `<evergreen_facts>` block. |
| `plugins.memo` | `{open_delimiter, close_delimiter}` wrapping the `<memo_notes>` block. |
| `plugins.affect.presentation` | `{prefix, separator, suffix, baseline, level_connector, level_high, level_elevated, level_noticeable}` formatting the Affect status line. |
| `plugins.affect.block` | `{open_delimiter, close_delimiter, instruction}` wrapping the `<affect>` block. |
| `plugins.affect.decision_instruction` | Prompt instruction passed to `decide` listeners. |
| `plugins.proactive.framing` | `{ruling, ladder, approach, context_query}` used when assembling a proactive event. |
| `plugins.proactive.generation_instruction` | `{base, variants}` instructions rotated across proactive events. |

Template placeholders in `plugins.proactive.framing`:

- `ruling`: supports `{dimension}`, `{value}`, `{neutral}`, and `{deviation}`.
- `approach`: supports `{variant}`.
- `ladder`: non-empty list of strings indexed by `unanswered_proactive` count (the last entry covers all higher counts).
- Escape literal braces as `{{` and `}}`. Unknown placeholders fail startup.

## See also

- [guide.md](guide.md): CLI usage, harness setup, deployment, authentication, and migration.
- [plugins.md](plugins.md): Built-in plugins and the Plugin API.
- [api.md](api.md): HTTP endpoints, proxy routing, and MCP protocol.
