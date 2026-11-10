# Plugins

Every part of the companion is a plugin. The built-in ones are `identity`, `memory`, `evergreen`, `memo`, `affect`, `proactive`, and `relay`. This page covers what each one does, the CLI commands and MCP tools it adds, and the services it offers to other plugins. For the config keys see [configuration.md](configuration.md#plugins), for the routes see [api.md](api.md#plugins), and for writing your own see [Plugin API](#plugin-api).

## Built-in plugins

`relay` stays off until `plugins` lists it. The rest are on unless you disable them. Each one is a module in `src/dusha/` with the same name.

### identity

Prepends `identity.md`, or the file at `plugins.identity.path`, verbatim to every context. It has no commands, tools, or services, and it adds no part when the file is empty.

### memory

Stores every message and builds the injection from recent messages and search hits. Search is lexical over SQLite FTS5, with neighbors around each hit. It offers the `messages` service.

| Command | Does |
| --- | --- |
| `dusha memory search <query> [--limit]` | Search the archive |
| `dusha memory show <message_id>` | Show one message |
| `dusha memory recent [--limit]` | Show the latest messages |
| `dusha memory reindex` | Rebuild the search index |

| Tool | Does |
| --- | --- |
| `memory_search` | Search the archive, with `context_messages` neighbors per hit |
| `memory_get` | Read one message with its neighbors |

### evergreen

Long-term facts under a stable key, with revisions, priority, review times, and expiry. No automatic extraction: the companion edits them through the tools. It offers the `evergreen` service.

| Command | Does |
| --- | --- |
| `dusha evergreen list` | List current facts by priority |
| `dusha evergreen history <fact_id>` | List every revision of a fact |
| `dusha evergreen remember <key> <text>` | Remember a fact under a stable key |
| `dusha evergreen revise` | Replace a fact at the revision you last read |
| `dusha evergreen forget <fact_id> <expected_revision> --reason` | Forget a fact at the revision you last read |

| Tool | Does |
| --- | --- |
| `evergreen_list` | List evergreen facts |
| `evergreen_remember` | Store a new fact |
| `evergreen_revise` | Replace a fact at `expected_revision` |
| `evergreen_forget` | Forget a fact at `expected_revision` |

### memo

Short notes the companion keeps in view until it finishes them. It offers the `memo` service.

| Command | Does |
| --- | --- |
| `dusha memo add <text>` | Add a memo |
| `dusha memo list` | List memos, oldest first |
| `dusha memo done <note_id> <reason>` | Archive a memo with the reason it is done |

| Tool | Does |
| --- | --- |
| `memo_add` | Add a memo |
| `memo_list` | List memos |
| `memo_done` | Archive a memo |

### affect

A deterministic emotion engine over the dimensions in `emotions.json`. It moves on each user message, decays over time, and reacts to silence. A `decide` plugin can pick the emotion for a message, and embedding semantic appraisal does it when no plugin does. It offers the `affect` service.

| Command | Does |
| --- | --- |
| `dusha affect show` | Show the affect state and advance decay |

| Tool | Does |
| --- | --- |
| `affect_status` | Read the affect state |

### proactive

Decides when the companion speaks first, from the affect state, silence, quiet hours, cooldown, and the daily cap. It queues events, and a consumer leases one, sends it, and acknowledges it. It needs `affect`. It offers the `proactive` service.

| Command | Does |
| --- | --- |
| `dusha proactive evaluate` | Run one evaluation pass |
| `dusha proactive poll` | Lease pending events |
| `dusha proactive ack <event_id> <outcome>` | Acknowledge a leased event |

| Service method | Does |
| --- | --- |
| `evaluate()` | Run one pass, returns the queued event or `None` |
| `poll(consumer, limit)` | Lease pending events |
| `acknowledge(event_id, consumer, outcome)` | Report `sent`, `failed`, or `release` |

### relay

Reaches the user's other devices through their own plugins: presence, screenshot, notification, spawn, music, and hands. It checks presence first, so `auto` picks the device in use. It also adds `<local_time>` to the context, mirrors proactive messages to the active device, and serves the `relay.endpoint` event.

| Tool and command | Does |
| --- | --- |
| `look` | See what's on the screen of one device |
| `notify` | Show a notification on one device |
| `spawn`, `spawn_status`, `spawn_wait` | Start a coding agent on a device and fetch its answer |
| `music` | See or control music: status, play, pause, toggle, next, previous, stop, volume, find, album |
| `shell`, `type_text`, `press_key`, `list_windows` | Hands on one device |
| `speak` | Say something out loud in the companion's voice |

Each takes `--device`, or `device` on MCP, defaulting to `auto`. The commands are `dusha relay <name>`, with underscores written as dashes, such as `dusha relay spawn-status`.

| Key | Default | Notes |
| --- | --- | --- |
| `plugins.relay.token` | `""` | One token for every device. |
| `plugins.relay.tokens` | `{}` | A token per device, which overrides `token`. |
| `plugins.relay.timeout_seconds` | `60` | |
| `plugins.relay.mirror_proactive` | `true` | Mirror proactive messages to the active device. |
| `plugins.relay.mirror_title` | `dusha` | |
| `plugins.relay.spawn_timeout_seconds` | `1800` | |
| `plugins.relay.spawn_poll_seconds` | `2` | |
| `plugins.relay.mirror_voice` | `false` | |
| `plugins.relay.speak_order` | `presence`, `discord` | Routes for `speak`, in order. |
| `plugins.relay.speak_route` | `""` | One route instead of the order. |
| `plugins.relay.speak_timeout_seconds` | `180` | |
| `plugins.relay.speak_help` | `""` | Replaces the tool description of `speak`. |

## External plugins

An external plugin is a directory under `plugins_dir` with `pyproject.toml` and a `[tool.dusha]` table. The gateway imports it into its own process. [dusha-plugin-collections](https://github.com/Somme4096/dusha-plugin-collections) has working ones, such as `system_one` and `everos_memory`.

| Key | Default | Notes |
| --- | --- | --- |
| `plugins_dir` | `""` | Where external plugins live. Empty uses `plugins` in the companion home. `DUSHA_PLUGINS_DIR` overrides it. |

## Plugin API

> [!NOTE]
> This adds detail to the [Plugin](https://github.com/Somme4096/dusha#plugin) table in the README.

The module defines `apply(ctx, config)`, which runs at startup with the plugin's `options` from `config.json`. `inject` lists the services the plugin calls, and an external plugin that's not in `config.json` stays inactive. Also note that external plugins share the gateway's Python environment, so install their dependencies in it if you use editable install for dev purposes

`inject` also takes `api`, `cli`, and `mcp` when the plugin registers routes, commands, or tools.

| Hook | Receives |
| --- | --- |
| `message` | The stored row: `id`, `conversation_id`, `role`, `text`, `content`, `external_id`, `occurred_at`, `ingested_at`, `harness`, `external_conversation_id`, `route`. An exception is logged and the message is not redelivered. |
| `claim` | A user message about to be stored: `harness`, `conversation_id`, `text`, `route`. A non-null answer stores it as an `event`, so `message` listeners such as affect and proactive treat it as no user turn. |
| `context` | `query`, `harness`, `conversation_id`, `exclude_message_ids`, and `include_recent`. A returned `Block` carries `name`, `text`, `data`, `session` records, and an `instruction` for the state block. |
| `decide` | `message`, `emotions` (the resolved dimensions), `state` (the affect snapshot), and `instruction`. |
| `<event>` | The arguments passed by `emit` or `collect`. Custom events stay in the gateway process, so they reach every active plugin, including the emitter's own listeners. |

`ctx.data_dir` is `plugins/<plugin>` under `data_dir`. `ctx.collect` keeps each plugin's first non-null result.

| Service | Methods |
| --- | --- |
| `messages` | `get`, `recent`, `search`, `context`, `after` |
| `companion` | `ingest_message`, `ingest_transcript`, `build_context`, `latest_route` |
| `affect` | `status` |
| `evergreen` | `remember`, `revise`, `forget`, `list_current`, `history` |
| `memo` | `add`, `list`, `done` |
| `proactive` | `evaluate`, `poll`, `acknowledge` |
| `providers` | `names`, `get(name)`. A provider has `url(kind)` and `request_headers()`, with the key and `{env:NAME}` filled in. |

Services from `ctx.provide` are plain Python objects. `deltas` apply after the increment, and dimension names come from your `emotions.json`.
One thing to pay attention to is that a dependency conflict in plugin(whether internal or external) makes dusha unable to start gateway, since they share one Python environment.
