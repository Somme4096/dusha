# Agent rules

- Always use period and comma instead of semicolon. This applies to every user-facing and agent-facing instructions, including comments.
- Do not write comments or descriptions inside the code that are more than one(1) line unless absolutely necessary and user agreed. Use link to actual README.md on github (https://github.com/Somme4096/dusha) for public-facing API descriptions, explain nothing else.
- Write E2E tests only, no unit tests. 
- Let `hk` run `ruff`, `pytest`, and commit message checks automatically on `git commit` instead of running tests manually after implementation. You may use `hk check` when you need to test before a commit.
- Do NOT EVER use SHA-256 check for ANYTHING unless user explicitly stated so.

## CLI writing convention

- Format status, warning, and error output in two lines, a bold colored headline (`Success!`, `Warning!`, or `Error!`) followed by sentence-case, period-terminated body lines with manual line breaks.
- Emit one `Success!` header per completed operation or setup phase, placing each outcome or follow-up hint on its own line.
- Write command and group `--help` descriptions as short imperative phrases without a trailing period, and keep CLI `help=` separate from companion MCP tool prompts.
- Document every CLI option with sentence-case `help=` text ending in a period.
- Use consistent CLI terminology (`companion`, `proxy`, `server`, `provider`, `profile`), avoid aliases like `client proxy` or `agent`, and use regular English verbs such as `Specify a companion` or `Add a provider` instead of `Name a ...`.
