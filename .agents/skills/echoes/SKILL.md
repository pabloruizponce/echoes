---
name: echoes
description: Prepare, validate, run, review, schedule, and deliver a Scholar Inbox to NotebookLM research digest workflow. Use when Codex needs to bootstrap Scholar Inbox and NotebookLM auth, create or use a researcher profile, fetch a digest, rerank papers for a researcher, apply agent-reviewed paper selection, process PDFs into NotebookLM notebooks, create Codex-authored English or Spanish media and delivery plans, send the digest to Telegram, run setup health checks, or configure daily Codex automations.
---

# echoes

Use this skill as a router. Load only the reference for the current stage.

## Core Rules

- Treat requests like `the digest`, `today's digest`, or `digest of the day` as full pipeline requests.
- Treat scheduled automation as a previous-day Europe/Madrid digest unless the user gives an explicit date.
- Resolve the active researcher profile from `ECHOES_PROFILE`, otherwise `<repo>/.echoes/PROFILE.md`. The tracked repo `PROFILE.md` is only a template.
- Do not expose Scholar Inbox cookies, saved browser headers, Telegram tokens, chat IDs, or NotebookLM auth state in chat.
- Use `uv run python ...` for helper scripts. Do not use bare `python`.
- Stop at the first real blocker. Do not silently downgrade to partial delivery unless the automation route or user explicitly allows it.
- Never use a one-command deterministic digest pipeline. Codex must review ranking selection, write media prompts, and write delivery copy every run.
- Use normal project cron automations for daily scheduled digest runs. This repository includes `.codex/config.toml` so trusted-project automations can use the normal uv cache, local private runtime state, and outbound network connections.
- Keep user-facing trace concise and outcome-oriented.

## Prepare And Validate

- First tell the user: `Open chrome://inspect/#remote-debugging and enable remote debugging.`
- Second tell the user: `Open https://www.scholar-inbox.com/digest in that same Chrome session and log in.`
- Ask the user to reply only after remote debugging is enabled and Scholar Inbox is logged in from that same Chrome session.
- Use Chrome MCP attached to the real Chrome session to capture authenticated Scholar Inbox request headers. Do not use local cookie-store inspection in the normal path.
- Run `uv run python scripts/prepare_env.py doctor --json` after setup or when debugging readiness.
- Read [prepare.md](../../../references/prepare.md) for the full setup flow.

## Run Pipeline

- For an explicit date, add `--date YYYY-MM-DD` to the fetch step. For automation, fetch yesterday's Europe/Madrid digest with `uv run python scripts/fetch_digest.py --yesterday --json`; stop if the fetch reports a requested-date mismatch, otherwise use that command's returned `output_path` for reranking and every downstream stage.
- The Codex route must run stage by stage: fetch, prepare the ranking review packet, inspect the active profile plus API relevance scores and abstracts, apply Codex-reviewed selected titles with rationale, process the reviewed ranking, write a media plan, generate media with that plan, write a delivery plan, then send with that plan.
- If a scheduled run reports sandbox/network-policy errors such as `Operation not permitted`, `nodename nor servname`, or uv cache permission failures, first verify that the project automation is using the trusted project config in `.codex/config.toml`.
- Read [run-pipeline.md](../../../references/run-pipeline.md) before running a full digest.
- Read [automation.md](../../../references/automation.md) before creating or updating scheduled automations.

## Stage References

- Fetch digest: [fetch-digest.md](../../../references/fetch-digest.md)
- Researcher profile: [researcher-profile.md](../../../references/researcher-profile.md)
- Ranking and agent review: [rank-papers.md](../../../references/rank-papers.md)
- Process papers: [process-papers.md](../../../references/process-papers.md)
- Generate media: [generate-media.md](../../../references/generate-media.md)
- Compose/send message: [compose-message.md](../../../references/compose-message.md)

## Script Entry Points

- `scripts/prepare_env.py`: setup, credential saving, smoke tests, `doctor`.
- `scripts/fetch_digest.py`: fetch and persist Scholar Inbox JSON.
- `scripts/researcher_profile.py`: initialize the active profile, inspect public links, or collect private profile evidence from webpages/descriptions/confirmed paper PDFs.
- `scripts/rerank_papers.py`: prepare evidence-only ranking review packets; Codex makes the selection.
- `scripts/apply_ranking_review.py`: validate and persist Codex-reviewed shortlist decisions and per-paper rationale.
- `scripts/process_papers.py`: download PDFs, create NotebookLM notebooks, upload sources. Requires reviewed ranking metadata.
- `scripts/generate_media.py`: generate/download English-by-default or explicitly Spanish audio and video explainers. Requires a Codex-authored `media-plan.json`.
- `scripts/send_digest.py`: deliver completed media to Telegram. Requires a Codex-authored `delivery-plan.json`.
