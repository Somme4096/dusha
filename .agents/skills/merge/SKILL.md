---
name: merge
description: >-
  Land requested dusha changes to main after checks pass.
  Use only when the user requests a merge.
  Do not use for review, preparation, passing checks, or skill installation.
disable-model-invocation: true
metadata:
  delta-action: land
---

# Merge

This skill lands requested dusha changes to main on origin.
Origin is git@github.com:Somme4096/dusha.git.
Target remains main unless the user names a different branch.

## Preflight

1. Record the current branch with git branch --show-current.
2. List changes with git status --short --branch.
3. Separate unrelated edits and secrets from the merge.
4. Commit only requested files with a semantic message.
5. Use type(scope): summary format per AGENTS.md.
6. Exclude secrets and unrelated files from the commit.

## Sync

1. Fetch origin before you rebase.
2. Rebase the change onto origin/main with git rebase origin/main.
3. Do not use interactive rebase.
4. Do not open an editor.
5. Use git commit -m for messages.

## Check

1. Run uv sync --extra test per .github/workflows/publish.yml.
2. Run uv run ruff check . per .github/workflows/publish.yml.
3. Run uv run pytest per .github/workflows/publish.yml and pyproject.toml.
4. Run all checks on the final rebased commits.
5. Passing earlier commits does not qualify.
6. Pending, failing, missing, or unverifiable checks block the merge.
7. Do not assume that started checks will pass.

## Project rules

1. Add only E2E tests per AGENTS.md.
2. Keep code comments to one line per AGENTS.md.
3. Link public API docs to https://github.com/Somme4096/dusha per AGENTS.md.
4. Use periods and commas in prose per AGENTS.md.
5. Do not add SHA-256 checks per AGENTS.md.

## Questions and conflicts

The merge request already authorizes the merge.
Do not ask for merge permission again.
Ask only when scope or conflict blocks progress.
Keep questions minimal and specific.

1. On conflict, list the conflicted hunks.
2. Wait for user guidance on each hunk.
3. Without guidance, apply only new hunks diff-by-diff.
4. Never overwrite whole files.
5. Preserve unrelated work in all resolutions.

## Land

1. Switch to main when the change sits on another branch.
2. Fast-forward main to the commits with passing checks.
3. Push main with git push origin main.
4. Do not force-push main.
5. If the push rejects, fetch origin again.
6. Rebase onto origin/main again.
7. Retry the push once.

## Final state

1. Check that main matches origin/main with git status.
2. Check landed commits with git log.
3. Prepared commits, pushed branches, or started checks do not complete the merge.
4. Only a push with passing checks completes the merge.

## Blockers

Stop on failing checks, secrets, unsafe changes, or ambiguous scope.
Report the blocker and the unmerged state.
Name the exact blocker.
