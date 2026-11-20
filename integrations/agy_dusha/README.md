# Antigravity CLI (`agy`) plugin

This plugin connects Antigravity CLI (`agy`) directly to `dusha serve` through lifecycle hooks (`PreInvocation` and `Stop`). It works with the default Google Account OAuth login (including Google AI Pro) without `dusha proxy` or a Gemini API key.

## Install

Start `dusha serve <name>`, then install this directory as an `agy` plugin from the repository root:

```sh
agy plugin install integrations/agy_dusha
```

You can also copy it into your global `agy` plugins folder manually:

```sh
mkdir -p ~/.gemini/config/plugins
cp -a integrations/agy_dusha ~/.gemini/config/plugins/dusha
```

Register the Dusha MCP server so `agy` can call memory, evergreen, memo, and other plugin tools:

```sh
agy mcp add --header "X-Companion-Token: replace-with-dusha-token" \
  dusha http://127.0.0.1:8765/mcp
```

Omit `--header` when server [auth](../../docs/guide.md#auth) is disabled.

## Configuration

The hook uses Python's standard library only and needs no extra packages.

When `DUSHA_URL` is unset, it reads the server URL and token from your companion's `config.json` in the same order as `dusha`:

1. `DUSHA_CONFIG`
2. `DUSHA_HOME`
3. `DUSHA_COMPANION`
4. The sole companion under `$XDG_CONFIG_HOME/dusha` (`~/.config/dusha` by default)

If no companion config is found, it defaults to `http://127.0.0.1:8765` with no token.

| Variable | Default | Description |
| --- | --- | --- |
| `DUSHA_URL` | Auto-discovered, else `http://127.0.0.1:8765` | Explicit Dusha server URL. Skips companion config discovery when set. |
| `DUSHA_TOKEN` | Auto-discovered, else `""` | Server `api_token` sent as `X-Companion-Token`. |
| `DUSHA_COMPANION` | `""` | Companion name under the Dusha config root. Required when several companions exist and `DUSHA_URL` is unset. |
| `DUSHA_HOME` | `""` | Companion home directory containing `config.json`. |
| `DUSHA_CONFIG` | `""` | Explicit path to a companion `config.json`. |
| `DUSHA_HARNESS` | `agy` | Harness label stored on messages and matched against `harnesses.<name>` rules. |
| `DUSHA_PLUGINS` | `""` | Optional comma-separated plugin list (or `none`) sent in `X-Companion-Plugins`. |
| `DUSHA_ROUTE` | `""` | Optional proactive delivery route stored on ingested messages. |
| `DUSHA_TIMEOUT` | `5` | HTTP timeout in seconds for calls to `dusha serve`. |

## Behavior

- **`PreInvocation`**: Ingests the user's prompt through `POST /state/v1/messages` with a turn-scoped `external_id` derived from the transcript step index, then calls `POST /state/v1/context` (`include_recent: false`) and returns the injection as a `systemMessage` step. Later tool-call invocations in the same turn hit server-side deduplication (`duplicate: true`) and return `{}`, keeping the turn's prompt prefix intact.
- **`Stop`**: Reads `PLANNER_RESPONSE` entries produced after the latest `USER_INPUT` in `transcriptPath` (resolving truncated content from `transcript_full.jsonl` when needed) and archives them through `POST /state/v1/messages`.
- **Fail-open**: If `dusha serve` is offline, unreachable, or returns an HTTP error, the hook logs a `[dusha]` warning to `stderr`, prints `{}`, and exits `0` so `agy` is never blocked.

## Troubleshooting

Confirm the server answers:

```sh
curl http://127.0.0.1:8765/health
```

Validate the plugin directory with `agy`:

```sh
agy plugin validate --strict integrations/agy_dusha
```
