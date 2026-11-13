# Dusha

[![PyPI](https://img.shields.io/pypi/v/dusha)](https://pypi.org/project/dusha/)
[![Python](https://img.shields.io/pypi/pyversions/dusha)](https://pypi.org/project/dusha/)
[![License](https://img.shields.io/github/license/Somme4096/dusha)](LICENSE)
[![CI](https://github.com/Somme4096/dusha/actions/workflows/publish.yml/badge.svg)](https://github.com/Somme4096/dusha/actions/workflows/publish.yml)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)

> [!IMPORTANT]
> This README.md is proudly written by a human (˶>⩊<˶)❤️

**Dusha** (_Rus:_ душа, [dʊˈʂa], "soul") is a lightweight python API that:

1) stores messages, memories, affect state, anything you want
2) injects those contexts at every LLM call (any provider is supported)
3) has an extensible MCP and CLI to let you add any tools.

Because it's just a hackable python API, you can do **literally anything**. Including:

- hop between harnesses and get the same soul on the other side
- write your own emotion system
- call weather API and feed it to the model real-time (so they know it's raining outside)
- feed them your browser history
- let your companion play music on any device through built-in `relay` plugin
- integrate an external memory provider like [Supermemory](https://github.com/supermemoryai/supermemory) and add tools to Dusha MCP

See [dusha-plugin-collections](https://github.com/Somme4096/dusha-plugin-collections).

## Quickstart


```sh
uv tool install dusha # docker image available. See below
dusha init <name> --serve # Also you can use --proxy for proxy setup, or without flags for generating both
dusha serve <name>
```

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

INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8766 (Press CTRL+C to quit)
```

Now in your harness or app, point your endpoint at the URL `provider add` gave you: in this case, `http://127.0.0.1:8766/302ai` and you're good to go. Now every request routes through dusha proxy.

Optionally (highly recommended), add [MCP](https://github.com/Somme4096/dusha/blob/main/docs/api.md#mcp).

### Docker (and Podman)

Prefer a container? The image is `ghcr.io/somme4096/dusha`. We have compose at home:

```sh
mkdir dusha && cd dusha
curl -O https://raw.githubusercontent.com/Somme4096/dusha/main/deploy/compose.yaml
touch .env # Put EMBEDDING_API_KEY=... here if you use one.
docker compose up -d # podman and podman-compose also works.
```

Detail in [Deployment](https://github.com/Somme4096/dusha/blob/main/docs/guide.md#deployment).

## How it works

`user message → dusha proxy → injects context → routed to upstream model → reply → (harness, dusha)`.

Dusha adds context "after" each message so cache can hit.

<details>
<summary>How do messages, memory, affect, and identity work in stock dusha?</summary>

- **Messages:** Basic SQLite FTS5 plus optional embedding search. A plugin can do everything you can imagine if that's not enough.
- **Evergreen:** Long-term facts that you edit from the CLI and your companion edits through integration tools. No automatic extraction.
  - Think of it as a `USER.md` but with proper lifecycle management and duplication prevention.
- **Affect:** A deterministic emotion engine that uses values like `{"anger": 0.35}` and shifts over time, including when the user goes unresponsive. Of course, you can make your own!
  - Message intent analysis can also be an external plugin. drop the [system_one](https://github.com/Somme4096/dusha-plugin-collections/tree/main/system_one) plugin or any other classifier plugin into the plugins directory, and its `decide` listener picks which emotion each user message should provoke.
- **Identity:** `identity.md`, or custom name.

</details>

## Plugin

Everything is a plugin in dusha! Identity, memory, evergreen, memo, affect, and proactive are built in that you can alter/disable. 

See [Plugin](https://github.com/Somme4096/dusha/blob/main/docs/plugins.md) for list of built-in plugins and how to build your own plugin. If you like, you can also take a look at [dusha-plugin-collections](https://github.com/Somme4096/dusha-plugin-collections) which contain dusha plugins I use daily.

## See Also

- [docs/configuration.md](https://github.com/Somme4096/dusha/blob/main/docs/configuration.md): JSON settings, identity and prompt files.
- [docs/guide.md](https://github.com/Somme4096/dusha/blob/main/docs/guide.md): service setup, auth and network, backup, and troubleshooting.
- [docs/api.md](https://github.com/Somme4096/dusha/blob/main/docs/api.md): HTTP endpoints, the proxy, and MCP.
- [docs/plugins.md](https://github.com/Somme4096/dusha/blob/main/docs/plugins.md): the built-in plugins, their commands and tools, and the Plugin API.
- [dusha-plugin-collections](https://github.com/Somme4096/dusha-plugin-collections): My daily-driving dusha plugins. Useful as reference implementation.

## Versioning

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
