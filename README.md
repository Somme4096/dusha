# Dusha

> *This README.md is proudly written by a human (and a little bit by Claude (˶>⩊<˶)❤️)*

*Dusha* (Russian: душа, [dʊˈʂa], "soul") is a small HTTP API that stores messages, identity, facts, and affect state outside your harness. Any client that can POST a message and read back an injection string can use it, so you can hop between harnesses (or even use multiple at once!) and still get the same soul on the other side.

Every prompt, emotional vector, and threshold is configurable through JSON. Dusha also has an *extensible plugin system* that lets you wire any external memory provider easily!

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

Integrations (e.g. opencode-dusha) send each message to `dusha` and add its context to the system prompt before calling your model. That's it, so it works with any harness or provider. If your client only supports the OpenAI API, use `dusha` as a proxy at `/v1/chat/completions` instead.

<details>
<summary>How does memory/affect/identity work in Dusha?</summary>


- Messages: Basic SQLite FTS5 + optional embedding search. Also `~/.config/dusha/<name>/mods/` for external memory providers integration.
- Evergreen: Long-term facts that you edit from the CLI and your companion edits through integration tools. No automatic extraction 
	* think of it as a USER.md that actually has good lifecycle management and duplication prevention
- Affect: A deterministic emotion engine with decay and silence drift. Proactive messages fire on thresholds. 
	* And yeah, message intent analysis can be a plugin too: drop Jev or any other classifier into `~/.config/dusha/<name>/mods/` and it picks which emotion each user message moves.
- Identity: `identity.md`, or name one your own.

</details>

## Plugin API

A plugin is a directory under the mods directory holding `README.md`, `main.py`, `pyproject.toml`, and `uv.lock`. 

A decision plugin exports one operation.

| Operation | Request fields | Returns |
| --- | --- | --- |
| `decide` | `message`, `emotions` (the resolved dimensions), `state` (the affect snapshot), `instruction` | `{"emotion": "<dimension>"}` or `None` |

A memory plugin exports these operations. The API reads only the fields listed.

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

`match_phrase` deltas use dimension names from your `emotions.json`. 

## Documentation

- [docs/configuration.md](https://github.com/Somme4096/dusha/blob/main/docs/configuration.md): JSON settings, identity and prompt files.
- [docs/guide.md](https://github.com/Somme4096/dusha/blob/main/docs/guide.md): service setup, auth and network, backup, and troubleshooting.
- [docs/api.md](https://github.com/Somme4096/dusha/blob/main/docs/api.md): HTTP endpoints and harness integration, including AstrBot.

## Acknowledgement

Sincere gratitude to:

- [Drivesoid](https://github.com/A1batr055/Drivesoid) for their 16-emotion system and `base/mood` cycle.
- [Omemo](https://github.com/OmniDimen/omemo) for their gateway-as-memory-layer idea.
