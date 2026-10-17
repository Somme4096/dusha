# SystemOne external plugin

This external plugin listens for `decide`. It calls a running [llama.cpp server](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
with decision-model support (`POST /v1/systemone`) and returns the emotion the
server picks. The external plugin contract is in the project
[README](https://github.com/Somme4096/dusha#plugin-api).

## Install

```sh
mkdir -p ~/.config/dusha/dusha/plugins
cp -R examples/plugins/system_one ~/.config/dusha/dusha/plugins/system_one
```

```json
{
  "plugins": {
    "system_one": {
      "timeout_seconds": 15,
      "options": {
        "base_url": "http://127.0.0.1:8000",
        "timeout_seconds": 10
      }
    }
  }
}
```

## Options

| Option            | Required | Meaning                                                              |
| ----------------- | -------- | -------------------------------------------------------------------- |
| `base_url`        | yes      | SystemOne server root, for example `http://127.0.0.1:8000`.          |
| `timeout_seconds` | no       | Finite positive HTTP timeout, default `10`. Keep it below the external plugin's `timeout_seconds`. |
| `api_key`         | no       | Bearer token sent with every request.                                |
| `model`           | no       | Checkpoint name. Omitted, the server picks.                          |
| `allowed_ips`     | no       | Literal IPv4 or IPv6 addresses every request and redirect must use.  |

The allowed choices are the keys of `request.emotions`. Each is sent with its
`description` when present, otherwise a human-readable dimension name. Set
`description` on a dimension in `emotions.json` to supply one.

A cross-origin redirect drops the bearer token. An HTTPS to HTTP redirect is
refused.

## Tests

`tests/test_system_one_e2e.py` runs the external plugin against a local stand-in for the
SystemOne server. `uv run pytest` from the project root collects it.
