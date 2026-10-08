# Dusha

> *This README.md is proudly written by a human (and a little bit by Claude (˶>⩊<˶)❤️)*

*Dusha* (Russian: душа, [dʊˈʂa], "soul") is a small HTTP API that stores messages, identity, evergreen facts, and affect state outside your harness. Any client that can POST a message and read back an injection string can use it. (You can also use it across multiple harnesses!)

Every prompt, emotional vector, and threshold is configurable through JSON.

<details>
<summary>How does memory/affect/identity work?</summary>

- Messages: Basic SQLite FTS5 + optional embedding search. Also `~/.config/dusha/<name>/mods/` for external memory providers integration.
- Evergreen: Long-term facts that you edit from the CLI and your companion edits through integration tools. No automatic extraction (think of it as a USER.md that actually has good lifecycle management and duplication prevention)
- Affect: A deterministic emotion engine with decay and silence drift. Proactive messages fire on thresholds. And hey, message intent analysis is a plugin too: drop Jev or any other classifier into `~/.config/dusha/<name>/mods/` and it picks which emotion each user message moves.
- Identity: `identity.md`.

</details>

## How to run it

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```sh
mkdir -p ~/.config/dusha/dusha
cp config.example.json ~/.config/dusha/dusha/config.json
uv tool install --editable .
dusha serve dusha
```

The first two lines make a folder for a companion named `dusha` and give it a config. Use any name you like. If your shell cannot find `dusha` after the install, run `uv tool update-shell`.

Check that it is up:

```sh
curl http://127.0.0.1:8765/health
```

You should see `"status": "ok"`. If you don't, look at [docs/guide.md](docs/guide.md).

Everything about one companion is in `~/.config/dusha/<name>/`. Including: 

- its `config.json`
- the emotion and prompt files
- `mods/`, and `data/` with the database.
- It respects `XDG_CONFIG_HOME` too.

### Advanced setup

Want a second companion? Make a second folder and give it a different `port`. `dusha list` shows all of them, and `dusha -c <name> <command>` talks to one.

Want it running in the background? Use [deploy/dusha@.service](deploy/dusha@.service) with the [service setup steps](docs/guide.md#run-as-a-systemd-service). (Linux only. Tell your agents to make PR for Windows, Mac, BSD or whatever, because I don't use them at all!)

## How it works

One turn of chat is four steps.

1. Your harness sends the user's message to `POST /state/v1/messages`. `dusha` stores it and updates the emotions.
2. Your harness calls `POST /state/v1/context` and gets one string back. It holds the identity, the evergreen facts, memos, recalled messages, and the current emotions.
3. Your harness puts that string in the system prompt and calls the model.
4. Your harness sends the reply to `POST /state/v1/messages` so it is remembered too.

Your harness is the one calling the chat model, so `dusha` does not care which harness or provider you use. If your client only speaks the OpenAI API, point it at `/v1/chat/completions` and `dusha` runs all four steps as a proxy.

Proactive messages go the other way. `dusha` decides it is time to say something, your harness picks the event up from `GET /state/v1/proactive/events`, sends it, and reports back.

The full request and response shapes are in the [API and integration guide](docs/api.md).

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
