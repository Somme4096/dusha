# Dusha

> [!WARNING]
> THIS SOFTWARE IS UNDER RAPID DEVELOPMENT. BREAKING CHANGES ARE HAPPENING BEFORE NOVEMBER.

> [!NOTE]
> *This README.md is proudly written by a human (and a little bit by Claude (˶>⩊<˶)❤️)*

*Dusha* (Russian: душа, [dʊˈʂa], "soul") is a small HTTP API and a proxy that stores messages, identity, memories, affect state, and anything you build outside your harness. Any client that can add a custom provider are able to use it, so you can hop between harnesses (or even use multiple at once!) and still get the same soul on the other side.

Almost everything is configurable through JSON. For things that can't, dusha is also built on [everything-is-a-plugin](https://github.com/deepseek-ai/deepseek-harness) architecture, so you can easily wire an external memory provider, or do crazy things like writing your own emotion system!

## Quickstart


```sh
uv tool install dusha
dusha serve <name>
```

This starts a companion named `<name>`. On the first run it makes the folder `~/.config/dusha/<name>/` and writes a `config.json`.
Then on the same or another device, run:

```sh
$ dusha proxy <name>

> Connect to:
> ? The server on this machine: http://192.168.0.100:8765/
> ? Another server...←

> ? Server URL http://100.145.95.87:8765
> ? Server token, empty for none **********

> Connected!
> Proxy now listening to http://100.145.95.87:8765.
> Saved ~/.config/dusha/<name>/config.json.
```

Add a custom provider:

```sh
$ dusha proxy <name> auth login # Add a provider profile from the models.dev catalog

> ? Provider (Use arrow keys, type to filter)
>  » 302.AI (302ai)
>    Abacus (abacus)
>    abliteration.ai (abliteration-ai)
>    above.dev (above)
>    AgentRouter (agentrouter)
>    Agnes AI (agnes)
>    ...

> Success!
> New provider 302ai added to <name> at http://127.0.0.1:8766/302ai.
> Restart dusha proxy <name> to use it.

$ dusha proxy <name>

> INFO:     Started server process [3970524]
> INFO:     Waiting for application startup.
> INFO:     Application startup complete.
> INFO:     Uvicorn running on http://127.0.0.1:8766 (Press CTRL+C to quit)
```

Congratulations! Now in your harness or app, point your endpoint at the URL `auth login` gave you: in this case, `http://127.0.0.1:8766/302ai`. And you're good to go. 

Optionally (but highly recommended), add [MCP](https://github.com/Somme4096/dusha/blob/main/docs/api.md#mcp).

## How it works

`user message → dusha proxy → injects context → routed to upstream model → reply → (harness, dusha)`.

Dusha adds context "after" each message so cache can hit.

> [!NOTE]
> <details>
> <summary>How do messages, memory, affect, and identity work in Dusha by default?</summary>
>
> - **Messages:** Basic SQLite FTS5 plus optional embedding search. An plugin can do everything you can imagine if that' not enough.
> - **Evergreen:** Long-term facts that you edit from the CLI and your companion edits through integration tools. No automatic extraction yet.
>   - Think of it as a `USER.md` but with proper lifecycle management and duplication prevention.
> - **Affect:** A deterministic emotion engine that uses values like `{"anger": 0.35}` and shifts over time, including when the user goes unresponsive. Of course, you can make your own!
>   - Message intent analysis can also be an external plugin. drop system_one plugin in examples/ or any other classifier plugin into the plugins directory, and its `decide` listener picks which emotion each user message moves.
> - **Identity:** `identity.md`, or name your own.
>
> </details>

## Plugin

Everything is a plugin in dusha. Identity, memory, evergreen, memo, affect, and proactive are built in that you can alter/disable. You can also create a plugin under the plugins directory holding `pyproject.toml` with a `[tool.dusha]` table. 

```toml
[tool.dusha]
entry = "my_plugin"
inject = ["messages"]
```

The module defines `apply(ctx, config)`, which runs at startup. `config` is the plugin's `options` from `config.json`. `inject` lists the services the plugin calls, including `api`, `cli`, and `mcp` when it registers routes, commands, or tools, and a plugin whose service is missing stays inactive. External plugins use the gateway's Python environment, so extra dependencies must be installed there and a version conflict affects the gateway.

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
| `ctx.mcp` | The gateway's MCP tool registry. `ctx.mcp.tool(name, handler, help=..., args=[...])` adds a tool to `/mcp`, see [Tools](#tools). |
| `ctx.<service>.<method>(...)` | Calls a method of an injected service and returns its result: `messages` (`get`, `recent`, `search`, `context`, `after`), `companion` (`ingest_message`, `build_context`, `latest_route`), `affect` (`status`), `evergreen` (`remember`, `revise`, `forget`, `list_current`, `history`), `memo` (`add`, `list`, `done`), `proactive` (`evaluate`, `poll`, `acknowledge`). |

Services provided by an external plugin are consumed by other plugins through `inject`, and they are direct Python objects.

The first `emotion` in `plugins` order wins. `deltas` from every plugin merge and apply after the increment. Dimension names come from your `emotions.json`.

### Commands

You can add CLI commands to your plugin. The dusha CLI is just another client to access dusha, so for example you can do things like:

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
As you can see, dusha builds `--help` list dynamically from registered commands. But it also caches the last command list, so `--help` works while the server is down.

| Helper | Makes |
| --- | --- |
| `arg(name, type=str, help="", choices=(), optional=False, repeated=False)` | A positional argument. |
| `option(name, type=str, help="", default=None, choices=(), required=False, repeated=False)` | `--name`, with each underscore as a dash. A `bool` option is a flag. |


### Tools & MCP

Dusha exposes a HTTP [MCP](https://github.com/Somme4096/dusha/blob/main/docs/api.md#mcp) per each companion. It takes the same `arg` and `option` specs as a CLI command, so it's pretty easy to add tools for dusha MCP in your own plugin:

```python
from dusha.commands import arg


def apply(ctx, config):
    ctx.mcp.tool(
        "notes_add",
        lambda text: {"note": save(text)},
        help="Add a note the companion keeps.",
        args=[arg("text", help="Note text.")],
    )
```

For clients that you'd like to receive proactive messages from, use integration for each platform under integrations/, or build your own there.

## Documentation

- [docs/configuration.md](https://github.com/Somme4096/dusha/blob/main/docs/configuration.md): JSON settings, identity and prompt files.
- [docs/guide.md](https://github.com/Somme4096/dusha/blob/main/docs/guide.md): service setup, auth and network, backup, and troubleshooting.
- [docs/api.md](https://github.com/Somme4096/dusha/blob/main/docs/api.md): HTTP endpoints and harness integration, including AstrBot.

## Verisioning

Dusha's version always ends in `.21`. Every release bumps only the numbers in front of it (0.0.21 → 0.1.21 → 1.0.21).

<details>
<summary>Why?</summary>

[The weigh of soul is 21 grams](https://en.wikipedia.org/wiki/21_grams_experiment). Dusha is a software with a soul, just like how your companion should be.
</details>

## Acknowledgement

Sincere gratitude to:

- [Drivesoid](https://github.com/A1batr055/Drivesoid) for their 16-emotion system and `base/mood` cycle.
- [Omemo](https://github.com/OmniDimen/omemo) for their gateway-as-memory-layer idea.
