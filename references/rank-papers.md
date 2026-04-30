# Rank And Review Papers

Use this section after a digest JSON snapshot exists.

## Inputs

- Fetched digest JSON from `scripts/fetch_digest.py`.
- Active profile from `ECHOES_PROFILE` or `<repo>/.echoes/PROFILE.md`.

## Evidence Packet

1. Reuse the active profile as-is when it contains real information.
2. Run `uv run python scripts/rerank_papers.py --digest <digest.json> --output-json <ranking.json> --output-markdown <ranking.md>`.
3. Treat the output as a Codex review packet, not an automatic ranking decision.
4. Do not use keyword matching, deterministic fallbacks, or score gates to select papers.

The packet should expose each paper's API relevance score, abstract, URL, digest position, and original metadata. Missing scores or abstracts are review notes, not automatic exclusions.

## Required Codex Review

Review every run before NotebookLM processing:

- Read the active researcher profile and the evidence packet.
- Select papers by reasoning from profile fit, API relevance score, abstract content, and metadata.
- Allow low-score or missing-score papers when the profile and abstract make them genuinely important.
- Allow an empty shortlist when the digest is weak.
- Write concise per-paper rationale for selected papers.

Persist the decision with either explicit titles:

```bash
uv run python scripts/apply_ranking_review.py \
  --ranking <ranking.json> \
  --selected-title "<paper title>" \
  --notes "<short review note>" \
  --output-json <reviewed-ranking.json> \
  --output-markdown <reviewed-ranking.md> \
  --json
```

or structured Codex review JSON:

```bash
uv run python scripts/apply_ranking_review.py \
  --ranking <ranking.json> \
  --review-json-path <codex-review.json> \
  --output-json <reviewed-ranking.json> \
  --output-markdown <reviewed-ranking.md> \
  --json
```

Use `--allow-empty` only for an intentionally empty reviewed shortlist.

## Outputs

- Ranking review packet JSON/Markdown.
- Reviewed ranking JSON/Markdown with `agent_review` metadata and Codex rationale.
- Downstream stages must use the reviewed ranking artifact.

## Final Response

Report the digest path/date, active profile path, reviewed shortlist count, and any uncertainty that affected selection.
