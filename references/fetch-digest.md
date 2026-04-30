# Fetch Digest

Use this section to fetch the current or requested Scholar Inbox digest from the saved session and persist a structured JSON snapshot for downstream ranking.

## Default Conversation

Use this default direction when the user asks to fetch the digest:

1. `I’m fetching the digest from the saved Scholar Inbox session now.`
2. After the command finishes, reply with a short outcome summary only.

Do not start by reopening Chrome or asking the user to log in again if the saved session is already available.
Do not preface the run with exploratory narration when the default fetch command is sufficient.

## Sequence

1. Run `uv run python scripts/fetch_digest.py` to fetch the latest/current digest.
2. If the user asked for a specific day, run `uv run python scripts/fetch_digest.py --date YYYY-MM-DD`.
3. For scheduled daily automation, run `uv run python scripts/fetch_digest.py --yesterday --json` to fetch the previous Europe/Madrid calendar day even when today's digest is already available.
4. Let the script read the saved session and optional saved browser request headers from `<repo>/.echoes/credentials.env` unless `ECHOES_CONFIG_DIR` overrides it.
5. Let the script call the Scholar Inbox JSON API at `https://api.scholar-inbox.com/api` with browser-like headers. `ECHOES_USER_AGENT` may override the default Chrome-style user agent.
6. Extract the digest papers from `digest_df`.
7. Normalize each paper so `title`, `url`, and `description` are always present as the canonical downstream fields whenever the source payload provides enough data.
8. Use `abstract` as the primary source for `description`. If `abstract` is missing, fall back to the first available entry in `summaries`.
9. Normalize the Scholar Inbox API `ranking_score` as `api_relevance_score` and `relevance_score`, while preserving `scholar_inbox_score` as a compatibility alias.
10. Preserve the visible paper metadata from the API payload in the saved JSON instead of discarding it.
11. Persist the snapshot to `<repo>/.echoes/digests/YYYY-MM-DD.json` unless the user passes `--output`.
12. Treat success as: requested digest resolved, papers extracted, descriptions captured when available, and JSON snapshot written.

## Response Style

- Prefer one concise progress update before running the fetch command.
- Skip commentary about routine repo exploration, reading local files, or checking obvious directories unless the fetch fails and that detail explains why.
- On success, summarize only:
  - effective digest date
  - output path
  - paper count
  - whether warnings were present
- Do not include fetch timestamps, shell traces, or “refreshed from cache vs rerun” narration unless the user asked for execution details.
- If the digest file already exists, still rerun the fetch command unless the user explicitly asked to reuse the cached artifact.

## Output Contract

- Write one JSON object per digest fetch.
- Include top-level fields for:
  - fetch timestamp
  - requested digest date
  - source current digest date returned by Scholar Inbox
  - effective digest date
  - source URL
  - API URL
  - raw paper count
  - empty-digest flag
  - parse warnings
  - missing-field notes
  - papers
- Keep all visible API metadata on each paper record.
- Add normalized `title`, `url`, `abstract`, `description`, `description_source`, `digest_position`, `api_relevance_score`, `relevance_score`, and `scholar_inbox_score` fields to each paper record.

## Failure Handling

- If `SCHOLAR_INBOX_SESSION` is missing and no saved `Cookie` header exists in `SCHOLAR_INBOX_REQUEST_HEADERS_JSON`, stop and return the user to the `prepare` flow.
- If local DNS resolution fails for Scholar Inbox, let the built-in DNS-over-HTTPS fallback resolve the host and retry the same request. Treat a failure after that retry as a real network-path blocker.
- If the API returns `success=false`, treat that as an auth or request failure, not as an empty digest.
- If the API returns no papers and `empty_digest` is false, save a warning in the snapshot because the payload may have drifted.
- If a requested digest date is provided and Scholar Inbox returns a different `current_digest_date`, fail the fetch instead of saving a mislabeled snapshot.
- If the HTTP request fails, do not fall back to live Chrome automatically unless the saved-session path cannot be repaired.
- Use live browser inspection only as a debugging fallback when the JSON API shape or auth behavior changes.

## Do Not Do This

- Do not require a live Chrome session for the normal fetch flow.
- Do not scrape the SPA shell HTML for papers when the JSON API is available.
- Do not discard extra paper metadata that may help Codex review or later processing.
- Do not silently treat `success=false` as a valid empty digest.
