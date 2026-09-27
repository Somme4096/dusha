# Companion State Gateway


`companion-gateway` is a Python API reference implementation of a combined memory, personality, and affect for LLM companions.

Essentially, it's a small FastAPI application with a completely configurable emotional system and a generic FTS5-Embedding hybrid memory structure combined. 

Every prompt, emotional vectors and their thresholds is completely customizable through JSON. 

## Quickstart

Requires Python 3.11 or newer and uv. 

1. Copy the example config into the default config directory: `mkdir -p ~/.config/companion-gateway && cp config.example.json ~/.config/companion-gateway/config.json`
2. Install the CLI: `uv tool install --editable .`
3. Start the service: `companion-gateway serve`

The install links the CLI to this checkout on uv's tool bin path. If `companion-gateway` is not on your `PATH`, run `uv tool update-shell`.

The service reads `config.json` from `~/.config/companion-gateway/` first, then a `config.json` in the working directory as a legacy fallback. Pass `--config PATH` to override. To run it in the background, use [deploy/companion-gateway.service](deploy/companion-gateway.service) with the [service setup steps](docs/guide.md#run-a-durable-single-instance-service).

Check it is running:

```sh
curl http://127.0.0.1:8765/health
```

Success reports `"status": "ok"` and a passing `database` integrity check. If startup fails, see [docs/guide.md](docs/guide.md).

## How it works

A user message reaches the gateway, which builds context from memory, personality, and affect state and returns it to your harness or model. The harness gets the reply through the gateway. A trusted decision suite can select one emotion dimension to adjust per new user message. See [docs/configuration.md](docs/configuration.md#decision-suites).

Build your harness plugin with the [API and integration guide](docs/api.md).

## Documentation

- [docs/configuration.md](docs/configuration.md): JSON settings, identity and prompt files.
- [docs/guide.md](docs/guide.md): service setup, auth and network, backup, and troubleshooting.
- [docs/api.md](docs/api.md): HTTP endpoints and harness integration, including AstrBot.
- [third-party-notices.md](third-party-notices.md): licenses and adapted work.
