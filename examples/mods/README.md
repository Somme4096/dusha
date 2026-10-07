# Plugins

Optional decision plugins load from `decision.mods_dir`. A plugin directory is
identified by `decision.module` and exports `decide(request, options)`.

`decide(request, options)`, where request has `message`, `emotions`, `state`,
and `instruction`. Return `{"emotion": name}` or `None`.

Plugins run in a dedicated `uv` environment and child process. OS permissions
are inherited intentionally. They are not containers or OS sandboxes. A plugin
must not assume credentials exist in inherited environment variables.

## Memory plugins

Memory plugins use `memory_plugin.module` and `memory_plugin.mods_dir`. `module`
defaults to empty, which disables the plugin. Every exported operation has the
Python contract `fn(request, options)`. The gateway stores messages and
evergreen facts itself, pushes committed message batches to the plugin with
scheduled catch-up, and calls the plugin for context injection. See the
[project README](https://github.com/Somme4096/sophia) for the public API.

The [EverOS plugin](everos-memory/README.md) is the shipped memory plugin. It
owns the EverOS sidecar delivery path, the outbox, and episodic extraction, and
it writes only `everos_*` extension tables alongside the core database. It
mirrors `user` and `assistant` messages and never creates or migrates core
message, fact, affect, or proactive tables. Install it by copying the plugin
directory into the configured mods directory. The gateway creates its isolated
`uv` environment from the plugin's `pyproject.toml` and `uv.lock`.

## `system_one`

Pure HTTP adapter for a running [llama.cpp server](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
with decision-model support (`POST /v1/systemone`). No `torch` dependency.

Install the plugin directory into the gateway's mods directory:

```sh
mkdir -p ~/.config/dusha/sophia/mods
cp -R examples/mods/system_one ~/.config/dusha/sophia/mods/system_one
```

Options passed to the mod:

| option            | required | meaning                                                        |
| ----------------- | -------- | -------------------------------------------------------------- |
| `base_url`        | yes      | SystemOne server root, e.g. `http://127.0.0.1:8000`. No default.    |
| `timeout_seconds` | no       | Finite positive timeout; default `10`.                         |
| `api_key`         | no       | Explicit bearer token supplied to the plugin.                |
| `model`           | no       | SystemOne checkpoint name; omitted so the server auto-selects.      |
| `allowed_ips`     | no       | Literal IPv4 or IPv6 addresses allowed for all requests.      |

The allowed choices are exactly the keys of `request.emotions`; each is sent
with its `description` when present, otherwise a human-readable dimension name.
Set `description` on a dimension in `emotions.json` to supply one.
