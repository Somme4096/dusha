# Companion State Gateway

`companion-gateway` gives an LLM companion durable memory, persistent affect, evergreen facts, and proactive message events. It keeps all of that in one SQLite file, so you can replace the harness without migrating data.

The gateway owns raw conversation memory, affect state, evergreen facts, and proactive decisions. It does not own the persona, write memory summaries, or run a separate vector database. Personality stays in your Markdown file and your harness.

## What one database stores

A single SQLite file holds the pieces that define a companion:

- Canonical messages, with original JSON, extracted text, timestamp, source, conversation, SHA-256 digest, and optional source message ID.
- An FTS5 lexical search index.
- Evergreen fact revisions, append-only and audited.
- Affect state for 16 dimensions.
- Proactive decisions and their delivery history.
- A derived embedding index of child offsets and vectors.

Canonical messages stay as written. Rebuild the derived index at any time without touching that history.

## Architecture

Hybrid retrieval treats each message as a parent and fixed character ranges as children. It fuses FTS5 parent ranks with vector child ranks through reciprocal-rank fusion. A semantic hit resolves back to the full parent and neighboring messages before injection.

The service sends chunks to the embedding endpoint you configure and reads the API key from an environment variable. It never stores that key. If the endpoint is unavailable, search falls back to lexical ranking.

The affect engine tracks fast base values and slower mood values. It applies event deltas, habituation, and two-timescale decay. A keyword matcher labels events, and an agent tool call can override the label.

The HTTP API under `/state/v1` is provider-neutral. `POST /state/v1/context` returns a ready context injection with identity, evergreen facts, memory, and affect state. The OpenAI-compatible proxy at `/v1` is optional.

One gateway database serves one companion. Run a second gateway with its own config, data directory, port, and user service for a second companion.

## Quickstart

Requires Python 3.11 or newer and uv.

```sh
cp config.example.json config.json
uv tool install --editable .
companion-gateway serve
```

The editable install places `companion-gateway` in `~/.local/bin` and links its code to this checkout. Rerun the install after you change dependencies in `pyproject.toml`.

The service reads `config.json` from the working directory, or `COMPANION_GATEWAY_CONFIG`, or the `--config` flag.

Check the service:

```sh
curl http://127.0.0.1:8765/health
```

The response reports status, database integrity, whether upstream is configured, and memory index state.

Upstream is optional. The example config points `upstream.base_url` at OpenAI. Set the variable named by `upstream.api_key_env` (`UPSTREAM_API_KEY` in the example) to route proxy chat completions. Without it, the proxy forwards the caller's `Authorization` header. The `/state/v1` API works with no upstream at all.

Record a message, then build context:

```sh
curl -X POST http://127.0.0.1:8765/state/v1/messages \
  -H 'Content-Type: application/json' \
  -d '{"role":"user","content":"Hello","conversation_id":"demo"}'

curl -X POST http://127.0.0.1:8765/state/v1/context \
  -H 'Content-Type: application/json' \
  -d '{"query":"Hello","conversation_id":"demo"}'
```

Set `api_token_env` to require a bearer token. The example leaves it empty for local use.

### Identity

The persona is raw user-authored Markdown. Point `identity_prompt.path` at your file:

```json
{
  "identity_prompt": {
    "path": "identity.md"
  }
}
```

The gateway never creates, infers, or archives that file, and no example personality ships here.

## Proactive events

The gateway evaluates silence, current drives, due follow-ups, quiet hours, cooldown, daily limit, and unanswered limit. It stores each decision before delivery, so restarts do not reset limits or duplicate an event. A harness polls and leases one event, sends it, then acknowledges it. See [docs/api.md](docs/api.md).

## Operations

The CLI inspects memory, affect, and evergreen facts, and it creates consistent backups without another model call. It also migrates a legacy YAML config once. See [docs/guide.md](docs/guide.md) for these commands and the packaged emotion and prompt exports.

## Limitations

Semantic quality depends on the embedding model you configure. The sqlite-vec query scans stored vectors, which suits a personal archive and may need an approximate index at larger scale. Evergreen facts go stale when an agent misses a correction. Expiry follows the clock, and semantic edits need an explicit revision. Agent classification depends on the model following the persona instruction, while the keyword matcher covers missed tool calls.

## Documentation

- [docs/guide.md](docs/guide.md) covers configuration, operations, the CLI, emotion and prompt exports, backup, and systemd.
- [docs/api.md](docs/api.md) documents the HTTP API and end-to-end integration, including AstrBot.
- [third-party-notices.md](third-party-notices.md) lists licenses and adapted work.
