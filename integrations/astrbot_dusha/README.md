# AstrBot plugin

This plugin connects AstrBot to the dusha server. It needs AstrBot 4.8 or later and supports Discord.

## Install

Start the server, then copy the plugin into AstrBot's plugin directory. From the repository root:

```sh
cp -a integrations/astrbot_dusha \
  /path/to/AstrBot/data/plugins/astrbot_dusha
```

Restart AstrBot or reload the plugin from its manager. Keep AstrBot's chat provider pointed at your model provider, not at the server.

## Settings

Each field's description lives in [`_conf_schema.json`](_conf_schema.json). The table lists the defaults and the points that need care.

| Field | Default |
| --- | --- |
| `gateway_url` | `http://127.0.0.1:8765` |
| `platform_id` | `""` |
| `config_profile` | `""` |
| `api_token` | `""` |
| `harness` | `astrbot` |
| `request_timeout_seconds` | `60` |
| `poll_interval_seconds` | `30` |
| `proactive_enabled` | `true` |
| `proactive_tool_keywords` | `["donsetch", "fetch"]` |
| `proactive_request_hooks` | `["astrbot_plugin_opencode_go_session"]` |
| `bindings` | `[]` |

- `platform_id`: run `/sid` on your Discord bot and copy its `Bot ID`. With `config_profile` also set, a chat must match both. With both empty, the settings serve no chat.
- `api_token`: the server's API token, when [auth](../../docs/guide.md#auth) is on.
- `harness`: a rename starts each chat with empty recent history, and the plugin stops receiving proactive events queued under the old name.
- `request_timeout_seconds` has a minimum of 1. Message storage runs plugin hooks in the server process, so raise it if storage is slow.
- `poll_interval_seconds` has a minimum of 5.
- `proactive_tool_keywords`: AstrBot tools whose names contain a keyword join proactive generation.

## Several companions

Each companion needs its own server, with its own home and port. Then:

1. In AstrBot, route each chat to a config profile, one per companion. Give each profile its own persona, because proactive messages use the persona of the target chat.
2. Add one `bindings` entry per companion, with that profile in `config_profile` and that server in `gateway_url`.

```json
{
  "bindings": [
    {
      "gateway_url": "http://127.0.0.1:8767",
      "config_profile": "work",
      "harness": "astrbot-work"
    }
  ]
}
```

The first entry that matches a chat serves it. The top-level settings serve chats that no entry matches. Each companion polls its own server, and it acknowledges an event as failed when AstrBot now routes its chat to another companion.

## Tools

| Tool | Arguments | Calls |
| --- | --- | --- |
| `search_conversation_memory` | `query`, `limit` | `POST /state/v1/memory/search` |
| `get_conversation_record` | `memory_id`, `context_messages` | `GET /state/v1/memory/{id}` |
| `remember_evergreen_fact` | `key`, `text`, `priority`, `review_after`, `expires_at`, `reason` | `POST /state/v1/evergreen/facts` |
| `revise_evergreen_fact` | `fact_id`, `expected_revision`, `text`, `priority`, `review_after`, `expires_at`, `reason` | `POST /state/v1/evergreen/facts/{id}/revisions` |
| `forget_evergreen_fact` | `fact_id`, `expected_revision`, `reason` | `POST /state/v1/evergreen/facts/{id}/forget` |
| `review_evergreen_facts` | `due_only`, `include_inactive`, `limit` | `GET /state/v1/evergreen/facts` |
| `yumecho_add` | `text` | `POST /state/v1/memo/add` |
| `yumecho_list` | `status`, `limit` | `GET /state/v1/memo/list` |
| `yumecho_done` | `note_id`, `reason` | `POST /state/v1/memo/{id}/done` |

Tools return JSON with `ok: true`, or `ok: false` plus `error` and, for HTTP failures, `status`.

## Behavior

Each turn follows the [client flow](../../docs/api.md#client-flow): the plugin stores the user message, requests context, and archives the assistant reply. Two details differ.

The rendered context goes into two parts of the user message. Facts, memos, the affect block, external plugin blocks, and recalled records go into a part that AstrBot marks provider-only. AstrBot sends it with the request and keeps it out of saved history. On the first turn of a conversation, the plugin also saves recent records once as a `<previous_session>` part, which bridges the previous session. Later turns skip them, because AstrBot's history holds them. AstrBot releases without provider-only parts get the text in the system prompt instead.

If the store call fails, the plugin logs `Message archive failed` and still requests context. If the context call fails, it logs `Context unavailable` and sends the request without an injection.

With `proactive_enabled` on, the plugin polls one event at a time. It generates a message with the route's persona, the event's context rendered the same way, and the tools above. It tries each configured provider until one answers, then sends the text, writes it into AstrBot's history after a `[proactive <reason>]` marker, and acknowledges `sent`. A wrong platform, a missing persona, an empty reply, or a failed send ends in a `failed` acknowledgement, and the server offers the event again later.

## Troubleshooting

Check the AstrBot log for lines that begin with `[dusha]`, and confirm the server answers:

```sh
curl http://127.0.0.1:8765/health
```
