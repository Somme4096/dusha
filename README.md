# Dusha

> [!NOTE]
> *This README.md is proudly written by a human (and a little bit by Claude (˶>⩊<˶)❤️)*

*Dusha* (Russian: душа, [dʊˈʂa], "soul") is a small HTTP API and a proxy that stores messages, identity, memories, affect state—anything makes your agent yours— outside your harness. Any client that can add a custom provider are able to use it, so you can hop between harnesses (or even use multiple at once!) and still get the same soul on the other side.

Almost everything is configurable through JSON. For things that aren't, dusha is also built on [everything-is-a-plugin](https://github.com/deepseek-ai/deepseek-harness) architecture, so you can easily wire an external memory provider, or do crazy things like writing your own emotion system!

## Quickstart


```sh
uv tool install dusha
dusha init <name>
dusha serve <name>
```

`init` makes the folder `~/.config/dusha/<name>/` and writes a `config.json`, then `serve` starts the companion. `serve` also writes the file when it is missing.
Then on the same or another device, run:

```console
$ dusha proxy <name>

<name> isn't connected to a server yet.
? Connect to: (Use arrow keys)
   The server on this machine (http://127.0.0.1:8765)
 » Another server

? Server URL http://100.145.95.87:8765
? Server token, empty for none **********

Connected to http://100.145.95.87:8765.
Saved ~/.config/dusha/<name>/config.json.
```

Add a custom provider:

```console
$ dusha -c <name> provider add # Add a provider profile from the models.dev catalog

? Provider (Use arrow keys, type to filter)
 » 302.AI (302ai)
   Abacus (abacus)
   abliteration.ai (abliteration-ai)
   above.dev (above)
   AgentRouter (agentrouter)
   Agnes AI (agnes)
   ...

Success!
New provider 302ai added to <name> at http://127.0.0.1:8766/302ai.
Restart dusha proxy <name> and dusha serve <name> to use it.

$ dusha proxy <name>

INFO:     Started server process [3970524]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8766 (Press CTRL+C to quit)
```

Now in your harness or app, point your endpoint at the URL `provider add` gave you: in this case, `http://127.0.0.1:8766/302ai` and you're good to go.

Optionally (but recommended), add [MCP](https://github.com/Somme4096/dusha/blob/main/docs/api.md#mcp).

## How it works

`user message → dusha proxy → injects context → routed to upstream model → reply → (harness, dusha)`.

Dusha adds context "after" each message so cache can hit.

<details>
<summary>How do messages, memory, affect, and identity work in Dusha by default?</summary>

- **Messages:** Basic SQLite FTS5 plus optional embedding search. An plugin can do everything you can imagine if that's not enough.
- **Evergreen:** Long-term facts that you edit from the CLI and your companion edits through integration tools. No automatic extraction.
  - Think of it as a `USER.md` but with proper lifecycle management and duplication prevention.
- **Affect:** A deterministic emotion engine that uses values like `{"anger": 0.35}` and shifts over time, including when the user goes unresponsive. Of course, you can make your own!
  - Side note is that Message intent analysis can also be an external plugin. drop the [system_one](https://github.com/Somme4096/dusha-plugin-collections/tree/main/system_one) plugin or any other classifier plugin into the plugins directory, and its `decide` listener picks which emotion each user message moves.
- **Identity:** `identity.md`, or name your own.

</details>

## Plugin

Everything is a plugin in dusha. Identity, memory, evergreen, memo, affect, and proactive are built in that you can alter/disable. You can also create a plugin under the plugins directory holding `pyproject.toml` with a `[tool.dusha]` table. 

```toml
[tool.dusha]
entry = "my_plugin"
inject = ["messages", "cli", "mcp"]
```

The module defines `apply(ctx, config)`, which runs at startup with the plugin's `options` from `config.json`. `inject` lists the services the plugin calls, and a plugin whose service is missing stays inactive. External plugins share the gateway's Python environment, so install their dependencies there.

| Call | Effect |
| --- | --- |
| `ctx.on("message", fn)` | `fn(message)` runs for every stored message, in id order. |
| `ctx.on("context", fn)` | `fn(request)` runs for every context build. Return a `Block` from `dusha.hooks` or `None`. |
| `ctx.on("decide", fn)` | `fn(request)` runs for every new user message while affect is on. Return `{"emotion": "<dimension>"}`, `{"deltas": {"<dimension>": <number>}}`, both, or `None`. |
| `ctx.on("<event>", fn)` | `fn(*args)` runs when a plugin emits `<event>`. |
| `ctx.interval(seconds, fn)` | `fn()` runs on that interval. |
| `ctx.on_dispose(fn)` | `fn()` runs on shutdown. |
| `ctx.data_dir` | The plugin's private directory. |
| `ctx.name` | The plugin's name. |
| `ctx.error` | A non-empty string marks the plugin failed in `/state/v1/plugins`. |
| `ctx.provide(name, value)` | Publishes a service other plugins can `inject`. |
| `ctx.emit(event, ...)` | Calls every listener of `event`. |
| `ctx.collect(event, ...)` | Calls every listener and returns `{"<plugin>": <result>}`. |
| `ctx.api` | The gateway's FastAPI router. |
| `ctx.cli` | The command registry, see [Commands](#commands). |
| `ctx.mcp` | The MCP tool registry, see [Tools](#tools--mcp). |
| `ctx.<service>.<method>(...)` | Calls an injected service: `messages`, `companion`, `affect`, `evergreen`, `memo`, `proactive`, `providers`. |

The first `emotion` in `plugins` order wins, and `deltas` from every plugin merge. See the [Plugin API](https://github.com/Somme4096/dusha/blob/main/docs/guide.md#plugin-api) for hook fields and service methods.

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

With version `1.0.21`, dusha entered `5:Stable/Production` on PyPI. I will avoid breaking changes in minor and patch releases and deprecate features before removing them.

Note that dusha's version always ends in `.21`. Every release bumps only the numbers in front of it (0.0.21 → 0.1.21 → 1.0.21).

<details>
<summary>Why?</summary>

[The weight of soul is said to be 21 grams](https://en.wikipedia.org/wiki/21_grams_experiment). Dusha is a software with a soul, and I hope it stays that way forever.
</details>

## Acknowledgement

Sincere gratitude to:

- [Drivesoid](https://github.com/A1batr055/Drivesoid) for their 16-emotion system and `base/mood` cycle.
- [Omemo](https://github.com/OmniDimen/omemo) for their gateway-as-memory-layer idea.
