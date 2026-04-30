# Process Papers

Use this section to turn the important-now shortlist into per-paper NotebookLM notebooks.

## Inputs

- A reviewed ranking JSON artifact produced by [scripts/apply_ranking_review.py](../scripts/apply_ranking_review.py)
- The referenced digest JSON artifact, or an explicit digest path override
- Valid NotebookLM authentication from the prepare flow

## Default Fast Path

1. Reuse the latest reviewed ranking JSON when one exists; otherwise stop and complete the ranking review step.
2. Run `uv run python scripts/process_papers.py --ranking <reviewed-ranking.json> --compression-profile printer --json` directly unless the user explicitly asked for different inputs or output behavior.
3. Let the script resolve `shortlist_titles`, notebook creation, upload, and readiness checks.
4. Persist per-paper results and a run manifest for downstream media generation.

## Trace Discipline

- Keep the trace lean: one short gate update, one short processing update, then the final result.
- On the happy path, do not explore the repo, read the script again, list directories, run `--help`, inspect manifests, or mention shell commands beyond the one processing command being executed.
- Do not narrate routine helper commands such as `find`, `ps`, ad hoc JSON reads, or repeated artifact inspection when the normal script path is already working.
- Do not re-read partial artifacts just to reassure yourself during the happy path. Trust `scripts/process_papers.py` unless it fails or times out.
- On success, report only:
  - which ranking artifact was used
  - how many papers were processed
  - the created notebook titles and IDs
  - the manifest path
- Prefer this three-message shape:
  - gate: `Using the latest saved ranking artifact and starting the paper-processing run now.`
  - progress: `The processing run is in flight. I’m waiting for NotebookLM source readiness checks to finish.`
  - final: concise result only
- Inspect per-paper files or rerun debugging commands only when the script reports a failure, timeout, or obviously inconsistent result.
- If the normal command succeeds, do not add a post-success message like `I’m giving it a longer window` or `I’m checking the manifest now`.

## Execution Contract

- Create one notebook per shortlisted paper.
- Upload one PDF source per notebook.
- Wait for the source to finish processing before marking that paper as complete.
- Continue processing the remaining papers if one paper fails.
- If compression fails or produces a larger file, upload the original PDF instead.
- Reject ranking artifacts that do not contain `agent_review` metadata.

## Script Support

- Use `uv run python scripts/process_papers.py` for the default flow.
- Pass `--ranking <path>` to target a specific ranking artifact.
- Pass `--digest <path>` only when the digest path cannot be derived from the ranking.
- Pass `--out-dir <path>` to override the default `<repo>/.echoes/processed/<digest-date>/` output location.
- Pass `--limit <n>` only for debugging. Do not use it in the normal workflow.
- Let the default compression profile stand unless the user asks to trade quality for smaller files. The default should favor readable figures over maximum size reduction.
- Pass `--compression-profile printer` for the normal workflow. Use `ebook` or `screen` only when the user explicitly wants stronger compression. Use `prepress` when quality matters more than size reduction.
- Pass `--json` when downstream steps need machine-readable notebook and source IDs.

## Output Contract

- Store outputs under `<repo>/.echoes/processed/<digest-date>/`.
- For each paper, keep:
  - `original.pdf`
  - `compressed.pdf` when compression succeeds
  - `result.json`
- For the whole run, keep `manifest.json`.
- Include enough metadata for later steps:
  - ranking path
  - digest path and digest date
  - notebook title and notebook ID
  - source ID
  - original, compressed, and uploaded PDF paths
  - the compression profile that was used
  - compression statistics
  - final per-paper status

## Response Style

Keep the user-facing summary concise. Report:

- how many shortlisted papers were processed
- which notebooks were created
- any per-paper failures that need follow-up

Do not include a long command-by-command recap unless the user explicitly asks for it.
Do not include reasoning about waits, internal timing windows, or whether a longer poll is needed unless the run actually stalls or fails.
