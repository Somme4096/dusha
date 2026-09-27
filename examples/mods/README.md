# Decision plugins

Optional decision plugins. The gateway loads a plugin directory named by
`decision.module` from `decision.mods_dir`. A plugin exports one function:

`decide(request, options)`, where request has `message`, `emotions`, `state`,
and `instruction`. Return `{"emotion": name}` or `None`.

Plugins run in a dedicated `uv` environment and child process. OS permissions
are inherited intentionally. They are not containers or OS sandboxes. A plugin
must not assume credentials exist in inherited environment variables.

## Memory plugins

The gateway also supports memory plugins for message ingestion, recall,
evergreen facts, and optional affect phrase matching. Memory plugins follow the
same convention as decision plugins. They export `remember`, `recall`, `forget`,
`render_facts`, and `match_phrase`, each receiving `(request, options)`.

The SQLite example is available in
[`sqlite-memory`](sqlite-memory/README.md). Install it by copying the plugin
directory into the configured mods directory. The gateway creates its isolated
`uv` environment from the plugin's `pyproject.toml` and `uv.lock`.

## `laya`

Pure HTTP adapter for a running [Laya](https://github.com/NandhaKishorM/laya)
service (`POST /v1/systemone`). No `laya`/`torch` dependency.

Install the plugin directory into the gateway's mods directory:

```sh
mkdir -p ~/.config/companion-gateway/mods
cp -R examples/mods/laya ~/.config/companion-gateway/mods/laya
```

Options passed to the mod:

| option            | required | meaning                                                        |
| ----------------- | -------- | -------------------------------------------------------------- |
| `base_url`        | yes      | Laya server root, e.g. `http://127.0.0.1:8000`. No default.    |
| `timeout_seconds` | no       | Finite positive timeout; default `10`.                         |
| `api_key`         | no       | Explicit bearer token supplied to the plugin.                |
| `model`           | no       | Laya checkpoint name; omitted so the server auto-selects.      |
| `allowed_ips`     | no       | Literal IPv4 or IPv6 addresses allowed for all requests.      |

The allowed choices are exactly the keys of `request.emotions`; each is sent
with its `description` when present, otherwise a human-readable dimension name.
