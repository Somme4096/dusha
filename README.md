# Dusha

> [!WARNING]
> THIS SOFTWARE IS UNDER RAPID DEVELOPMENT. BREAKING CHANGES ARE HAPPENING BEFORE NOVEMBER.

> [!NOTE]
> *This README.md is proudly written by a human (and a little bit by Claude (˶>⩊<˶)❤️)*

*Dusha* (Russian: душа, [dʊˈʂa], "soul") is a small HTTP API that stores messages, identity, facts, and affect state outside your harness. Any client that can POST a message and read back an injection string can use it, so you can hop between harnesses (or even use multiple at once!) and still get the same soul on the other side.

Every prompt, emotional vector, and threshold is configurable through JSON. Dusha also has an *extensible plugin system* that lets you wire any external memory provider or classifier easily!

## Quickstart

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```sh
uv tool install dusha
dusha serve <name>
```

The second line starts a companion named `<name>`. On the first run it makes the folder `~/.config/dusha/<name>/` and writes a `config.json` with every setting in it. It respects `XDG_CONFIG_HOME` too!

### Advanced setup

Want a second companion? Make a second folder and give it a different `port`. `dusha list` shows all of them, and `dusha -c <name> <command>` talks to one.

Want it running in the background? Use [deploy/dusha@.service](https://github.com/Somme4096/dusha/blob/main/deploy/dusha@.service) with the [service setup steps](https://github.com/Somme4096/dusha/blob/main/docs/guide.md#run-as-a-systemd-service).  *(Linux only. Tell your agents to make PR for it if you use something else!)*

## How it works

`user message → dusha → injects context → your model → reply → (harness, dusha)`

`dusha serve` is the server that holds the soul, and it can live on another machine. `dusha proxy <name>` is the client and runs next to your harness. Its first run asks for the server's URL and token and saves them. `dusha proxy <name> auth login` adds a provider from [models.dev](https://models.dev) with its key, see [provider login](https://github.com/Somme4096/dusha/blob/main/docs/api.md#provider-login). Point the harness's provider base URL at `http://127.0.0.1:8766/<profile>` and the proxy stores each message, adds the context to the latest user message, and forwards the request to your provider, see [proxy](https://github.com/Somme4096/dusha/blob/main/docs/api.md#proxy). The server also gives the harness the memory, evergreen, and memo tools over MCP at `/mcp`, see [MCP](https://github.com/Somme4096/dusha/blob/main/docs/api.md#mcp). While the server is away, the proxy keeps the conversation in an outbox and sends it later. Chat platforms that need proactive messages, such as AstrBot, use a plugin that calls the server directly.

<details>
<summary>How does memory/affect/identity work in Dusha?</summary>


- Messages: Basic SQLite FTS5 + optional embedding search. An external plugin in `~/.config/dusha/<name>/plugins/` can add its own block, such as an external memory provider.
- Evergreen: Long-term facts that you edit from the CLI and your companion edits through integration tools. No automatic extraction 
	* think of it as a USER.md that actually has good lifecycle management and duplication prevention
- Affect: A deterministic emotion engine with decay and silence drift. Proactive messages fire on thresholds. 
	* And yeah, message intent analysis can be an external plugin too: drop Jev or any other classifier into the plugins directory and its `decide` listener picks which emotion each user message moves.
- Identity: `identity.md`, or name one your own.

</details>

## Plugin API

Every part of Dusha is a plugin. Identity, memory, evergreen, memo, affect, and proactive are built in, and a plugin you add is an external plugin: a directory under the plugins directory holding `pyproject.toml` with a `[tool.dusha]` table. The gateway imports the module into its own process, so the plugin gets the same `ctx` as a built-in plugin, including `ctx.api` and `ctx.cli`:

```toml
[tool.dusha]
entry = "my_plugin"
inject = ["messages"]
```

The module defines `apply(ctx, config)`, which runs at startup. `config` is the plugin's `options` from `config.json`. `inject` lists the services the plugin calls, including `api` and `cli` when it registers routes or commands, and a plugin whose service is missing stays inactive. External plugins use the gateway's Python environment, so extra dependencies must be installed there and a version conflict affects the gateway.

| Call | Effect |
| --- | --- |
| `ctx.on("message", fn)` | `fn(message)` runs for every stored message, in id order, with the stored row: `id`, `conversation_id`, `role`, `text`, `content`, `external_id`, `occurred_at`, `ingested_at`, `harness`, `external_conversation_id`, `route`. An exception is logged and the message is not redelivered. |
| `ctx.on("context", fn)` | `fn(request)` runs for every context build with `query`, `harness`, `conversation_id`, `exclude_message_ids`, and `include_recent`. Return a `Block` from `dusha.hooks` or `None`. A block carries the prompt text, block data, and session records. |
| `ctx.on("decide", fn)` | `fn(request)` runs for every new user message while affect is on, with `message`, `emotions` (the resolved dimensions), `state` (the affect snapshot), and `instruction`. Return `{"emotion": "<dimension>"}`, `{"deltas": {"<dimension>": <number>}}`, both, or `None`. |
| `ctx.on("<event>", fn)` | `fn(*args)` runs when another plugin emits `<event>`. Custom events stay in the gateway process, so `emit` and `collect` reach every active plugin. |
| `ctx.interval(seconds, fn)` | `fn()` runs on that interval while the gateway serves. |
| `ctx.on_dispose(fn)` | `fn()` runs on shutdown. |
| `ctx.data_dir` | A private directory, `plugins/<name>` under `data_dir`. |
| `ctx.name` | The plugin's name. |
| `ctx.error` | Setting a non-empty string marks the plugin failed and shows the text in `/state/v1/plugins`. |
| `ctx.provide(name, value)` | Publishes a service object under `name`. Another plugin consumes it by listing `name` in `inject` and calling `ctx.<name>.<method>(...)`. |
| `ctx.emit(event, ...)` | Calls every active plugin listening for `event`, including this plugin's own listeners. |
| `ctx.collect(event, ...)` | Calls every listener and returns `{"<plugin>": <first non-null result>}` under each plugin's name. |
| `ctx.api` | The gateway's FastAPI router. Mount routes on it exactly as a built-in plugin does. |
| `ctx.cli` | The gateway's command registry. `ctx.cli.group(name, help)` returns a group, and `group.command(name, handler, help=..., args=[...])` adds `dusha <group> <name>`, see [Commands](#commands). |
| `ctx.<service>.<method>(...)` | Calls a method of an injected service and returns its result: `messages` (`get`, `recent`, `search`, `context`, `after`), `companion` (`ingest_message`, `build_context`, `latest_route`), `affect` (`status`), `evergreen` (`remember`, `revise`, `forget`, `list_current`, `history`), `memo` (`add`, `list`, `done`), `proactive` (`evaluate`, `poll`, `acknowledge`). |

Services provided by an external plugin are consumed by other plugins through `inject`, and they are direct Python objects.

The first `emotion` in `plugins` order wins. `deltas` from every plugin merge and apply after the increment. Dimension names come from your `emotions.json`.

### Commands

A command runs on the server. `dusha` on any machine fetches the server's command list and builds the command from it, so a plugin's commands work wherever you run `dusha -c <name>`. Describe the arguments with `arg` and `option` from `dusha.commands`:

```python
from dusha.commands import arg, option


def apply(ctx, config):
    notes = ctx.cli.group("notes", "Manage notes")
    notes.command(
        "add",
        lambda text, pinned, tags: {"note": save(text, pinned, tags)},
        help="Add a note",
        args=[arg("text"), option("pinned", bool), option("tags", repeated=True)],
    )
```

`dusha notes add "Buy tea" --pinned --tags home --tags errand` sends the values to the server as JSON and prints the handler's return value as JSON.

| Helper | Makes |
| --- | --- |
| `arg(name, type=str, help="", choices=(), optional=False, repeated=False)` | A positional argument. |
| `option(name, type=str, help="", default=None, choices=(), required=False, repeated=False)` | `--name`, with each underscore as a dash. A `bool` option is a flag. |

- `type` is `str`, `int`, `float`, or `bool`. `choices` limits a value to a list of strings.
- The handler receives every argument by name. A value the caller leaves out arrives as its `default`.
- The server checks names and types before the handler runs. Check rules that span arguments, such as two flags that exclude each other, in the handler.
- Raise `CommandError` from `dusha.commands`, `ValueError`, or `KeyError` to fail the command. `dusha` prints the message and exits with `1`.
- `ctx.cli.command(name, handler, ...)` adds a top-level command, as core does for `health` and `backup`. `serve`, `proxy`, `list`, and `plugins` are taken.
- A disabled plugin registers nothing, so `dusha --help` leaves its commands out and running one ends in `No such command`.
- `dusha` keeps the last command list per server, so `--help` works while the server is down.

## Documentation

- [docs/configuration.md](https://github.com/Somme4096/dusha/blob/main/docs/configuration.md): JSON settings, identity and prompt files.
- [docs/guide.md](https://github.com/Somme4096/dusha/blob/main/docs/guide.md): service setup, auth and network, backup, and troubleshooting.
- [docs/api.md](https://github.com/Somme4096/dusha/blob/main/docs/api.md): HTTP endpoints and harness integration, including AstrBot.

## Acknowledgement

Sincere gratitude to:

- [Drivesoid](https://github.com/A1batr055/Drivesoid) for their 16-emotion system and `base/mood` cycle.
- [Omemo](https://github.com/OmniDimen/omemo) for their gateway-as-memory-layer idea.
