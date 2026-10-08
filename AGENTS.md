# AGENTS.md

## Project

- `swarmsail` runs multiple Headscale servers.
- DNS records for Swarmsail must point directly to the hosting server; do not use record proxying.

## Working conventions

- Read the repository status and relevant existing documentation before making changes.
- Preserve unrelated user changes. Do not overwrite or discard work that is outside the current task.
- Keep changes small, reviewable, and scoped to the task.
- Do not place credentials, tokens, private keys, or other secrets in the repository, runbooks, logs, or chat.
- Prefer reversible changes and verify service behavior after operational changes.

## Runbooks

- As work progresses, create or update a focused Markdown runbook in `runbooks/` for each operational task or repeatable discovery.
- Use lowercase, hyphen-separated filenames, such as `runbooks/configure-headscale.md`.
- Include the purpose, prerequisites, exact commands or configuration steps, expected results, verification, rollback or cleanup, and any unresolved assumptions.
- Record only sanitized values and placeholders for environment-specific addresses, credentials, and tokens.
- Keep runbooks current when implementation details change; do not create duplicate notes for the same procedure.

## Git

- Stage only files created or modified for the current task.
- Inspect the staged file list and staged diff before committing.
- Use a concise, descriptive commit message.
