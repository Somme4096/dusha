# Agent rules

- Load and follow the actual `i-have-adhd` and `stop-slop` skills for responses and documentation, fetching the [upstream stop-slop skill](https://github.com/hardikpandya/stop-slop/blob/main/SKILL.md) and its references if unavailable; report access failures instead of substituting a summary, and use one sentence when it conveys the instruction.
- Preserve behavior within the requested refactor scope, unify repeated logic, remove dead code and WHAT/HOW comments, and retain only comments explaining non-obvious external constraints or workaround reasons; preserve licenses, directives, and machine-consumed tool/API documentation.
- Write E2E tests through public HTTP, CLI, or harness entry points with real internal wiring and temporary persistence, cover success, failure, and relevant restart behavior, replace existing unit coverage before removing it, stub only external providers with disclosure, and report blockers instead of substituting unit tests.
- Verify code changes with `uv run --frozen pytest -q`, `uv run --frozen ruff check .`, and `git diff --check`, reuse valid evidence, and check instruction-only changes without running application tests.
- Commit all completed task changes using semantic commits (`type(scope): summary`, such as `refactor(api): unify error handling`), exclude unrelated edits and secrets, and push only on request.
