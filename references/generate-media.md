# Generate Media

Use this section to generate and download one NotebookLM audio overview and one NotebookLM video overview for every processed paper notebook.

## Inputs

- A processed run manifest produced by [scripts/process_papers.py](../scripts/process_papers.py)
- A Codex-authored `media-plan.json`
- Valid NotebookLM authentication from the prepare flow
- Notebook IDs, source IDs, and per-paper work directories already stored in the processed manifest
- The target media language: English (`en`) by default, or Spanish (`es`) when explicitly requested by the user or automation prompt

## Required Media Plan

Codex must write `media-plan.json` before this script runs. It must contain one entry for every successfully processed paper:

```json
{
  "papers": [
    {
      "title": "Paper title exactly as processed",
      "audio_prompt": "Codex-authored NotebookLM audio prompt",
      "video_prompt": "Codex-authored NotebookLM video prompt"
    }
  ]
}
```

The script rejects plans with unknown titles, missing successful titles, or missing prompts. Non-`ok` processed papers do not need media-plan entries.

## Prompt Guidance

Read the active private researcher profile before writing `media-plan.json`, choose the target language, then keep each prompt light and written in that language:

- `audio_prompt`: ask for a clear explanation of the paper, personalized with one compact bit of researcher-profile context.
- `video_prompt`: ask for a clear explanation of the paper, personalized with one compact bit of researcher-profile context, and ask NotebookLM to include as much PDF content as possible, especially images, tables, and diagrams.
- Do not overguide the generation. Avoid rigid outlines, scene-by-scene direction, tone micromanagement, or invented target structures.

## Default Route

1. Reuse the latest saved `<repo>/.echoes/processed/*/manifest.json` unless the user explicitly names a processed manifest.
2. Read the processed manifest and author `media-plan.json` for the successful papers.
3. Run `uv run python scripts/generate_media.py --manifest <processed/manifest.json> --media-plan <media-plan.json> --language <es|en> --json`; omit `--language` only when using the English default.
4. Let the script resume from any existing per-paper `media/result.json` and already-downloaded media files before submitting anything new.
5. Let the script submit only the missing artifact generations, then poll NotebookLM artifact status and download each finished file locally as soon as it is ready.
6. Reuse the per-paper `work_dir` from the processed manifest so downloaded files land next to the paper PDFs and NotebookLM metadata.

## Execution Contract

- Read successful paper entries from the processed manifest instead of rediscovering notebooks manually.
- Rehydrate saved media state from `<paper work_dir>/media/result.json` when it exists.
- If `audio.mp3` already exists and is non-empty, treat that artifact as completed only when its saved language metadata matches the requested media language. Legacy audio files without language metadata count as Spanish only. For `video.mp4`, require matching saved language plus whiteboard generation metadata; archive stale or unverifiable videos and regenerate them.
- If a previous run left `audio.mp3.tmp` or `video.mp4.tmp`, clean up the stale temp file before retrying the download.
- Skip papers whose processing status is not `ok`, and record them as media failures in the media manifest.
- Generate exactly two artifacts per eligible paper using the Codex-authored prompts from `media-plan.json`.
- Submit NotebookLM audio and video in the selected language. Submit videos with explainer format and the validated whiteboard-style raw API code. Keep the official `--style whiteboard` CLI route only as the built-in fallback if the raw API submission is rejected. Do not ask for whiteboard style in the prompt.
- Submit only artifacts that do not already have a saved artifact ID or downloaded output before entering the polling loop.
- Treat long video generation as normal. The script should keep polling NotebookLM artifact status until each video is genuinely complete and downloadable.
- Continue retrying transient failures and rate limits until all remaining papers finish or a paper hits an unrecoverable error such as missing notebook/source metadata or lost auth.
- If the processed manifest points to a NotebookLM notebook that no longer exists, stop with a clear failure instead of waiting forever or submitting duplicate media requests. Recover by rerunning `process papers` to create a fresh notebook first.
- Do not rely on a single long blocking wait per artifact. Poll NotebookLM artifact status by saved artifact ID so the run can notice completions across all in-flight media and stop shortly after the files are ready.
- Do not resubmit an artifact just because it is still pending. Keep tracking the same artifact ID unless NotebookLM reports a real generation failure.
- Use a run lock for the processed output directory. If another media run is already active for the same manifest, stop instead of starting duplicate executions.
- Download completed artifacts by artifact ID to:
  - `<paper work_dir>/media/audio.mp3`
  - `<paper work_dir>/media/video.mp4`

## NotebookLM Video Style Drift

`notebooklm-py` documents `generate video --style whiteboard`, but the library is unofficial and talks to undocumented NotebookLM RPCs whose numeric enums can drift. In April 2026, generated videos submitted through the documented whiteboard CLI path reported raw style code `4`, while the visually whiteboard-like NotebookLM artifacts in the same notebook reported raw style code `3`.

For that reason, `scripts/generate_media.py` submits video generation through the NotebookLM API adapter with raw format code `1` (`explainer`) and raw style code `3` (observed whiteboard). The official CLI `--style whiteboard` path remains only as a one-time fallback if NotebookLM rejects the raw style-code request, and such fallback results are recorded with a style warning. Do not “fix” this by adding whiteboard wording to the prompt; style enforcement belongs in the generation parameters and saved artifact metadata.

## Script Support

- Use `uv run python scripts/generate_media.py` for the normal workflow.
- Pass `--manifest <path>` to target a specific processed run.
- Pass `--media-plan <path>` every time. There is no default prompt fallback.
- Pass `--language es` for Spanish or `--language en` for English. The flag also accepts `spanish` and `english`; the default is English.
- Pass `--out-dir <path>` to override the default output root. The default should remain the processed run directory that owns the manifest.
- Pass `--limit <n>` only for debugging. Do not use it in the normal workflow.
- Let the script use its built-in polling loop on the normal path. Video runs can legitimately take 15 to 20 minutes, but the script should still stop soon after NotebookLM marks them complete and downloadable.
- Pass `--json` when downstream steps need machine-readable artifact metadata.

## Output Contract

- Store run-level output in `<processed run dir>/media-manifest.json`.
- Store per-paper output in `<paper work_dir>/media/result.json`.
- Keep the downloaded media next to the processed paper outputs in `<paper work_dir>/media/`.
- Include enough metadata for later steps:
  - processed manifest path
  - digest date, digest path, and ranking path when available
  - selected media language
  - notebook ID and source ID
  - audio/video prompts
  - artifact IDs
  - audio/video requested language
  - video format, style, source, prompt fingerprint, and raw NotebookLM format/style codes
  - generation attempt counts
  - retry and wait history
  - final download paths
  - final per-paper status

## Trace Discipline

- Keep the trace very lean: one short gate update, at most one short long-wait update, then the final result.
- On the happy path, do not reopen the processed manifest, inspect individual result files, or narrate helper commands after the main script has started.
- Trust `scripts/generate_media.py` as the normal path instead of manually replaying NotebookLM commands unless the script fails or times out in a suspicious way.
- If the script reports that a media run is already active for the same processed manifest, do not start a second one.
- During normal long video generation, do not emit repeated “still waiting” updates for each polling cycle. Let the run stay quiet unless something materially changes.
- Only do targeted checks during a media run when one of these happens:
  - the script exits unexpectedly
  - the run lock disappears unexpectedly
  - a concrete failure is reported
  - the wait has clearly exceeded the normal video window and there is evidence of a stall
- If the normal command succeeds, report only:
  - which processed manifest was used
  - how many papers finished successfully
  - where the media manifest was written
  - any papers that failed and need follow-up

## Response Style

Keep the user-facing summary concise. Report:

- how many papers got both media artifacts downloaded
- which processed manifest was used
- any failures that require manual follow-up

Do not include a long command-by-command recap unless the user explicitly asks for it.
