# Decision suites

Optional decision suites. The gateway loads a suite directory named by
`decision.module` from `decision.mods_dir`. A suite exports one function:

`decide(request, options)`, where request has `message`, `emotions`, `state`,
and `instruction`. Return `{"emotion": name}` or `None`.

Suites run in a dedicated `uv` environment and child process. OS permissions
are inherited intentionally. They are not containers or OS sandboxes. A suite
must not assume credentials exist in inherited environment variables.

## `laya`

Pure HTTP adapter for a running [Laya](https://github.com/NandhaKishorM/laya)
service (`POST /v1/systemone`). No `laya`/`torch` dependency.

Install the suite directory into the gateway's mods directory:

```sh
mkdir -p ~/.config/companion-gateway/mods
cp -R examples/mods/laya ~/.config/companion-gateway/mods/laya
```

Options passed to the mod:

| option            | required | meaning                                                        |
| ----------------- | -------- | -------------------------------------------------------------- |
| `base_url`        | yes      | Laya server root, e.g. `http://127.0.0.1:8000`. No default.    |
| `timeout_seconds` | no       | Finite positive timeout; default `10`.                         |
| `api_key`         | no       | Explicit bearer token supplied to the suite.                 |
| `model`           | no       | Laya checkpoint name; omitted so the server auto-selects.      |
| `allowed_ips`     | no       | Literal IPv4 or IPv6 addresses allowed for all requests.      |

The allowed choices are exactly the keys of `request.emotions`; each is sent
with its `description` when present, otherwise a human-readable dimension name.
