# Agent rules

- Load and follow the `stop-slop` skill before writing every user-facing and agent-facing instructions, comments, responses and documentation. Always use period and comma instead of semicolon. This also applies to every user-facing and agent-facing instructions, including comments.
- Do not write comments or descriptions inside the code that are more than one(1) line and unless absolutely necessary and user agreed. Use link to actual README.md on github (https://github.com/Somme4096/dusha) for public-facing API descriptions, explain nothing else.
- Write E2E tests only, no unit tests. Drive the code through public HTTP, CLI, or harness adapters. Cover success, failure, and relevant restart behavior, stub only external providers with disclosure, and report blockers instead of substituting unit tests.
- Commit all completed task changes using semantic commits (`type(scope): summary`, such as `refactor(api): unify error handling`), exclude unrelated edits and secrets.
- Do NOT EVER use SHA-256 check for ANYTHING unless user explicitly stated so.
