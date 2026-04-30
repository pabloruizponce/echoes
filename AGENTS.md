# echoes Repository Instructions

## Repository Purpose

This repo implements the echoes workflow. It combines deterministic helper scripts with a Codex skill for agent-reviewed daily digest runs.

## Development Rules

- Use `uv run python ...` for helper scripts.
- Use `uv run pytest` for the full test suite.
- Do not expose Scholar Inbox cookies, saved browser headers, Telegram tokens, chat IDs, or NotebookLM auth state in chat.
- Keep private runtime state under `<repo>/.echoes`. The repo-root `PROFILE.md` is only a public template.
- Preserve the split between mechanical scripts and Codex-authored decision artifacts.

## Digest Operation Boundary

- Use `$echoes` when the user asks to prepare, run, review, deliver, or schedule a digest.
- Do not run a one-command digest pipeline. Ranking selection, media prompts, and delivery copy must be reviewed or written by Codex as explicit artifacts before downstream scripts run.
- Normal development tasks such as refactoring, fixing tests, or editing docs do not require running the digest workflow unless the user explicitly asks for a digest run.
