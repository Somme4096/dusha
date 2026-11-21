# Dusha Guide

Dusha runs as two services per companion: the server (`dusha serve`), which stores state and exposes MCP and CLI routes, and the proxy (`dusha proxy`), which routes model requests from your harness and injects context, such as memory or affect.

## Usage

### Managing companions

Each companion stores its configuration and data in a home directory under `$XDG_CONFIG_HOME/dusha/<name>/` (or `~/.config/dusha/<name>/` when `XDG_CONFIG_HOME` is unset). Companion names can contain letters, digits, dots, dashes, and underscores.

```sh
dusha companion add <name>
dusha companion list
dusha companion update <name>
dusha companion remove <name>
```

- `dusha companion add <name>` creates `~/.config/dusha/<name>/config.json` with mode `600`, writing both the server settings and a `proxy` section pointed at that local server. Pass `--serve` to write only the server settings, or `--proxy` to write only the proxy settings.
- `dusha companion update <name>` adds a missing `--serve` or `--proxy` section to an existing home, updates `--server-url` or `--token`, and upgrades legacy configuration files while keeping `<file>.bak` backups.
- `dusha companion list` prints the config root directory and all initialized companion names.
- `dusha companion remove <name>` deletes the companion home directory and its data. Pass `-y` / `--yes` to skip confirmation in non-interactive shells.

Every CLI command selects a companion using the first match in this order:

1. `--config <path>` (uses the file's parent directory as the companion home)
2. `--home <dir>`
3. `-c <name>` / `--companion <name>`, or the positional `<name>` argument on `companion`, `serve`, `proxy`, `provider`, and `plugin list`
4. `DUSHA_HOME` environment variable
5. `DUSHA_COMPANION` environment variable
6. The sole companion under the config root, when only one exists

If multiple companions exist and none is specified, the command exits with an error.

### Managing providers

`dusha provider` manages the `providers` section in the companion's `config.json`. Each entry defines an upstream model provider profile that the proxy serves at `http://127.0.0.1:8766/<profile>`.

```sh
dusha provider <name>
dusha provider <name> --add deepseek
dusha provider <name> --add custom --profile local-vllm
dusha provider <name> --list
dusha provider <name> --remove deepseek
```

Running `dusha provider <name>` in a terminal opens an interactive menu to add, list, or remove provider profiles:

- **Adding from the catalog**: Select a provider via interactive menu or pass `--add <provider_id>`. The CLI prompts for the API key. You can 1) Enter the key directly, 2) type `{env:VAR_NAME}` to read it from an environment variable, 3) press Enter to leave it empty. By default, the profile name matches the provider ID unless you provide custom name with `--profile <profile>`.
- **Adding a custom provider**: Choose `Custom` in the menu or pass `--add custom`. The CLI prompts for a profile name, base URL, optional per-protocol endpoints (`chat`, `messages`, `responses`), auth header style (`bearer` or `x-api-key`), API key, and optional static headers.
- **Refreshing model catalog**: The models.dev catalog is cached at `$XDG_CACHE_HOME/dusha/models-dev.json` (`~/.cache/dusha/models-dev.json` by default) for 24 hours. Pass `--refresh` to fetch a fresh copy.
- **Supported protocols**: The catalog includes OpenAI Chat Completions, OpenAI Responses, and Anthropic Messages providers (including Google's OpenAI-compatible endpoint). Providers that require cloud SDK credentials instead of an API key (such as AWS Bedrock, Google Vertex AI, Azure, and GitHub Copilot) are excluded.

Restart is required after adding or modifying a provider.

### Running the proxy

`dusha proxy <name>` starts the local HTTP proxy for an existing companion on, by default, `http://127.0.0.1:8766`. See [configuration.md](configuration.md#proxy) for details.

If the companion was created with `--serve` only, add the proxy section first:

```sh
dusha companion update <name> --proxy
```

When you configure `--proxy` without a saved server URL, an interactive terminal prompts and saves the selection to `config.json`. In non-interactive environments, pass `--server-url <url>` and `--token <token>`.

Once the proxy is running, connect your harness to `http://127.0.0.1:8766/<profile>` as described in [Connect a harness](#connect-a-harness).

### All core commands

| Command | Mode | Description |
| --- | --- | --- |
| `dusha companion add [name] [--serve] [--proxy] [--server-url URL] [--token TOKEN]` | Local | Create a new companion home and `config.json`. |
| `dusha companion update [name] [--serve] [--proxy] [--server-url URL] [--token TOKEN]` | Local | Add missing config sections, update server connection settings, or upgrade legacy files. |
| `dusha companion list` | Local | List initialized companions under the config root. |
| `dusha companion remove [name] [-y/--yes]` | Local | Delete a companion home and its data directory. |
| `dusha serve [name]` | Local | Run the companion server (`http://127.0.0.1:8765` by default). |
| `dusha proxy [name]` | Local | Run the companion proxy (`http://127.0.0.1:8766` by default). |
| `dusha provider [name] [--add ID] [--profile NAME] [--list] [--remove PROFILE] [--refresh]` | Local | Add, list, or remove model provider profiles. |
| `dusha plugin list [name]` | Server HTTP | Print each plugin's state, error, injected dependencies, provided services, and event listeners. |
| `dusha health` | Server HTTP | Run a SQLite integrity check on the server database. |
| `dusha backup <destination>` | Server HTTP | Write a consistent online SQLite backup to `<destination>` on the server host. |

Active plugins can register their own command groups (such as `memory`, `evergreen`, `memo`, `affect`, and `proactive`).

## Connect a harness

Connecting a harness to Dusha has two steps. 1) routing model requests through `dusha proxy` for automatic context injection and 2) registering the Dusha MCP for tools.

1. **Provider base URL**: Point the harness's provider base URL at `http://127.0.0.1:8766/<profile>`.
2. **API key (**`<token>` **or** `<harness>:<token>`**)**: Set the harness's provider API key to your Dusha `api_token` (or any placeholder string when [auth](#auth) is off), never your upstream provider's API key. 
   1. Prefix the value with `<harness>:` (for example, `claude-code:my-dusha-token`, or `claude-code:local` when auth is off) so the stored messages will have appropriate `<harness>` label. This is recommended because you can apply per-harness injection rules via `harnesses.<name>` settings.
   2. Without this prefix or an `X-Harness` header, the harness name defaults to `<profile>`, for example, `mistral`.
3. **MCP server**: Point the harness at `http://127.0.0.1:8765/mcp` (Streamable HTTP transport) and pass `X-Companion-Token: <token>` when [auth](#auth) is enabled.

See [api.md](api.md#proxy) for protocol routing and header details.

### Claude Code

In `~/.claude/settings.json`, set `ANTHROPIC_BASE_URL` to your proxy profile URL and `ANTHROPIC_API_KEY` (or `ANTHROPIC_AUTH_TOKEN`) to `claude-code:<token>`:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8766/anthropic",
    "ANTHROPIC_API_KEY": "claude-code:replace-with-dusha-token"
  }
}
```

Register the Dusha MCP server:

```sh
claude mcp add --transport http dusha http://127.0.0.1:8765/mcp \
  --header "X-Companion-Token: replace-with-dusha-token"
```

### Codex

In `~/.codex/config.toml`, define a custom model provider pointed at your proxy profile and register the MCP server:

```toml
model_provider = "dusha"

[model_providers.dusha]
name = "Dusha"
base_url = "http://127.0.0.1:8766/openai"
env_key = "DUSHA_API_KEY"
wire_api = "responses"

[mcp_servers.dusha]
url = "http://127.0.0.1:8765/mcp"
http_headers = { "X-Companion-Token" = "replace-with-dusha-token" }
```

Export the tagged Dusha token in your shell before running `codex`:

```sh
export DUSHA_API_KEY="codex:replace-with-dusha-token"
```

### Antigravity CLI (`agy`)

`agy` uses Google Account OAuth by default (including Google AI Pro) and ignores custom model base URLs in that mode. Instead of routing model traffic through `dusha proxy`, connect `agy` directly to `dusha serve` with the bundled [`integrations/agy_dusha`](../integrations/agy_dusha/README.md) lifecycle-hook plugin and the Dusha MCP server.

1. Keep `agy` on its default Google Account sign-in (leave `modelProvider` unset in `~/.gemini/antigravity-cli/settings.json`).
2. Install the bundled `agy` plugin from the repository root:

```sh
agy plugin install integrations/agy_dusha
```

3. Register the Dusha MCP server in `agy` (omit `--header` when [auth](#auth) is off):

```sh
agy mcp add --header "X-Companion-Token: replace-with-dusha-token" \
  dusha http://127.0.0.1:8765/mcp
```

The plugin's `PreInvocation` and `Stop` hooks call `dusha serve` directly to ingest user turns, inject context as a `systemMessage` step, and archive assistant replies. When `DUSHA_URL` is unset, it reads the server URL and token from your companion's `config.json` automatically (using `DUSHA_CONFIG`, `DUSHA_HOME`, `DUSHA_COMPANION`, or the sole companion under `~/.config/dusha`). Set `DUSHA_URL`, `DUSHA_TOKEN`, `DUSHA_HARNESS` (default `agy`), or `DUSHA_PLUGINS` to override. See [`integrations/agy_dusha/README.md`](../integrations/agy_dusha/README.md) for all environment options.

#### Using a Gemini API key instead of Google Account OAuth

If you want `agy` to call a custom Gemini endpoint instead of signing in with a Google Account, set `modelProvider` to `"gemini"` in `~/.gemini/antigravity-cli/settings.json`:

```json
{
  "modelProvider": "gemini"
}
```

Then override the endpoint URL and API key in your shell environment before launching `agy`:

```sh
export GOOGLE_GEMINI_BASE_URL="http://127.0.0.1:8766/<profile>"
export GEMINI_API_KEY="agy:replace-with-dusha-token"
```

> [!NOTE]
> `dusha proxy` injects context on OpenAI `chat/completions`, OpenAI `responses`, and Anthropic `messages` routes, and forwards native Gemini paths untouched unless paired with a protocol translation gateway.

## Deployment

### Systemd user service

The template unit [`deploy/dusha@.service`](../deploy/dusha@.service) runs `dusha serve <name>` for `~/.config/dusha/<name>/`.

1. Install the CLI and initialize the companion:

```sh
uv tool install dusha
dusha companion add <name> --serve
```

2. Install the systemd user unit:

```sh
mkdir -p ~/.config/systemd/user
curl -fsSL -o ~/.config/systemd/user/dusha@.service \
  https://raw.githubusercontent.com/Somme4096/dusha/main/deploy/dusha@.service
```

3. If you use environment variables for secrets (such as `EMBEDDING_API_KEY`), store them in `~/.config/dusha/<name>/.env`:

```sh
install -m 600 /dev/null ~/.config/dusha/<name>/.env
${EDITOR:-vi} ~/.config/dusha/<name>/.env
```

```ini
EMBEDDING_API_KEY=replace-with-your-key
```

The unit sets `PATH` to `%h/.local/bin:/usr/local/bin:/usr/bin:/bin`. If `dusha` is installed elsewhere, add a `PATH=` entry to `.env`.

4. Enable and start the service:

```sh
systemctl --user daemon-reload
systemctl --user enable --now dusha@<name>
systemctl --user status dusha@<name>
curl http://127.0.0.1:8765/health
```

### Docker and Podman

Container images are published to `ghcr.io/somme4096/dusha` on each release. The container runs `dusha serve --home /data`, storing `config.json`, `data/state.sqlite3`, `identity.md`, and `plugins/` in the `/data` volume.

1. Download [`deploy/compose.yaml`](../deploy/compose.yaml), initialize `/data/config.json`, and start the container (`podman compose` accepts the same commands):

```sh
mkdir dusha && cd dusha
curl -fsSL -O https://raw.githubusercontent.com/Somme4096/dusha/main/deploy/compose.yaml
touch .env
docker compose run --rm dusha companion add --home /data --serve
docker compose up -d
curl http://127.0.0.1:8765/health
```

The compose file sets `DUSHA_HOST=0.0.0.0` inside the container and binds `127.0.0.1:8765:8765` on the host. To access the server from another machine, expose it over Tailscale or a TLS reverse proxy rather than publishing plain HTTP.

2. To edit `config.json` in the volume:

```sh
docker compose cp dusha:/data/config.json .
${EDITOR:-vi} config.json
docker compose cp config.json dusha:/data/config.json
docker compose restart
```

3. To upgrade the image or create a database backup:

```sh
docker compose pull && docker compose up -d
docker compose exec dusha dusha backup /data/backup.sqlite3
docker compose cp dusha:/data/backup.sqlite3 ./backup.sqlite3
```

## Auth

When authentication is not enabled, both the server and proxy bind to `127.0.0.1`. To enable authentication, set `api_token` in the server's `config.json`:

```json
{
  "api_token": "replace-with-a-random-token"
}
```

Restart the server afterwards. 

To connect a remote proxy or CLI to an authenticated server, run `dusha companion update <name> --proxy --server-url <url> --token <token>` on the client machine.

### Dusha token and `harness:token`

Dusha proxy only accepts **dusha token** instead of upstream provider's API key. This is true for all provider routes, for example, `http://127.0.0.1:8766/mistral` and `http://127.0.0.1:8766/opencode-go` both uses your dusha token. Dusha proxy replace your dusha token to your key saved in config.json before routing it.

Why? Because most harnesses only let you configure a base URL and an API key, so setting custom headers like `X-Companion-Token`  for auth is often not possible. You can prefix the API key with `<harness>:` to identify the harness without setting custom HTTP headers:

| Harness API key value | When auth is on (`api_token` set) | When auth is off (`api_token` empty) |
| --- | --- | --- |
| `<token>` | Authenticates the request. Harness name defaults to `<profile>` (or `X-Harness`). | Accepted (any value works). Harness name defaults to `<profile>` (or `X-Harness`). |
| `<harness>:<token>` (for example, `claude-code:my-token`) | Authenticates `<token>` and sets the harness name to `<harness>`. | Sets the harness name to `<harness>` (for example, `claude-code:local`). |

Setting `<harness>:<token>` labels every stored message with `<harness>` and applies the matching [`harnesses.<name>`](configuration.md#harnesses) context filter rules (for example, excluding `affect` in a coding harness).

### Route behavior with auth enabled

- **Server routes**: Every `/state/v1/*`, `/cli/v1/*`, and `/mcp` request requires `X-Companion-Token: <token>`. Missing or invalid tokens return `401`.
- **Proxy routes**: Every `/<profile>/*` request requires `<token>` or `<harness>:<token>`. The proxy authenticates to the Dusha server automatically using `proxy.server.token`.
- **Public and disabled routes**: `GET /health` remains public on both server and proxy. The interactive OpenAPI endpoints (`/docs`, `/redoc`, and `/openapi.json`) are disabled.

## Backup and restore

Create a consistent online backup of the SQLite database while the server is running:

```sh
dusha backup /path/to/backups/state-$(date +%F).sqlite3
```

The destination path is resolved on the server machine. Do not copy `data/state.sqlite3` directly while the server is running. Back up `config.json`, `identity.md`, custom `emotions.json` or `prompts.json` files, and the `plugins/` directory alongside the database snapshot.

To restore from a backup:

1. Stop `dusha serve`.
2. Copy the backup file to `<data_dir>/state.sqlite3` (and remove any leftover `state.sqlite3-wal` or `state.sqlite3-shm` files).
3. Start `dusha serve`.

## Migrating

Run `dusha companion update <name>` to automatically migrate legacy configuration and prompt files from earlier `1.x` releases. The command writes `<file>.bak` before modifying any file.

### `prompts.json` schema 3 to schema 4

Starting in `1.1.21`, prompt overrides live under `plugins.<name>` rather than top-level keys. Schema 3 overlays continue to load with a warning until `2.0.21`. Run `dusha companion update <name>` or move the keys manually:

| Schema 3 key | Schema 4 key |
| --- | --- |
| `affect_presentation` | `plugins.affect.presentation` |
| `affect` | `plugins.affect.block` |
| `decision_instruction` | `plugins.affect.decision_instruction` |
| `proactive_framing` | `plugins.proactive.framing` |
| `proactive_generation_instruction` | `plugins.proactive.generation_instruction` |
| `evergreen` | `plugins.evergreen` |
| `context_blocks.memo_open_delimiter`, `context_blocks.memo_close_delimiter` | `plugins.memo.open_delimiter`, `plugins.memo.close_delimiter` |
| `context_blocks.plugin_open_delimiter`, `context_blocks.plugin_close_delimiter` | Removed (unused). |
| `companion_state.memory_instruction` | `plugins.memory.instruction` |

`companion_state.open_delimiter` and `companion_state.close_delimiter` remain unchanged.

### `proxy.upstreams` to `providers`

Starting in `1.1.21`, provider profiles live under top-level `providers` instead of `proxy.upstreams` so server plugins can access them as well. `proxy.upstreams` continues to load with a warning until `2.0.21`.

- Run `dusha companion update <name>` to move `proxy.upstreams` to `providers`.
- Replace `dusha proxy <name> auth login`, `status`, and `logout` with `dusha provider <name> --add`, `--list`, and `--remove`.
- Legacy Python aliases (`Settings.upstreams`, `dusha.client.settings.Upstream`, `save_upstream`, `dusha.config.create_default_config`, and 1.x `ctx.prompts` keys) emit a `DeprecationWarning` and will be removed in `2.0.21`.

### Upstream provider key as proxy credential

Harnesses that pass the upstream provider API key to the proxy instead of the Dusha token continue to work with a warning until `2.0.21`, provided the key matches `providers.<profile>.key` in `config.json`. Update harnesses to send the Dusha `api_token` (or any placeholder string when authentication is disabled).

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| `Configuration file not found` on startup | Run `dusha companion list` to check initialized companions, or run `dusha companion add <name>` to create `config.json`. |
| `several companions exist` error | Pass `-c <name>`, `--home <dir>`, or `--config <path>`, or set `DUSHA_COMPANION`. |
| Startup fails on a missing identity file | Check `plugins.identity.path` in `config.json` and ensure the file exists and is valid UTF-8, or remove `path` to use `identity.md`. |
| Startup fails on an empty `plugins.identity.path` | Remove `plugins.identity.path` to default to `identity.md`, or set `"disabled": true` under `plugins.identity`. |
| Startup fails on `upstream` or `api_openai` | Remove those keys and configure a profile under [`providers`](configuration.md#providers) instead. |
| Requests return `401 Unauthorized` | Pass `X-Companion-Token: <api_token>` to the server, or set `<api_token>` as the provider API key in harnesses routed through the proxy. |
| CLI command fails with `cannot connect to the server` | Start `dusha serve <name>`, or verify `proxy.server.url` and `port` in `config.json`. |
| Plugin CLI command fails with `No such command` | The plugin is disabled or inactive. Run `dusha plugin list <name>` to inspect its status. |
| `dusha provider --add` fails with `cannot load the models.dev catalog` | The machine is offline and has no cached catalog. Connect to the internet once, or use `dusha provider <name> --add custom`. |
| Proxy returns `502 Bad Gateway` | The upstream provider could not be reached or timed out. Check `providers.<profile>.base_url` and `endpoints` in `config.json`. |
| Proxy forwards responses but stores no messages | Check `GET http://127.0.0.1:8766/health` and inspect `server.status`. Verify `proxy.server.url` and `proxy.server.token`. |
| State route returns `503 Service Unavailable` | The response `detail` names the disabled or inactive plugin. Enable it and any required dependencies in `config.json`, then restart the server. |
| Startup fails on an external plugin import or `apply` error | Inspect the traceback for the failing plugin directory, check its `[tool.dusha]` table in `pyproject.toml`, and ensure its dependencies are installed in the server environment. |
| Duplicate proactive events or SQLite write errors | Multiple `dusha serve` processes are using the same `state.sqlite3`. Stop the duplicate process. |

## See also

- [configuration.md](configuration.md): Configuration files and key reference.
- [plugins.md](plugins.md): Built-in plugins and the Plugin API.
- [api.md](api.md): HTTP endpoints, proxy routing, and MCP protocol.
