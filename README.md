# Companion State Gateway

A small Python service that adds durable raw memory, persistent affect, and proactive-message events to an LLM companion. Personality stays in the harness. One SQLite file owns the companion state, so replacing AstrBot does not require a migration.

The service adapts Omemo's OpenAI-compatible proxy boundary and Drivesoid v2.0.0's affect dynamics. It has no WebUI, vector database, task broker, or generative memory pass.

## What it stores

Every message keeps its original JSON content, extracted text, timestamp, source, conversation, SHA-256 digest, and optional source message ID. SQLite FTS5 is a rebuildable index. Search results include the original hit and adjacent original messages.

Affect has fast `base` values and slower `mood` values for:

`vitality`, `fatigue`, `longing`, `intimacy`, `possessiveness`, `lust`, `jealousy`, `anxiety`, `protectiveness`, `fear`, `contentment`, `elation`, `seeking`, `play`, `dejection`, and `irritability`.

Supported event labels include `fear_separation`, `fear_death`, `fear_concern`, and `fear_general`. Phrase matching is deterministic and configurable. Harnesses and the CLI can submit an explicit label when wording alone is not enough.

## Install and run

```sh
cp config.example.yaml config.yaml
mise exec -- uv sync
export UPSTREAM_API_KEY='replace-me'
mise exec -- uv run companion-gateway serve
```

Check it:

```sh
curl http://127.0.0.1:8765/health
```

The OpenAI-compatible base URL is `http://127.0.0.1:8765/v1`. A generic client should send these headers so state remains separated and routable:

```text
X-Companion-Id: sophia
X-Conversation-Id: stable-conversation-id
X-Harness: client-name
X-Companion-Route: opaque-return-address
```

The proxy accepts streaming and non-streaming chat completions. It forwards the caller's `Authorization` header unless `upstream.api_key_env` supplies a key.

## AstrBot and Discord

Install the directory `integrations/astrbot_companion_gateway` as an AstrBot plugin and set its gateway URL and companion ID. Keep AstrBot's model provider pointed at the real upstream provider. The plugin supplies stable Discord session IDs, records both sides of each exchange, and injects state through AstrBot's request hook.

The plugin polls pending events with `harness=astrbot`. It loads the active AstrBot persona, asks the configured provider for one message, sends through `Context.send_message`, and acknowledges the event. AstrBot's current Discord adapter supports that generic session path.

Do not point AstrBot through the OpenAI proxy while the plugin is active. AstrBot cannot put its changing session ID into static provider headers, so that setup would create a second `default` conversation. Other harnesses can use the proxy when they can set the headers above.

## Proactive event API

The service evaluates silence, current drives, due follow-up events, quiet hours, cooldown, daily limit, and unanswered limit. It stores decisions before delivery, so restarts do not reset the limits or duplicate an event.

Poll and lease one event:

```sh
curl 'http://127.0.0.1:8765/state/v1/proactive/events?consumer=my-harness&harness=my-harness&limit=1'
```

After sending, acknowledge it:

```sh
curl -X POST http://127.0.0.1:8765/state/v1/proactive/events/EVENT_ID/ack \
  -H 'Content-Type: application/json' \
  -d '{"consumer":"my-harness","outcome":"sent","text":"the exact sent message"}'
```

Use `outcome: failed` to release it after the retry delay. A user reply cancels unsent events and resets the unanswered count.

## Memory and affect access

```sh
mise exec -- uv run companion-gateway memory search '青い硝子'
mise exec -- uv run companion-gateway memory show 42
mise exec -- uv run companion-gateway memory recent
mise exec -- uv run companion-gateway affect show
mise exec -- uv run companion-gateway affect event fear_concern \
  --note 'Travel safety concern' --follow-up-minutes 180
mise exec -- uv run companion-gateway proactive evaluate
```

The CLI gives the companion or an operator direct memory access without another model call. The REST endpoints expose the same data to MCP bridges.

## Backup and service setup

Create a consistent live backup:

```sh
mise exec -- uv run companion-gateway backup /path/to/backups/state-$(date +%F).sqlite3
```

For `systemd --user`:

```sh
mkdir -p ~/.config/companion-gateway ~/.config/systemd/user
cp config.example.yaml ~/.config/companion-gateway/config.yaml
cp deploy/companion-gateway.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now companion-gateway
journalctl --user -u companion-gateway -f
```

Edit the unit if this repository or `mise` lives elsewhere. The data directory contains the only state database. Back up `state.sqlite3` with the CLI instead of copying it while writes are active.

## Limits

FTS5 provides lexical and substring recall. It does not match paraphrases as well as an embedding index. The raw schema and message IDs allow a later embedding table without changing canonical data.

Deterministic phrase classification cannot infer every emotional nuance. Use explicit event labels for safety-critical fear or relationship events. The service does not generate persona content or decide what the companion believes.
