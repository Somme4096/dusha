# Companion State Gateway

Run the [quickstart](#quickstart) to start a local gateway.

`companion-gateway` is a Python API reference implementation of a combined memory, personality, and affect structure for LLM companions. You supply the personality; the gateway stores memory, affect, and proactive state in one SQLite file, so you can change the harness without migrating data.

## Quickstart

Requires Python 3.11 or newer and uv. Budget about 5 minutes when both are installed, or allow 10 to 15 minutes for the first install on a slow connection.

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

A user message reaches the gateway, which builds context from memory, personality, and affect state and returns it to your harness or model; the harness archives the reply through the gateway. A trusted Python decision plugin can select one emotion dimension to adjust per new user message; see [docs/configuration.md](docs/configuration.md#decision-plugins).

Build your harness integration with the [API and integration guide](docs/api.md).

## Documentation

- [docs/configuration.md](docs/configuration.md): settings, identity and prompt files, and legacy config migration.
- [docs/guide.md](docs/guide.md): service setup, auth and network, backup, and troubleshooting.
- [docs/api.md](docs/api.md): HTTP endpoints and harness integration, including AstrBot.
- [third-party-notices.md](third-party-notices.md): licenses and adapted work.
