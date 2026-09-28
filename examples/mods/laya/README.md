# Laya decision plugin

This plugin calls a running [Laya](https://github.com/NandhaKishorM/laya) HTTP
service. The public plugin API is documented in the project [README](https://github.com/Somme4096/sophia).

The gateway runs it in its dedicated `uv` environment as a child process. The
plugin has inherited OS permissions and is not a container or OS sandbox.

The runner passes `decide(request, options)`. `request` has `message`,
`emotions`, `state`, and `instruction`. The function returns `{"emotion":
name}` or `None`.

Options include `base_url` (required), `timeout_seconds`, explicit `api_key`,
`model`, and optional `allowed_ips`. When `allowed_ips` is supplied, every
request and redirect must use a literal address in that list. The adapter uses
only credentials explicitly supplied in options. OS permissions remain
inherited by design.
