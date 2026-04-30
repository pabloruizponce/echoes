# Run Pipeline

Use this section when the user asks for the full digest.

## Default Codex Route

1. Run `uv run python scripts/prepare_env.py doctor --json` when setup health is unknown. Stop on required errors.
2. Fetch the digest with `uv run python scripts/fetch_digest.py --json`; add `--date YYYY-MM-DD` when requested, or use `--yesterday` for scheduled previous-day automation.
3. Resolve the active profile from `ECHOES_PROFILE` or `<repo>/.echoes/PROFILE.md`. If it is missing or still a template, complete [researcher-profile.md](researcher-profile.md).
4. Prepare the Codex review packet with `uv run python scripts/rerank_papers.py --digest <digest.json> --output-json <ranking.json> --output-markdown <ranking.md>`.
5. Read the active profile and ranking packet every run. Select papers by reasoning from the profile, API relevance scores, abstracts, and metadata; do not apply keyword fallbacks or score gates.
6. Persist the reviewed shortlist with `uv run python scripts/apply_ranking_review.py --ranking <ranking.json> --selected-title "<title>" ... --output-json <reviewed-ranking.json> --output-markdown <reviewed-ranking.md> --json`, or pass structured rationale with `--review-json-path`. Use `--allow-empty` only when Codex deliberately selects no papers.
7. Process papers from the reviewed ranking: `uv run python scripts/process_papers.py --ranking <reviewed-ranking.json> --digest <digest.json> --compression-profile printer --json`.
8. Choose the digest language from the user's run request: default to English (`en`), or use Spanish (`es`) only when the user or automation prompt explicitly asks for Spanish.
9. Read the active private researcher profile again, then write `media-plan.json` for every successfully processed paper in the selected language. Include one `audio_prompt` and one `video_prompt` per title, keeping prompts light and personalized with compact profile context.
10. Generate media with `uv run python scripts/generate_media.py --manifest <processed/manifest.json> --media-plan <media-plan.json> --language <es|en> --json`.
11. Codex writes `delivery-plan.json` in the selected language with the intro message and one per-paper message for every deliverable paper.
12. Send to Telegram with `uv run python scripts/send_digest.py --manifest <media-manifest.json> --delivery-plan <delivery-plan.json> --json`.

Do not run a one-command digest pipeline. The ranking selection, media prompts, and delivery copy are required Codex-reviewed artifacts.

## Blocker Policy

- Missing or invalid Scholar Inbox auth: stop and return to the prepare flow.
- Missing or template researcher profile: stop and create the active profile in private runtime.
- Missing or invalid NotebookLM auth: stop before processing papers.
- Ranking artifact without `agent_review`: stop and complete the Codex ranking review.
- Missing or invalid `media-plan.json`: stop before media generation.
- Missing or invalid `delivery-plan.json`: stop before Telegram delivery.
- Empty reviewed shortlist: write the reviewed artifact with `--allow-empty`, skip NotebookLM/media/send, and report that no paper cleared review.
- Partial media: continue to delivery only when partial delivery is explicitly enabled and at least one paper has complete audio and video.
- Missing Telegram config: stop at delivery.

## Output Summary

Report the effective digest date, reviewed shortlist count, media plan path, delivery plan path, delivered count, artifact paths, and any deferred paper/blocking stage. Avoid command-by-command recaps unless requested.
