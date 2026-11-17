# Plugins

Every companion feature in Dusha is a plugin. The built-in plugins are `identity`, `memory`, `evergreen`, `memo`, `affect`, `proactive`, and `relay`.

Only plugins listed under `plugins` in `config.json` are loaded.

 `dusha companion add` pre-populates all built-in plugins except `relay`. Set `"disabled": true` on an entry to turn a plugin off while keeping its settings in `config.json`, or remove the entry from `plugins` to skip loading it altogether. For plugin configuration keys, see [configuration.md](configuration.md#plugins). For HTTP endpoints, see [api.md](api.md#endpoints).

## Built-in plugins

Each built-in plugin is implemented as a module with the same name under `src/dusha/`.

### `identity`

Reads `identity.md` (or the file configured at [`plugins.identity.path`](configuration.md#identity)) and prepends its text verbatim to every context build. If the file is empty or does not exist when `path` is omitted, no block is added. `identity` registers no CLI commands, MCP tools, or HTTP routes.

### `memory`

Stores conversation messages in SQLite, indexes them with FTS5 for lexical search, and injects recent conversation history alongside relevant search hits into `<companion_state>` during context builds. The core `messages` service (`MemoryStore`) is always available, while enabling `memory` activates context recall, `/state/v1/memory/*` routes, CLI commands, and MCP tools.

**CLI commands:**

| Command | Description |
| --- | --- |
| `dusha memory search <query> [--limit 8]` | Search the message archive (1 to 50 hits, with 1 neighbor on each side). |
| `dusha memory show <message_id>` | Print a single stored message by ID. |
| `dusha memory recent [--limit 20]` | List the latest stored messages in chronological order. |
| `dusha memory reindex` | Rebuild the SQLite FTS5 search index. |

**MCP tools:**

| Tool | Arguments | Description |
| --- | --- | --- |
| `memory_search` | `query` (str), `limit` (int, default `8`), `context_messages` (int, default `1`) | Search the conversation archive with neighboring messages around each hit. |
| `memory_get` | `message_id` (int), `context_messages` (int, default `1`) | Read one archived message and its surrounding neighbors. |

### `evergreen`

Manages long-term facts under keys (such as `user.preference.editor`) with revision history, priority ordering (`0` to `100`), optional review timestamps, and expiration timestamps.

Evergreen facts are never extracted automatically. Instead, you manage them from the CLI and the companion updates them through MCP tools. Active, unexpired facts are injected inside `<evergreen_facts>`. Provides the `evergreen` service.

**CLI commands:**

| Command | Description |
| --- | --- |
| `dusha evergreen list [--include-inactive] [--due-only] [--limit 100]` | List current facts ordered by priority. |
| `dusha evergreen history <fact_id>` | List all revisions of a fact in order. |
| `dusha evergreen remember <key> <text> [--priority 50] [--source-message-id ID] [--reason TEXT] [--review-after ISO] [--expires-at ISO]` | Store a new fact at revision `1` under a unique active key. |
| `dusha evergreen revise <fact_id> <expected_revision> <text> [--priority INT] [--source-message-id ID] [--reason TEXT] [--review-after ISO \| --clear-review-after] [--expires-at ISO \| --clear-expires-at]` | Write a new revision of an existing fact if `expected_revision` matches the latest revision. |
| `dusha evergreen forget <fact_id> <expected_revision> --reason TEXT [--source-message-id ID]` | Mark a fact as `forgotten` while preserving its revision history. |

**MCP tools:**

| Tool | Arguments | Description |
| --- | --- | --- |
| `evergreen_list` | `include_inactive` (bool, default `false`), `due_only` (bool, default `false`), `limit` (int, default `100`) | List current evergreen facts. |
| `evergreen_remember` | `key` (str), `text` (str), `priority` (int, default `50`), `reason` (str, default `""`), `review_after` (str, optional), `expires_at` (str, optional) | Store a new evergreen fact. |
| `evergreen_revise` | `fact_id` (str), `expected_revision` (int), `text` (str), `priority` (int, optional), `reason` (str, default `""`), `review_after` (str, optional), `clear_review_after` (bool, default `false`), `expires_at` (str, optional), `clear_expires_at` (bool, default `false`) | Replace a fact's text or metadata at `expected_revision`. |
| `evergreen_forget` | `fact_id` (str), `expected_revision` (int), `reason` (str) | Forget a fact at `expected_revision` with a reason. |

### `memo`

Maintains short notes that the companion keeps in view across conversations until it marks them done. Active memos are injected oldest-first inside `<memo_notes>`. Provides the `memo` service.

**CLI commands:**

| Command | Description |
| --- | --- |
| `dusha memo add <text>` | Create an active memo. |
| `dusha memo list [--status active\|archived] [--limit 20]` | List active or archived memos, oldest first. |
| `dusha memo done <note_id> <reason>` | Archive a memo with a completion reason. |

**MCP tools:**

| Tool | Arguments | Description |
| --- | --- | --- |
| `memo_add` | `text` (str) | Add a short memo to keep in view. |
| `memo_list` | `status` (`"active"` or `"archived"`, default `"active"`), `limit` (int, default `20`) | List memos, oldest first. |
| `memo_done` | `note_id` (int), `reason` (str) | Archive a completed memo with a reason. |

### `affect`

Runs a deterministic emotion engine over the dimensions defined in `emotions.json`. Each dimension tracks an immediate `base` value and a slower-moving `mood` value. 

On every new user message, `affect` queries `decide` listeners (and optional [embedding appraisal](configuration.md#embedding)), increments the selected emotion, applies any dimension deltas, and resets unanswered proactive counts. 

Between messages, values decay exponentially toward `mood` and `neutral`, and silence rules drift configured dimensions over time. Injects the `<affect>` block. Provides the `affect` service.

**CLI commands:**

| Command | Description |
| --- | --- |
| `dusha affect show` | Advance affect decay to the current time and print the state. |

**MCP tools:**

| Tool | Arguments | Description |
| --- | --- | --- |
| `affect_status` | *(none)* | Advance decay and return the current `base`, `mood`, and interaction timestamps. |

### `proactive`

Evaluates on a background timer whether the companion should initiate a message based on silence duration, quiet hours, daily send limits, cooldowns, unanswered count, and emotion trigger thresholds. Requires the `affect` plugin enabled. Queued events are leased, delivered, and acknowledged by a harness consumer (see [Proactive delivery](api.md#proactive-delivery)). Provides the `proactive` service.

**CLI commands:**

| Command | Description |
| --- | --- |
| `dusha proactive evaluate` | Run a single evaluation pass immediately and print the queued event or `null`. |
| `dusha proactive poll [--consumer cli] [--limit 1]` | Lease pending proactive events. |
| `dusha proactive ack <event_id> <sent\|failed\|release> [--consumer cli] [--text TEXT] [--error TEXT]` | Acknowledge a leased event. |

### `relay`

Opt-in plugin (`"relay": {}` under `plugins` in [`config.json`](configuration.md#relay)) that connects a hub companion to device plugins (`presence`, `screenshot`, `notification`, `acp`, `music`, `hands`, `voice` from [dusha-plugin-collections](https://github.com/Somme4096/dusha-plugin-collections)).

- Queries the `presence` plugin to resolve `device: "auto"` to the device currently in use.
- Dispatches capabilities locally via in-process events when the target device is the hub itself, or over HTTP (`POST <device_url>/state/v1/...`) for remote devices.
- Injects `<local_time>` (for example, `<local_time>"Monday 14:30"</local_time>`) into every context build.
- Listens for `proactive.sent` to mirror proactive messages as notifications (and optional voice) on the active device.
- Listens for `relay.endpoint` to resolve a device's URL and auth headers for other plugins.

**CLI commands and MCP tools:**

Every MCP tool has a matching CLI command under `dusha relay <command>` (with underscores written as hyphens on the CLI, such as `dusha relay spawn-status`).

| MCP tool | CLI command | Arguments | Description |
| --- | --- | --- | --- |
| `look` | `dusha relay look` | `device` (str, default `"auto"`) | Capture a screenshot from a device. |
| `notify` | `dusha relay notify` | `title` (str), `body` (str, default `""`), `device` (str, default `"auto"`), `urgency` (`"low"`, `"normal"`, `"critical"`, default `"normal"`) | Display a notification on the active desktop or phone. |
| `spawn` | `dusha relay spawn` | `prompt` (str), `device` (str, default `"auto"`), `agent` (str, default `""`), `cwd` (str, default `""`) | Launch a background coding agent on a device and return its `job` ID. |
| `spawn_status` | `dusha relay spawn-status` | `job` (str), `device` (str, default `""`) | Check the status and output of a spawned job. |
| `spawn_wait` | `dusha relay spawn-wait` | `job` (str), `device` (str, default `""`), `timeout_seconds` (float, default `0`) | Poll until a spawned job finishes or times out. |
| `music` | `dusha relay music` | `action` (str, default `"status"`), `query` (str, default `""`), `device` (str, default `"auto"`), `player` (str, default `""`) | Inspect or control media playback (`status`, `play`, `pause`, `toggle`, `next`, `previous`, `stop`, `volume`, `find`, `album`). |
| `shell` | `dusha relay shell` | `command` (str), `device` (str, default `"auto"`), `cwd` (str, default `""`), `timeout_seconds` (float, default `60`) | Execute a shell command on a device. |
| `type_text` | `dusha relay type-text` | `text` (str), `device` (str, default `"auto"`) | Type text into the focused window on a device. |
| `press_key` | `dusha relay press-key` | `key` (str), `device` (str, default `"auto"`) | Press a key or shortcut on a device. |
| `list_windows` | `dusha relay list-windows` | `device` (str, default `"auto"`) | List open windows on a device. |
| `speak` | `dusha relay speak` | `text` (str), `device` (str, default `"auto"`) | Synthesize and play speech on the active device or upload a voice clip to Discord. |

## Make your own plugin

An external plugin is a directory inside `plugins_dir` (`~/.config/dusha/<name>/plugins/` by default) containing a `pyproject.toml` file and a Python module that defines `apply(ctx, config)`.

### 1. Create the plugin directory and `pyproject.toml`

The directory name is the plugin's identifier. It must start with a letter or underscore and contain only letters, digits, and underscores.

```text
~/.config/dusha/<companion>/plugins/
└── notes/
    ├── pyproject.toml
    └── notes.py
```

In `pyproject.toml`, declare `[tool.dusha]` with `entry` (the module name to import) and `inject` (the list of services the plugin accesses on `ctx`):

```toml
[project]
name = "dusha-notes"
version = "0.1.0"

[tool.dusha]
entry = "notes"
inject = ["prompts", "api", "cli", "mcp"]
```

Dusha adds both `<plugin_dir>` and `<plugin_dir>/src` to `sys.path` when loading the entry module, so `entry = "notes"` resolves `notes.py`, `notes/__init__.py`, or `src/notes/__init__.py`.

### 2. Implement `apply(ctx, config)`

`apply(ctx, config)` runs once at server startup. `config` is the `options` dictionary from `plugins.<name>.options` in `config.json`.

```python
from __future__ import annotations

import json
from typing import Any

from dusha import prompts as _prompts
from dusha.commands import CommandError, arg, option
from dusha.hooks import Block, ContextRequest, render

PROMPTS = {
    "open_delimiter": "<notes>",
    "close_delimiter": "</notes>",
}


def apply(ctx: Any, config: dict[str, Any]) -> None:
    max_items = int(config.get("max_items", 5))
    storage = ctx.data_dir / "notes.json"
    slots = {**PROMPTS, **ctx.prompts["plugins"].get(ctx.name, {})}
    delimiters = _prompts.section({"plugins": {ctx.name: slots}}, ctx.name, dict.fromkeys(PROMPTS, str))

    def load() -> list[dict[str, Any]]:
        if not storage.exists():
            return []
        return json.loads(storage.read_text(encoding="utf-8"))

    def add(text: str, pinned: bool = False) -> dict[str, Any]:
        value = text.strip()
        if not value:
            raise CommandError("note text is required")
        items = load()
        note = {"id": len(items) + 1, "text": value, "pinned": pinned}
        items.append(note)
        storage.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        return note

    def on_context(request: ContextRequest) -> Block | None:
        items = load()[-max_items:]
        if not items:
            return None
        return render(ctx.name, delimiters["open_delimiter"], items, delimiters["close_delimiter"])

    ctx.on("context", on_context)

    @ctx.api.get("/state/v1/notes", tags=["state"])
    def list_notes_route() -> dict[str, Any]:
        return {"notes": load()}

    group = ctx.cli.group("notes", "Manage quick notes")
    group.command(
        "add",
        lambda text, pinned: {"note": add(text, pinned)},
        help="Add a note",
        args=[arg("text"), option("pinned", bool, help="Pin the note.")],
    )
    group.command("list", lambda: {"notes": load()}, help="List all notes")

    ctx.mcp.tool(
        "notes_add",
        lambda text, pinned: {"note": add(text, pinned)},
        help="Save a quick note for future turns.",
        args=[
            arg("text", help="Note text."),
            option("pinned", bool, default=False, help="Pin the note."),
        ],
    )
```

### 3. Enable the plugin in `config.json`

Add the plugin under `plugins` in `~/.config/dusha/<companion>/config.json` and restart `dusha serve`:

```json
{
  "plugins": {
    "identity": {},
    "memory": {},
    "evergreen": {},
    "memo": {},
    "affect": {},
    "proactive": {},
    "notes": {
      "options": {
        "max_items": 10
      }
    }
  }
}
```

Verify that the plugin is active and test its CLI and HTTP routes:

```sh
dusha plugin list <companion>
dusha -c <companion> notes add "Check deployment logs" --pinned
curl http://127.0.0.1:8765/state/v1/notes
```

### Dependencies and environment

External plugins execute in the same Python environment as `dusha serve`. If your plugin imports third-party packages, install them into Dusha's environment (for example, `uv tool install dusha --with httpx-sse`). A dependency conflict or an unhandled exception inside `apply(ctx, config)` stops the server from starting.

## Plugin API

### Manifest (`[tool.dusha]`)

Every external plugin requires a `pyproject.toml` containing a `[tool.dusha]` table with only these keys:

| Key | Type | Required | Description |
| --- | --- | --- | --- |
| `entry` | `str` | Yes | Dotted Python module name providing `apply(ctx, config)`. |
| `inject` | `list[str]` | No | Names of core or plugin-provided services that this plugin accesses on `ctx` (defaults to `[]`). |

### Startup loading process

At startup, Dusha resolves `inject` dependencies iteratively:

1. A plugin listed in `config.json` with `"disabled": true` (or `false`) is marked `disabled` and its `apply` function is not called.
2. Any other listed plugin waits in `pending` until every service in its `inject` list is available (either from core services or provided via `ctx.provide()` by an earlier plugin).
3. Once its dependencies are satisfied, `apply(ctx, config)` runs and the plugin state becomes `active`. If `apply` raises an unhandled exception, all active plugins are disposed in reverse order and server startup fails.
4. Any plugin whose `inject` dependencies are still missing after all resolvable plugins have started is marked `inactive` with `"missing service(s): ..."`, and a placeholder route returns `503` for `/state/v1/<name>` and `/state/v1/<name>/*`.
5. If an `active` plugin assigns a non-empty string to `ctx.error` at runtime, `/state/v1/plugins` reports its state as `failed` alongside the error text. Clear `ctx.error = ""` to return the reported state to `active`.

### Context (`ctx`)

The `ctx` object passed to `apply(ctx, config)` exposes the following properties and methods:

| Member | Description |
| --- | --- |
| `ctx.name` | The plugin's registered name (`str`). |
| `ctx.data_dir` | Private directory (`<data_dir>/plugins/<name>/`) as a `pathlib.Path`, created automatically on access. |
| `ctx.error` | Read/write `str`. Setting a non-empty string marks the plugin as `failed` in `GET /state/v1/plugins` and `dusha plugin list`. |
| `ctx.on(event, fn)` | Register `fn` as a listener for `event`. Exceptions raised inside `fn` during dispatch are logged without stopping other listeners. |
| `ctx.emit(event, *args)` | Call all listeners of `event` across active plugins in `plugins` order, ignoring return values. |
| `ctx.collect(event, *args)` | Call all listeners of `event` across active plugins and return `{"<plugin_name>": <first_non_null_result>}`. |
| `ctx.provide(name, value)` | Publish `value` as a named service that other plugins can request in `inject`. Raises `ValueError` if `name` is already provided. |
| `ctx.interval(seconds, fn)` | Schedule `fn()` to run repeatedly in a worker thread every `max(1, seconds)` seconds while the server is running. |
| `ctx.on_dispose(fn)` | Register a cleanup callback `fn()` executed in reverse registration order on server shutdown. |
| `ctx.<service>` | Access a service listed in `inject` (or published by the same plugin via `ctx.provide`). Accessing an uninjected service raises `AttributeError`. |

### Hooks and events

Register listeners with `ctx.on(event, fn)`:

| Event | Listener argument | Expected return value |
| --- | --- | --- |
| `claim` | `offer: dict[str, Any]` with `harness`, `conversation_id`, `text`, `route` | Called before a `user` message is stored. If any plugin returns a non-`None` value, the message role is changed from `"user"` to `"event"`, preventing `affect` and `proactive` from treating it as a user turn. |
| `message` | `stored: dict[str, Any]` with `id`, `conversation_id`, `role`, `text`, `content`, `external_id`, `occurred_at`, `ingested_at`, `harness`, `external_conversation_id`, `route` | Runs once for each newly stored (non-duplicate) message. If a listener returns a non-`None` value, `POST /state/v1/messages` includes it in the response under `"<plugin_name>"`. |
| `context` | `request: ContextRequest` | Runs on every context build for plugins allowed by the harness filter. Return a `Block` from `dusha.hooks` or `None`. |
| `decide` | `request: DecisionRequest` | Runs on each new `user` message when `affect` is active. Return `{"emotion": "<dimension>"}`, `{"deltas": {"<dimension>": <float>}}`, both, or `None`. |
| `<custom>` | `*args` passed to `ctx.emit` or `ctx.collect` | Any value (ignored by `emit`, collected per plugin by `collect`). |

Dataclasses and helpers in `dusha.hooks`:

```python
from dusha.hooks import Block, ContextRequest, DecisionRequest, render
```

- **`ContextRequest`**:
  - `query: str = ""`
  - `harness: str = ""`
  - `conversation_id: str = ""`
  - `exclude_message_ids: tuple[int, ...] = ()`
  - `include_recent: bool = True`
- **`DecisionRequest`**:
  - `message: str`
  - `emotions: dict[str, dict[str, Any]]` (resolved dimension specs from `emotions.json`)
  - `state: dict[str, Any]` (current affect snapshot)
  - `instruction: str` (`plugins.affect.decision_instruction` from `prompts.json`)
- **`Block`**:
  - `name: str` (plugin name)
  - `text: str` (formatted text appended to `injection`, empty string to omit from the raw text)
  - `data: Any` (structured payload placed under `context.blocks[name]`)
  - `session: tuple[dict[str, Any], ...] = ()` (conversation records appended to `<companion_state>`)
  - `instruction: str = ""` (instruction string added to `<companion_state>.instructions[name]`)
- **`render(name: str, open_delimiter: str, data: Any, close_delimiter: str) -> Block`**:
  - Serializes `data` as compact JSON with `<`, `>`, and `&` escaped as `\u003c`, `\u003e`, and `\u0026`, wraps it in the delimiters, and returns `Block(name, rendered_text, data)`.

### Services

List a service name in `inject` to access it as `ctx.<service>`:

| Service | Source | Description and key methods |
| --- | --- | --- |
| `settings` | Core | The loaded `AppConfig` instance (`home`, `data_dir`, `host`, `port`, `timezone`, `api_token`, `plugins_dir`, `embedding`, `plugins`, `providers`, `harnesses`). |
| `database` | Core | The server's SQLite `Database` (`connect()`, `transaction(immediate=False)`, `migrate(sql)`, `backup(destination)`, `integrity_check()`). |
| `prompts` | Core | Resolved prompts mapping. Pass to `dusha.prompts.section(ctx.prompts, ctx.name, spec, templates)` to validate and read plugin prompt slots. |
| `providers` | Core | `Providers` registry from `config.json`: `names() -> list[str]`, `get(name) -> Provider`. A `Provider` offers `url(kind=None) -> str` (`kind` in `"chat"`, `"messages"`, `"responses"`) and `request_headers(conversation_id="") -> dict[str, str]` with `{env:VAR}` and `{conversation_id}` expanded. |
| `messages` | Core | `MemoryStore`: `get(message_id)`, `recent(conversation_id=None, limit=10, exclude_ids=None)`, `search(query, limit=8, context_messages=1, exclude_ids=None)`, `context(message_id, radius=1)`, `after(cursor, limit)`, `ingest(...)`, `latest_route(harness="")`. |
| `companion` | Core | `CompanionService`: `ingest_message(...)`, `ingest_transcript(...)`, `build_context(...)`, `latest_route(harness="")`, `plugins_status()`, `backup(destination)`. |
| `api` | Core | FastAPI `APIRouter` mounted on the server with authentication applied. Register custom routes under `/state/v1/<plugin_name>`. |
| `cli` | Core | `Commands` registry for adding CLI commands and groups (see [Commands](#commands)). |
| `mcp` | Core | `Tools` registry for adding MCP tools (see [Tools & MCP](#tools--mcp)). |
| `evergreen` | `evergreen` plugin | `EvergreenStore`: `remember(...)`, `revise(...)`, `forget(...)`, `list_current(include_inactive=False, due_only=False, limit=100)`, `history(fact_id)`. |
| `memo` | `memo` plugin | `MemoStore`: `add(text)`, `list(status="active", limit=20)`, `done(note_id, reason)`. |
| `affect` | `affect` plugin | `AffectEngine`: `status()`, `record_user_message(...)`, `on_proactive_sent()`, `describe(snapshot)`. |
| `proactive` | `proactive` plugin | `ProactiveEngine`: `evaluate()`, `poll(consumer, limit=1, harness="")`, `acknowledge(event_id, consumer, outcome, *, text="", external_id="", error="")`. |

### Commands

Inject `"cli"` to register top-level commands or command groups that the `dusha` CLI discovers dynamically from `GET /cli/v1/commands`:

```python
from dusha.commands import CommandError, arg, option


def apply(ctx, config):
    notes = ctx.cli.group("notes", "Manage notes")
    notes.command(
        "add",
        lambda text, pinned, tags: {"note": save(text, pinned, tags)},
        help="Add a note",
        args=[
            arg("text"),
            option("pinned", bool, help="Pin the note."),
            option("tags", repeated=True, help="Tag names."),
        ],
    )
```

- `ctx.cli.group(name, help="") -> Group`: Creates a subcommand group (`dusha <group> <command>`). Group and command names can use letters, digits, dashes, and underscores, and cannot use reserved core command names (`init`, `serve`, `proxy`, `provider`, `companion`, `plugin`, `health`, `backup`).
- `ctx.cli.command(name, handler, *, help="", args=())`: Registers a top-level command (`dusha <name>`).
- `group.command(name, handler, *, help="", args=())`: Registers a subcommand inside `group`.
- `handler(**values)` receives the validated arguments as keyword arguments and returns any JSON-serializable value, which the CLI pretty-prints as JSON. Raise `CommandError("reason")`, `ValueError("reason")`, or `KeyError("id")` to return HTTP `400` and display an `Error!` message in the CLI.

Argument and option builders in `dusha.commands`:

| Builder | Signature and behavior |
| --- | --- |
| `arg` | `arg(name, type=str, *, help="", choices=(), optional=False, repeated=False)`. Positional argument (`type` in `str`, `int`, `float`). Cannot be `bool`. Required positional arguments must precede optional ones, and only the last positional argument can set `repeated=True`. |
| `option` | `option(name, type=str, *, help="", default=None, choices=(), required=False, repeated=False)`. Flag or named option (`--name` with underscores converted to dashes). When `type=bool`, creates a boolean flag (`--flag`, or `--flag/--no-flag` when `default=True`). |

### Tools & MCP

Inject `"mcp"` to expose tools at `POST /mcp` using the same `arg` and `option` builders:

```python
from dusha.commands import arg, option


def apply(ctx, config):
    ctx.mcp.tool(
        "notes_add",
        lambda text, pinned: {"note": save(text, pinned)},
        help="Add a note the companion keeps.",
        args=[
            arg("text", help="Note text."),
            option("pinned", bool, default=False, help="Pin the note."),
        ],
        not_found="note not found",
    )
```

- `ctx.mcp.tool(name, handler, *, help="", args=(), not_found="not found")`: Registers an MCP tool. Tool names can contain `1` to `128` letters, digits, underscores, dots, and dashes.
- In MCP JSON Schema generation, `arg(...)` and `option(..., required=True)` become required properties, while `option(..., required=False)` becomes an optional property with `default`.
- If `handler` raises `KeyError`, the MCP response returns `isError: true` with `{"detail": not_found}`. Raising `CommandError` or `ValueError` returns `isError: true` with the exception message in `detail`.

### Custom prompt slots

Inject `"prompts"` and call `dusha.prompts.section(prompts, name, spec, templates=None)` to read user-overridable prompt slots from [`prompts.json`](configuration.md#promptsjson) under `plugins.<name>`:

```python
from dusha import prompts as _prompts

DEFAULTS = {
    "open_delimiter": "<notes>",
    "close_delimiter": "</notes>",
    "greeting": "Saved {count} notes.",
}
SPEC = {
    "open_delimiter": str,
    "close_delimiter": str,
    "greeting": str,
}
TEMPLATES = {
    "greeting": {"count": 0},
}


def apply(ctx, config):
    merged = {"plugins": {ctx.name: {**DEFAULTS, **ctx.prompts["plugins"].get(ctx.name, {})}}}
    texts = _prompts.section(merged, ctx.name, SPEC, TEMPLATES)
```

`spec` maps each slot name to `str`, `list` (non-empty list of strings), or a nested spec `dict`. `templates` validates Python `.format()` placeholders at startup and rejects unknown placeholders.

## See also

- [configuration.md](configuration.md): Plugin settings, `emotions.json`, and `prompts.json`.
- [api.md](api.md): HTTP routes and MCP protocol specification.
- [guide.md](guide.md): CLI usage, harness setup, and troubleshooting.
- [dusha-plugin-collections](https://github.com/Somme4096/dusha-plugin-collections): Reference external plugins (`system_one`, `presence`, `ambient`, `acp`, `everos_memory`, and more).
