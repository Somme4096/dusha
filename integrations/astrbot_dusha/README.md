# AstrBot plugin

This plugin delivers proactive messages from dusha servers to AstrBot. It needs AstrBot 4.8 or later and supports Discord.

The plugin no longer stores conversations, injects chat context, or registers tools. Chat memory comes from the dusha proxy provider, so point AstrBot's chat provider at the dusha proxy (`provider: dusha`) and let the proxy serve the model behind it. Without that provider, AstrBot has no memory and the plugin only sends nudges.

## Install

Start the server, then copy the plugin into AstrBot's plugin directory. From the repository root:

```sh
cp -a integrations/astrbot_dusha \
  /path/to/AstrBot/data/plugins/astrbot_dusha
```

Restart AstrBot or reload the plugin from its manager.

## Settings

Each field's description lives in [`_conf_schema.json`](_conf_schema.json). Every setting sits in one `companions` entry, and the table lists the defaults and the points that need care.

| Field | Default |
| --- | --- |
| `label` | `Companion` |
| `gateway_url` | `http://127.0.0.1:8765` |
| `platform_id` | `""` |
| `config_profile` | `""` |
| `harness` | `astrbot` |
| `api_token` | `""` |
| `poll_interval_seconds` | `30` |
| `proactive_enabled` | `true` |
| `request_timeout_seconds` | `60` |

- `label`: a name for the entry in the AstrBot settings page. It is never sent to the server and never affects routing.
- `platform_id`: run `/sid` on your Discord bot and copy its `Bot ID`. With `config_profile` also set, a chat must match both.
- `config_profile`: an AstrBot config profile ID or name. Leave both `platform_id` and `config_profile` empty to disable the entry.
- `harness`: keep it unique per companion. The server keys proactive events by this name, so a rename orphans the events queued under the old name and the companion stops receiving them.
- `api_token`: the server's API token, when [auth](../../docs/guide.md#auth) is on.
- `poll_interval_seconds` has a minimum of 5.
- `request_timeout_seconds` has a minimum of 1.

## Several companions

Each companion needs its own server, with its own home and port. Then:

1. In AstrBot, route each chat to a config profile, one per companion. Give each profile its own persona, because proactive messages use the persona of the target chat.
2. Add one `companions` entry per companion, with a label, its own `gateway_url`, and a `harness` no other entry uses. Use `config_profile` to send a chat to that entry.

```json
{
  "companions": [
    {
      "label": "Alice",
      "gateway_url": "http://127.0.0.1:8765",
      "config_profile": "Alice profile",
      "harness": "alice-bot"
    },
    {
      "label": "Bruno",
      "gateway_url": "http://127.0.0.1:8767",
      "platform_id": "123456789012345678",
      "config_profile": "Bruno profile",
      "harness": "bruno-bot"
    }
  ]
}
```

The first entry that matches a chat serves it. Each companion polls its own server and acknowledges an event as failed when AstrBot now routes its chat to another companion, so the server offers it to the right one later.

## Behavior

With `proactive_enabled` on, each companion polls one event at a time. It generates a message with the route's persona and the event's context, rendered the way the [client flow](../../docs/api.md#client-flow) renders a turn. It tries the route's chat provider first and then each other provider until one answers, then sends the text, writes it into AstrBot's history after a `[proactive <reason>]` marker, and acknowledges `sent`.

A wrong platform, a missing persona, an empty reply, or a failed send ends in a `failed` acknowledgement, and the server offers the event again later.

## Troubleshooting

Check the AstrBot log for lines that begin with `[dusha]`, and confirm the server answers:

```sh
curl http://127.0.0.1:8765/health
```
