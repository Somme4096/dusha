# Configuration

Each companion reads one `config.json` from its home directory. `dusha serve <name>` writes the file on the first run:

```sh
dusha serve dusha
${EDITOR:-vi} ~/.config/dusha/dusha/config.json
```

The generated file matches [config.example.json](../config.example.json) and lists every server key. Its mode is `600`, readable only by you. Omit a key to keep its default. Restart the service after an edit.

## Which file loads

A companion home is `$XDG_CONFIG_HOME/dusha/<name>/`, or `~/.config/dusha/<name>/` when `XDG_CONFIG_HOME` is unset. `dusha serve`, `dusha proxy`, and every other command take the first match:

1. `--config PATH`. The file's directory becomes the home.
2. `--home DIR`
3. `--companion NAME` (`-c NAME`), or the name after `dusha serve` or `dusha proxy`
4. `DUSHA_HOME`
5. `DUSHA_COMPANION`
6. The single companion under the config root
7. Packaged defaults, when no companion exists

Startup fails when several companions exist and you select none, or when the selected home has no `config.json`. A name holds letters, digits, dots, dashes, and underscores. The exception is the first run of `dusha serve <name>` or `dusha proxy <name>`, which writes the file.

Every command other than `serve`, `proxy`, and `list` talks to the companion's server over HTTP. It takes `proxy.server` from the selected file, and when `proxy.server.url` is empty, the server keys of the same file: `host`, `port`, and `api_token`.

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
| `api_token` | `""` | The API token. Empty turns auth off. See [Auth](guide.md#auth). |

`upstream` and `api_openai` moved to [proxy.upstreams](#proxy). Startup rejects them.

## Proxy

`dusha proxy <name>` reads the `proxy` section of the same `config.json`, and `dusha serve` ignores it. When `<name>` has no `config.json` on the machine, the first run asks for the server's URL and token and writes a file that holds only this section, with mode `600`. Add your profiles under `proxy.upstreams` afterwards. On the server's own machine, add the section to the existing file by hand.

| Key | Default | Notes |
| --- | --- | --- |
| `proxy.host` | `127.0.0.1` | Must be a loopback address. The proxy has no auth of its own. |
| `proxy.port` | `8766` | |
| `proxy.server.url` | `""` | The server's base URL. Empty uses the server in the same file, with its `host`, `port`, and `api_token`. The other `dusha` commands use it too. |
| `proxy.server.token` | `""` | The server's `api_token`. The file then holds a secret. The first run writes it with mode `600`, and a file you edit by hand needs `chmod 600`. |
| `proxy.server.timeout_seconds` | `10` | |
| `proxy.upstreams.<name>.base_url` | | Provider base URL, exactly as the harness would use it. |
| `proxy.upstreams.<name>.headers` | `{}` | Headers set on every forwarded request. Values can hold `{conversation_id}`, the matched conversation's name, and `{env:NAME}`. A missing variable fails startup. |
| `proxy.upstreams.<name>.timeout_seconds` | `120` | |

A profile name holds letters, digits, dots, dashes, and underscores, and cannot be `health`. See [Proxy](api.md#proxy) for how requests are routed.

## Plugins

Every part of the companion is a plugin under `plugins`, keyed by name. The built-in plugins are `identity`, `memory`, `evergreen`, `memo`, `affect`, and `proactive`. Any other name is an [external plugin](#external-plugins), which lives in a directory under `plugins_dir` and is imported into the gateway process. Messages are always stored. Earlier releases called these mods, and the old `mods_dir` key and `DUSHA_MODS_DIR` variable are no longer accepted.

- Plugins run in the order you list them, which is also the order of their blocks in the injection. Built-in plugins you leave out keep their packaged order ahead of the ones you list.
- `"disabled": true` turns a plugin off and keeps the rest of its settings. A plugin that is off leaves the injection, its routes return `503`, and its commands exit with `1`. Stored rows stay on disk.
- A plugin that needs a service another plugin provides stays inactive while that plugin is off. `proactive` needs `affect`.

```json
{
  "plugins": {
    "memo": {"disabled": true},
    "system_one": {"options": {"base_url": "http://127.0.0.1:8000"}}
  }
}
```

## Identity, emotions, and prompts

| Key | Default | Notes |
| --- | --- | --- |
| `plugins.identity.path` | unset | Markdown file. The service prepends it verbatim to every context. Leave the key out to use `identity.md` next to `config.json`, when that file exists. An empty value fails startup unless `plugins.identity.disabled` is `true`. |
| `plugins.affect.emotions.path` | `""` | Custom emotions file. Empty uses the packaged one. |
| `plugins.affect.emotions.expected_version` | `""` | Startup fails unless the file's `emotion_version` matches. Empty accepts any version. |
| `prompts.path` | `""` | Overlay for the packaged prompt text. |

A path that points at a missing file fails startup.

Export the packaged files, then edit the copies:

```sh
python -c "import json, dusha.emotions as e; json.dump(e.default_emotions(), open('emotions.json','w'), ensure_ascii=False, indent=2)"
python -c "import json, dusha.prompts as p; d=p.default_prompts(); d.pop('schema_version'); d.pop('prompts_version'); json.dump(d, open('prompts.json','w'), ensure_ascii=False, indent=2)"
```

```json
{
  "plugins": {
    "identity": {"path": "identity.md"},
    "affect": {"emotions": {"path": "emotions.json", "expected_version": "0.4.0"}}
  },
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
  "plugins": {
    "affect": {
      "mood_follow_hours": 12,
      "mood_return_hours": 72,
      "increment": 0.1,
      "dimensions": {"fear": {"neutral": 0.05}},
      "silence": {"longing": {"rate_per_hour": 0.02}}
    },
    "proactive": {"thresholds": {"longing": 0.4}}
  }
}
```

| Key | Patches |
| --- | --- |
| `plugins.affect.increment` | `decision.increment`, the step a decision adds. A per-dimension `increment` still wins. |
| `plugins.affect.dimensions` | `neutral`, `floor`, `tau`, or `increment` of an existing dimension. |
| `plugins.affect.silence` | A silence rule. A new dimension name adds a rule. |
| `plugins.proactive.thresholds` | The trigger threshold for that dimension. The dimension needs a trigger in the file. |

An override beats `plugins.affect.emotions.path`, and that file beats the packaged one.

### prompts.json

The overlay replaces whole slots. A slot you include must carry every key of that slot. Omitted slots keep the packaged text.

| Slot | Holds |
| --- | --- |
| `affect_presentation` | Wording of the Affect line. |
| `companion_state` | Delimiters and the memory instruction for the state block. |
| `affect` | Delimiters and the instruction for the affect block. |
| `evergreen` | Delimiters for the fact block. |
| `context_blocks` | Delimiters for the memo and plugin blocks. |
| `proactive_framing` | `ruling`, `ladder`, `approach`, and `context_query` for proactive messages. |
| `proactive_generation_instruction` | `base` plus `variants`. The gateway rotates one variant per send. |
| `decision_instruction` | Selection criteria sent with every `decide` event. |

Placeholders in `proactive_framing`:

- `ruling` accepts `{dimension}`, `{value}`, `{neutral}`, and `{deviation}`.
- `approach` accepts `{variant}`.
- `ladder` holds one line per unanswered count. The last line covers higher counts.
- Write a literal brace as `{{` or `}}`. An unknown placeholder fails startup.

## Memory and context

Search is lexical and uses SQLite FTS5.

| Key | Default | Notes |
| --- | --- | --- |
| `plugins.memory.recent_messages` | `8` | Recent messages in the injection. |
| `plugins.memory.search_hits` | `4` | |
| `plugins.memory.context_messages` | `1` | Neighbors added around each search hit. |
| `plugins.evergreen.max_items` | `32` | |
| `plugins.evergreen.max_chars` | `4000` | |
| `plugins.memo.max_items` | `10` | Memos per injection. |
| `plugins.memo.max_chars` | `2000` | Size of the memo block. |
| `plugins.memo.text_max_chars` | `4000` | Longest memo the API accepts. |
| `plugins.memo.reason_max_chars` | `1000` | Longest archive reason the API accepts. |

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
| `embedding.failure_cooldown_seconds` | `60` | Pause after a failed request. |

## External plugins

An external plugin is a directory under `plugins_dir` with `pyproject.toml` and a `[tool.dusha]` table. The gateway imports it into its own process. The [README](../README.md#plugin-api) describes the contract, and [examples/plugins](../examples/plugins/README.md) holds two working external plugins.

| Key | Default | Notes |
| --- | --- | --- |
| `plugins_dir` | `""` | Empty uses `plugins` in the companion home. `DUSHA_PLUGINS_DIR` overrides it. |

An external plugin's entry under `plugins` is keyed by its directory name and takes these keys:

| Key | Default | Notes |
| --- | --- | --- |
| `options` | `{}` | Passed to the plugin's `apply` as `config`. |
| `disabled` | `false` | |

External plugins run inside the gateway process with its permissions and dependencies. Install only plugins you wrote or reviewed. A plugin dependency conflict affects the whole gateway.

## Proactive

| Key | Default | Notes |
| --- | --- | --- |
| `plugins.proactive.poll_interval_seconds` | `60` | |
| `plugins.proactive.min_silence_minutes` | `180` | |
| `plugins.proactive.cooldown_minutes` | `360` | Gap after a send. |
| `plugins.proactive.max_per_day` | `2` | |
| `plugins.proactive.max_unanswered` | `2` | Sends stop after this many ignored messages. |
| `plugins.proactive.quiet_start_hour` | `1` | Local hour. |
| `plugins.proactive.quiet_end_hour` | `8` | Local hour. |
| `plugins.proactive.lease_seconds` | `120` | Time a harness holds a polled event before it returns to pending. |
| `plugins.proactive.retry_delay_minutes` | `15` | Wait after a failed delivery. |

The emotional triggers live in [emotions.json](#emotionsjson). Limits survive a restart.

## See also

- [guide.md](guide.md): service setup, auth, backup, troubleshooting.
- [api.md](api.md): HTTP endpoints and integrations.
