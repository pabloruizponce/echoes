# Compose Message

Use this section to prepare and send the final digest delivery through a Telegram bot after media generation has finished.

## Inputs

- A run-level media manifest produced by [scripts/generate_media.py](../scripts/generate_media.py)
- The linked processed `manifest.json`
- The linked reviewed ranking artifact with Codex shortlist rationale
- The linked digest artifact with the effective digest date and paper metadata
- A Codex-authored `delivery-plan.json`
- The selected media language from `media-manifest.json`
- Telegram bot credentials:
  - `TELEGRAM_BOT_TOKEN`
  - `TELEGRAM_CHAT_ID`

Use `python-telegram-bot` for Telegram delivery. Do not switch to another Telegram client library for the normal workflow.

## Required Delivery Plan

Codex must write `delivery-plan.json` before `scripts/send_digest.py` runs:

```json
{
  "intro_message": "Codex-authored digest intro",
  "papers": [
    {
      "title": "Paper title exactly as shown in the media manifest",
      "message": "Codex-authored Telegram text for this paper"
    }
  ]
}
```

The script rejects plans with unknown titles, missing deliverable titles, missing intro text, or missing per-paper message text. Legacy PDF/audio/video caption fields are accepted for compatibility but ignored by the sender.

## Default Route

1. Reuse the latest saved `<repo>/.echoes/processed/*/media-manifest.json` unless the user explicitly names a specific run.
2. Read the linked processed, ranking, and digest artifacts from the manifest metadata instead of rebuilding state manually.
3. Preserve shortlist order from the processed manifest and ranking artifact. Do not reorder papers by filename, notebook ID, or media completion time.
4. Compose `delivery-plan.json` with one intro message and one complete paper entry per deliverable paper.
5. Send with `uv run python scripts/send_digest.py --manifest <media-manifest.json> --delivery-plan <delivery-plan.json> --json`.
6. Finish one paper completely before sending media for the next paper.

## Completed-Only Automation Delivery

- Use `uv run python scripts/send_digest.py --manifest <media-manifest.json> --delivery-plan <delivery-plan.json> --completed-only --json` when the run allowed partial media.
- In completed-only mode, deliver only papers whose media status is `ok` and whose `audio.mp3` and `video.mp4` files exist.
- Preserve the original manifest order for delivered papers.
- Skip incomplete papers and send one short final note in the selected media language listing the skipped/deferred papers with the saved media error or missing asset reason.
- Return `skipped_count` and `skipped_papers` in JSON so the automation can record exactly what was deferred.
- If no papers have complete media, do not use completed-only delivery as a workaround. Return to the media stage blocker instead.

## Telegram Setup Contract

- Treat `TELEGRAM_BOT_TOKEN` as the bot authentication token.
- Treat `TELEGRAM_CHAT_ID` as the destination chat ID.
- Reuse them from `<repo>/.echoes/credentials.env` unless `ECHOES_CONFIG_DIR` overrides the config location.
- If the user provides the bot token in chat, save it privately for future runs instead of treating it as a one-off value.
- If the chat ID is discovered from Telegram updates, save it privately for future runs instead of asking the user for it again next time.
- Do not paste raw secrets into chat unless the user explicitly asks for help wiring them in.

Prefer these private save paths:

- discover the chat ID from a direct message to the bot and save it privately: `uv run python scripts/prepare_env.py discover-telegram-chat-id --token-stdin`
- save both values: `uv run python scripts/prepare_env.py save-telegram-config --chat-id <id> --token-stdin`
- save only a newly discovered chat ID: `uv run python scripts/prepare_env.py save-telegram-chat-id --chat-id <id>`

If either value is missing, empty, or unverified, stop delivery and give the user these setup steps:

1. Open Telegram and start a chat with `@BotFather`.
2. Run `/newbot` and follow the prompts to create the bot.
3. Copy the bot token that BotFather returns and save it as `TELEGRAM_BOT_TOKEN`.
4. Start a direct chat or target group chat with the new bot.
5. Send at least one message to the bot so the chat becomes discoverable.
6. Run `uv run python scripts/prepare_env.py discover-telegram-chat-id --token-stdin`, send the generated code to the bot in a direct chat, and let the helper save the matching chat ID.
7. Save the bot token and chat ID privately in `<repo>/.echoes/credentials.env`.
8. Rerun the compose/send step after both values are available.

Keep the setup explanation concise and action-oriented.

## Artifact Resolution

- Use the media manifest as the entry point for the delivery step.
- Reuse saved Telegram credentials from `credentials.env` before asking the user to provide them again.
- Read the effective digest date from the digest artifact when available.
- Read the shortlist titles and Codex rationale from the reviewed ranking artifact.
- Read the paper title, canonical URL, per-paper work directory, and media file paths from the processed and media manifests.
- Prefer the canonical paper URL as the paper link in the user-facing message.
- Do not attach the local PDF separately by default; the canonical paper URL in the summary is the paper access point.

## Message Language

Codex writes Telegram text in the same language as the generated media. English is the default; when the media manifest was generated with `media_language: "es"`, write the intro and paper messages in Spanish.

## Message Contents

Use Telegram `HTML` formatting in `delivery-plan.json`. Prefer simple tags: `<b>`, `<i>`, `<u>`, `<s>`, `<code>`, and `<a href="...">...</a>`. Escape literal `<`, `>`, and `&` in paper titles or generated text unless they are part of intended Telegram HTML.
The send script automatically prefixes the intro and paper summaries with a suitable emoji when the delivery plan text does not already contain one. Audio and video captions are generated by the sender.

### Intro Message

Write one intro text message before any paper-specific content. Include:

- the current delivery date or the digest's effective date
- the digest or run being delivered
- how many papers were selected
- a short note that the shortlist reflects the researcher-aware ranking
- a short note that each paper will be delivered with a summary, voice note, and video preview

Keep the introduction brief and readable in a single Telegram message.

You may use a compact format like:

```text
📚 <b>Digest for <digest date></b>

Sharing <b><N> paper(s)</b> selected from today's digest, prioritized against the current researcher profile.

For each paper I’m sending:
1. 📄 a short summary with the paper link
2. 🎧 a playable voice note
3. 🎬 a video preview
```

Prefer light Telegram formatting with short bold labels and a few helpful emojis. Keep it readable, not decorative.
The example above is English because English is the default; use equivalent natural Spanish wording for explicit Spanish media runs.

### Per-Paper Text Message

For each paper, write one text block with:

- the paper title
- a short explanation of why it was selected now
- the canonical paper link when available

When Codex review rationale is available, summarize it directly from the reviewed ranking artifact. When it is sparse, use Codex judgment and the paper metadata to write a concise explanation; do not rely on script-generated fallback copy.

Prefer a structured format like:

```text
📄 <b><paper title></b>

🎯 <b>Why it matters now</b>
- <one short reason in the selected language>
- <optional second short reason in the selected language>

🔗 <b>Paper</b>
<a href="<canonical paper URL>">Open original paper</a>
```

Keep the text scannable. Do not send one dense paragraph.
Use `<u>`, `<s>`, and `<code>` only when they add clarity, for example `<code>benchmark</code>` labels or a concise correction with `<s>descartado</s>`.
Translate ranking rationale into the selected language before sending it.

When the ranking artifact contains English rule-style reasons and the selected language is Spanish, rewrite them into concise Spanish justifications focused on research fit. For example:

- `matches must include signals: Human motion generation`
  becomes
  `Encaja de forma directa con generación de movimiento humano, una de las señales prioritarias del perfil.`
- `matches must include signals: Human motion generation or prediction with explicit relevance to interactions, pose, or full-body digital humans`
  becomes
  `Conecta de forma clara con predicción o generación de movimiento humano, con relevancia para pose, interacción y humanos digitales de cuerpo completo.`

Collapse duplicated or near-duplicated reasons into one or two polished bullets in the selected language. Do not expose scoring jargon, section names, or matching-rule prefixes to the user.
For default English runs, still remove scoring jargon and rule prefixes, but keep the final justifications in natural English.

## Media Delivery Sequence

The send script uses `python-telegram-bot` primitives that map cleanly to this sequence:

1. `send_message` for the intro text
2. For each paper, `send_message` for the paper summary
3. convert that paper's MP3 to OGG/Opus and send it with `send_voice`
4. `send_video` for that paper's MP4 video file with `supports_streaming=True`

Use `send_voice` for generated audio. This makes Telegram render it like a voice note instead of a music track, so playback-speed controls and voice-message interactions are available in clients that support them. The MP3 generated by NotebookLM must be converted to OGG/Opus with `ffmpeg` before upload; if conversion fails, mark only that paper's audio as incomplete and continue with the video.

Use `send_video` for videos first so Telegram shows the inline player and thumbnail preview whenever possible. The sender reads the MP4 width, height, and duration with `ffprobe` and passes them to Telegram to preserve the display ratio. If inline video upload fails after retries for any reason, the sender falls back to `send_document` and records that fallback in the JSON result. If the hosted Bot API still cannot upload an over-limit MP4, create a Telegram-safe H.264/AAC copy under the hosted upload limit and send that copy, preferring inline video before document fallback for the copy as well.

Complete the summary, voice note, and video sends for one paper before moving to the next paper.
Do not batch all summaries first, all audios first, or all videos later.

## Media Labels And Captions

Do not write media captions in `delivery-plan.json`. The sender generates short captions automatically:

- audio: `🎧 <paper title>`
- video: `🎬 <paper title>`

The send script assigns sanitized filenames that preserve the media type:

- `<paper-slug>-voice-<digest-date>.ogg`
- `<paper-slug>-video-<digest-date>.mp4`

## Failure Handling

- If a paper lacks ranking reasons, write a reasoned Codex-authored explanation from available paper metadata before delivery.
- If the canonical paper link is missing, omit the link or say it is unavailable.
- If the audio file is missing or cannot be converted to a Telegram voice note, report that the audio is unavailable for that paper and still send the video if present.
- If the video file is missing, report that the video is unavailable for that paper and still send the audio if present.
- If all generated media assets are missing for a paper, send only the paper text message and mark the paper as incomplete.
- In completed-only automation mode, skip any paper with missing media instead of sending a partial paper block.
- Do not recompress or transcode MP4 files for Telegram delivery.
- Do not abort the whole digest because one paper is incomplete.
- Reserve hard failure for missing Telegram configuration, invalid `delivery-plan.json`, or a total inability to resolve the delivery artifacts.

## Trace Discipline

- Keep the compose/send trace lean: one short gate update, one short sending update, then the final result.
- On the happy path, do not narrate local secret-source scans, ad hoc JSON extraction, temporary helper scripts, or `getUpdates` retries step by step.
- Do not echo raw bot tokens, full shell commands, or long inline scripts in the user-facing summary.
- If the user supplies a bot token in chat or a chat ID is discovered during delivery setup, save it privately and mention only that it has been saved for future runs.
- If Telegram configuration is missing, stop quickly and give only the missing setup steps.
- If delivery is ready, prefer this three-message shape:
  - gate: `I found the latest digest delivery bundle and I’m resolving the Telegram destination now.`
  - progress: `The destination is ready. I’m sending the intro, summary, voice note, and video in order now.`
  - final: concise delivery outcome only
- If chat ID discovery requires `getUpdates`, mention only the resolved outcome, not each polling attempt.
- If a send fails, report the failing artifact and the next recovery action without replaying the whole command trace.
- Do not narrate the same resolution step twice. Once the destination or manifest is resolved, move directly to sending.
- In the final message, report the delivery outcome, the saved destination status, and any missing assets. Omit internal extraction details that were already implied by success.

## Response Style

Keep the user-facing delivery summary concise and outcome-focused. Report:

- which digest or media manifest was used
- how many papers were delivered successfully
- any papers with missing audio or video assets

Do not include a long command-by-command recap unless the user explicitly asks for execution details.
