# Dusha

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

Integrations (e.g. opencode-dusha) send each message to `dusha` and add its context to the system prompt before calling your model. That's it, so it works with any harness or provider. If your client only supports the OpenAI API, use `dusha` as a proxy instead. Set the client's base URL to `http://127.0.0.1:8765/v1/to/<provider-host>/v1` and keep your own key and model, see [passthrough](https://github.com/Somme4096/dusha/blob/main/docs/api.md#passthrough).

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

Every part of Dusha is a plugin. Identity, memory, evergreen, memo, affect, and proactive are built in, and a plugin you add is an external plugin: a directory under the plugins directory holding `pyproject.toml` and `uv.lock`. The gateway installs it into its own `uv` environment, runs it as a long-lived child process, and imports the module named in `[tool.dusha]`:

```toml
[tool.dusha]
entry = "my_plugin"
inject = ["messages"]
```

The module defines `apply(ctx, config)`, which runs when the external plugin loads and again after a restart. `config` is the plugin's `options` from `config.json` plus `context_max_chars`. `inject` lists the services the plugin calls, and a plugin whose service is missing stays inactive.

| Call | Effect |
| --- | --- |
| `ctx.on("message", fn)` | `fn(message)` runs for every stored message, in id order, with the stored row: `id`, `conversation_id`, `role`, `text`, `content`, `external_id`, `occurred_at`, `ingested_at`, `sha256`, `harness`, `external_conversation_id`, `route`. An exception leaves the message undelivered, and the gateway retries it before any later one. |
| `ctx.on("context", fn)` | `fn(request)` runs for every context build with `query`, `harness`, `conversation_id`, `exclude_message_ids`, and `include_recent`. Return `{"text": "...", "records": [{"source": "...", "text": "..."}]}` or `None`. |
| `ctx.on("decide", fn)` | `fn(request)` runs for every new user message while affect is on, with `message`, `emotions` (the resolved dimensions), `state` (the affect snapshot), and `instruction`. Return `{"emotion": "<dimension>"}`, `{"deltas": {"<dimension>": <number>}}`, both, or `None`. |
| `ctx.interval(seconds, fn)` | `fn()` runs on that interval while the gateway serves. |
| `ctx.on_dispose(fn)` | `fn()` runs on shutdown. |
| `ctx.data_dir` | A private directory, `plugins/<name>` under `data_dir`. |
| `ctx.<service>.<method>(...)` | Calls a method of an injected service and returns its JSON result: `messages` (`get`, `recent`, `search`, `context`, `after`), `companion` (`ingest_message`, `build_context`, `latest_route`), `affect` (`status`), `evergreen` (`remember`, `revise`, `forget`, `list_current`, `history`), `memo` (`add`, `list`, `done`), `proactive` (`evaluate`, `poll`, `acknowledge`). |

The first `emotion` in `plugins` order wins. `deltas` from every plugin merge and apply after the increment. Dimension names come from your `emotions.json`.

## Documentation

- [docs/configuration.md](https://github.com/Somme4096/dusha/blob/main/docs/configuration.md): JSON settings, identity and prompt files.
- [docs/guide.md](https://github.com/Somme4096/dusha/blob/main/docs/guide.md): service setup, auth and network, backup, and troubleshooting.
- [docs/api.md](https://github.com/Somme4096/dusha/blob/main/docs/api.md): HTTP endpoints and harness integration, including AstrBot.

## Acknowledgement

Sincere gratitude to:

- [Drivesoid](https://github.com/A1batr055/Drivesoid) for their 16-emotion system and `base/mood` cycle.
- [Omemo](https://github.com/OmniDimen/omemo) for their gateway-as-memory-layer idea.
