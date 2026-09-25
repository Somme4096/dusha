# Decision mods

Optional, externally hosted decision adapters. The gateway loads a single
Python file dynamically from `~/.config/companion-gateway/mods/`. A mod exports
one function:

```python
decide(request: DecisionRequest, options: Mapping[str, Any]) -> DecisionResult | None
```

Return a `DecisionResult` to answer, or `None` to abstain and let the gateway
fall back. Raise on failure; the gateway catches and records the error.

Mods may import `httpx`, the standard library, and the core types from
`companion_gateway.decision`. They must not import heavy model runtimes: the
model belongs in its own process, reached over the network.

## `laya.py`

Pure HTTP adapter for a running [Laya](https://github.com/NandhaKishorM/laya)
service (`POST /v1/systemone`). No `laya`/`torch` dependency.

Install by copying this file into the gateway's mods directory:

```sh
mkdir -p ~/.config/companion-gateway/mods
cp examples/mods/laya.py ~/.config/companion-gateway/mods/laya.py
```

Options passed to the mod:

| option            | required | meaning                                                        |
| ----------------- | -------- | -------------------------------------------------------------- |
| `base_url`        | yes      | Laya server root, e.g. `http://127.0.0.1:8000`. No default.    |
| `timeout_seconds` | no       | Finite positive timeout; default `10`.                         |
| `api_key_env`     | no       | Env var holding the bearer token. Missing env is an error.     |
| `model`           | no       | Laya checkpoint name; omitted so the server auto-selects.      |

The allowed choices are exactly the keys of `request.emotions`; each is sent
with its `description` when present, otherwise a human-readable dimension name.
