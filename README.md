# Dusha


`dusha` is a Python API reference implementation that combines memory, personality, and affect for LLM companions. One `dusha` process serves one companion, and each companion lives in its own home directory.

The built-in SQLite store owns messages and evergreen facts, and core retrieval runs on lexical SQLite FTS5. A memory plugin can inject extra context and consume gateway-pushed message batches. A shared embedding endpoint adds semantic affect appraisal when configured.

Every prompt, emotional vector, and threshold is configurable through JSON. 

## Quickstart

Requires Python 3.11 or newer and uv. 

1. Create a home for your companion and copy the example config into it. This example names her `sophia`: `mkdir -p ~/.config/dusha/sophia && cp config.example.json ~/.config/dusha/sophia/config.json`
2. Install the CLI: `uv tool install --editable .`
3. Start the service: `dusha serve sophia`

The install links the CLI to this checkout on uv's tool bin path. If `dusha` is not on your `PATH`, run `uv tool update-shell`.

A companion home is `$XDG_CONFIG_HOME/dusha/<name>/`, or `~/.config/dusha/<name>/` when `XDG_CONFIG_HOME` is unset. It holds `config.json`, your emotion and prompt files, `mods/`, and `data/` with the database. Run a second companion by adding a second home with its own `port`. `dusha list` prints the companions, and `dusha -c <name> <command>` runs any other command against one. With a single companion you can leave the name out. To run it in the background, use [deploy/dusha@.service](deploy/dusha@.service) with the [service setup steps](docs/guide.md#run-a-durable-single-instance-service).

Check it is running:

```sh
curl http://127.0.0.1:8765/health
```

Success reports `"status": "ok"` and a passing `database` integrity check. If startup fails, see [docs/guide.md](docs/guide.md).

## How it works

A user message reaches the gateway, which builds context from memory, personality, and affect state and returns it to your harness or model. The harness gets the reply through the gateway. The built-in SQLite store owns messages and facts unless you set `storage.enabled` to false. A trusted memory plugin can inject extra context and receive message batches. A trusted decision plugin can select one emotion dimension to adjust per new user message. See [docs/configuration.md](docs/configuration.md).

Build your harness plugin with the [API and integration guide](docs/api.md).

## Plugin API

A plugin is a directory under the mods directory holding `README.md`, `main.py`, `pyproject.toml`, and `uv.lock`. The gateway syncs its locked dependencies with `uv` and runs it as a child process. Every operation in `main.py` has the signature `fn(request, options)`, where `options` is the `options` object from the plugin's config section. A plugin sees a filtered environment: the basic system variables plus the names you list in `env_passthrough`.

A decision plugin exports one operation.

| Operation | Request fields | Returns |
| --- | --- | --- |
| `decide` | `message`, `emotions` (the resolved dimensions), `state` (the affect snapshot), `instruction` | `{"emotion": "<dimension>"}` or `None` |

A memory plugin exports these operations. The gateway reads only the fields listed.

| Operation | Request fields | Returns |
| --- | --- | --- |
| `ingest_messages` | `messages`, a list of stored rows with `id`, `conversation_id`, `role`, `text`, `occurred_at`, `ingested_at`, `sha256`, `harness`, `external_conversation_id` | `{"highest_id": <id of the last row>}` |
| `inject_context` | `query`, `scope`, `max_chars`, `harness`, `conversation_id` | `{"text": "...", "records": [{"source": "...", "text": "..."}]}` |
| `match_phrase` | `message` | `{"deltas": {"<dimension>": <number>}}` or `{"deltas": null}` |
| `status` | none | `{"status": {...}}`, returned as the memory index status |
| `backfill_once` | `limit`, `force` | `{"status": {...}}` |
| `rebuild_chunks` | none | `{"status": {...}}` |
| `rebuild_index` | none | `{"status": {...}}` |
| `close` | none | `None` |

`match_phrase` deltas use dimension names from your `emotions.json`. The gateway logs a warning for a name it does not know and skips it. See [examples/mods](examples/mods/README.md) for two working plugins.

## Documentation

- [docs/configuration.md](docs/configuration.md): JSON settings, identity and prompt files.
- [docs/guide.md](docs/guide.md): service setup, auth and network, backup, and troubleshooting.
- [docs/api.md](docs/api.md): HTTP endpoints and harness integration, including AstrBot.
- [third-party-notices.md](third-party-notices.md): licenses and adapted work.
