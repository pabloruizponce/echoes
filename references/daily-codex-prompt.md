Use `$echoes` to run the daily echoes route for yesterday's Europe/Madrid digest from this local echoes workspace.

First run `uv run python scripts/prepare_env.py doctor --json` and stop on required setup or authentication errors.

Fetch the digest with `uv run python scripts/fetch_digest.py --yesterday --json`; stop if the fetch reports a requested-date mismatch, and otherwise use the returned `output_path` as the digest path for reranking and every downstream stage. Do not switch to any latest saved digest artifact.

Create a ranking review packet, inspect the active private researcher profile plus API relevance scores and abstracts every run, and apply the Codex-reviewed shortlist and rationale with `scripts/apply_ranking_review.py`.

Process the reviewed papers, choose English media and delivery unless this automation prompt explicitly asks for Spanish, write `media-plan.json` in the chosen language, generate NotebookLM audio and video with `--media-plan` and `--language <es|en>`, write `delivery-plan.json` in the chosen language, and deliver completed papers to Telegram with `--delivery-plan`.

Do not reveal Scholar Inbox cookies, saved browser headers, Telegram tokens, chat IDs, or NotebookLM auth state.

Do not run a one-command deterministic digest pipeline. Codex must review ranking selection, write media prompts, and write delivery copy during this run.

Open an inbox item with the effective digest date, reviewed shortlist, media plan path, delivery plan path, delivered count, artifact paths, deferred papers, and blocking stage or error if the run fails.
