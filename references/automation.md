# Automation

Use this section when the user wants echoes to run on a schedule.

## Readiness Gate

Before creating or updating a scheduled automation:

1. Run `uv run python scripts/prepare_env.py doctor --json`.
2. Stop on required `error` checks.
3. Continue with `warn` checks only when they do not affect the requested automation.

## Default Schedule Behavior

- Daily automation should fetch the previous Europe/Madrid calendar day with `uv run python scripts/fetch_digest.py --yesterday --json`.
- Use the fetch command's returned `output_path` as the digest path for reranking and every downstream stage; do not switch to a latest saved digest artifact.
- Stop if the fetch reports a requested-date mismatch instead of continuing with a mislabeled current digest.
- The automation should continue in Codex and make the ranking review decision every run.
- Use a standalone project cron automation for the daily digest.
- Keep the project trusted so Codex loads `.codex/config.toml`; that config grants the automation full local command access, including the normal uv cache, private echoes runtime state, NotebookLM state, and outbound network connections.
- The automation must use the stage-by-stage route. It must not call a one-command digest pipeline.
- Codex must write the reviewed ranking, `media-plan.json`, and `delivery-plan.json` before the downstream scripts run.
- Default to English media and delivery. If the automation prompt explicitly asks for Spanish, write `media-plan.json` and `delivery-plan.json` in Spanish and pass `--language es` to `scripts/generate_media.py`.
- Allow partial media delivery only for scheduled automation and only when at least one paper has complete audio and video.
- If doctor fails with sandbox/network-policy messages such as `Operation not permitted`, `nodename nor servname`, or an unwritable uv cache, first verify that the automation is running from the trusted project and loading `.codex/config.toml`.

## Recommended Automation Prompt

Use [daily-codex-prompt.md](daily-codex-prompt.md) as the canonical reusable prompt for cron or any standalone Codex automation. Keep that file aligned with the schedule behavior above: run the doctor gate first, fetch yesterday's Europe/Madrid digest, stop on requested-date mismatch, continue stage by stage, require Codex-reviewed ranking selection, require Codex-written `media-plan.json` and `delivery-plan.json`, default to English unless Spanish is explicitly requested, pass the matching `--language <es|en>` to media generation, and never replace the reviewed route with a one-command deterministic pipeline.

## Output Expectations

The automation should report:

- effective digest date
- reviewed shortlist titles
- media plan path
- delivery plan path
- processed/media/delivery artifact paths
- delivered count
- deferred papers and reasons, if any
- blocking stage and error, if it fails
