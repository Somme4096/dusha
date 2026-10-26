# TODO

- Side-call rules for the proxy. Harnesses send title, summary, and subagent requests through the same base URL, and those land in the archive as conversations. Add per-profile JSON rules in `proxy.upstreams` that skip or tag them, such as by system prompt, missing tools, or prompt size.
- Real-harness tests. Record the requests real harnesses send through the proxy once, keep them as fixtures, and replay them in E2E tests against a stub provider. Adding a harness then means adding a fixture, not code.
