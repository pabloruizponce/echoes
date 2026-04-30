# echoes

`echoes` is a Codex-guided workflow for turning a Scholar Inbox digest into a researcher-aware NotebookLM media digest and Telegram delivery.

The current implementation is explicit about its stack: Scholar Inbox for discovery, NotebookLM for media generation, Telegram for delivery, Codex for reviewed decisions, and `uv` for Python execution.

## Privacy Model

Tracked files are intended to be public. Private runtime state lives in the untracked project folder `.echoes/` by default:

- Scholar Inbox session data and saved browser request headers
- Telegram bot tokens and chat IDs
- the filled active `PROFILE.md`
- fetched digests, rankings, reviewed selections, PDFs, manifests, generated media, and logs
- Chrome setup profile and NotebookLM storage under `.echoes/notebooklm`

Override the runtime root with `ECHOES_CONFIG_DIR`. Override NotebookLM storage with `NOTEBOOKLM_HOME`. Never commit or share `.echoes/`.

## Install

Fresh machine:

```bash
curl -fsSL https://raw.githubusercontent.com/pabloruizponce/echoes/main/install.sh | bash
```

Existing checkout:

```bash
./install.sh
```

Common options:

```bash
./install.sh --dry-run
./install.sh --no-prompt --skip-auth --skip-profile --skip-cron
./install.sh --install-dir /srv/echoes --config-dir /srv/echoes/.echoes
./install.sh --cron-time 07:15
```

The installer detects macOS/Linux, checks dependencies, syncs the project, prompts for private credentials, discovers the Telegram chat ID from a direct message to the bot, hands browser-auth/profile synthesis to Codex when needed, runs `doctor`, and can install a managed cron block.

Manual prerequisites:

- `git`
- Python 3.10 or newer
- `uv`
- Codex CLI, logged in with `codex login`
- `ffmpeg` for Telegram voice-note conversion
- `ghostscript`, optional but recommended for PDF compression
- Node.js 20.19+ and Chrome/Chromium for the initial Chrome DevTools MCP auth capture

Manual setup:

```bash
git clone https://github.com/pabloruizponce/echoes.git
cd echoes
uv sync
uv run python scripts/prepare_env.py setup
codex login
```

Use the project skill at `.agents/skills/echoes/` with `$echoes`.

Check readiness:

```bash
uv run python scripts/prepare_env.py doctor --json
```

## Prepare

Each checkout or server needs its own private `.echoes/` state.

1. Create the active researcher profile at `.echoes/PROFILE.md`, either by importing an existing markdown profile or by collecting evidence with `uv run python scripts/researcher_profile.py collect-evidence` before Codex writes the active profile. The repo-root `PROFILE.md` is only a public template.
2. Save Telegram credentials through the installer, or run `uv run python scripts/prepare_env.py discover-telegram-chat-id --token-stdin`.
3. Run `uv run python scripts/prepare_env.py notebooklm-login`. By default this uses `.echoes/notebooklm`.
4. Capture Scholar Inbox auth with the `$echoes` prepare flow, then rerun `uv run python scripts/prepare_env.py doctor --json`.

For Scholar Inbox auth capture, Chrome DevTools MCP is used only during setup or re-authentication. Scheduled runs use the private saved headers.

## Run

The normal route is stage-by-stage and agent-reviewed:

1. `uv run python scripts/prepare_env.py doctor --json`
2. `uv run python scripts/fetch_digest.py --json`
3. `uv run python scripts/rerank_papers.py --digest <digest.json> --output-json <ranking.json> --output-markdown <ranking.md>`
4. Codex reviews the active profile, API relevance scores, abstracts, and paper metadata, then persists the selected papers and rationale with `scripts/apply_ranking_review.py`.
5. `uv run python scripts/process_papers.py --ranking <reviewed-ranking.json> --digest <digest.json> --compression-profile printer --json`
6. Codex writes `media-plan.json`.
7. `uv run python scripts/generate_media.py --manifest <processed/manifest.json> --media-plan <media-plan.json> --json`
8. Codex writes `delivery-plan.json`.
9. `uv run python scripts/send_digest.py --manifest <media-manifest.json> --delivery-plan <delivery-plan.json> --json`

There is no one-command digest pipeline. Ranking selection, media prompts, and delivery copy are Codex-authored artifacts on every run.

English is the default media and delivery language. Ask for Spanish, or pass `--language es` to `scripts/generate_media.py`, when you want a Spanish digest.

## Schedule

Use `scripts/run_daily_codex.sh` for cron-friendly runs. It reads `references/daily-codex-prompt.md`, runs `codex exec` from this repo, and writes logs under `.echoes/logs` unless `ECHOES_LOG_DIR` is set.

The runner intentionally uses unsandboxed Codex because the workflow needs private local credentials, NotebookLM browser state, the uv cache, and outbound access to Scholar Inbox, NotebookLM, arXiv, and Telegram. Run it under a dedicated user and set `ECHOES_ALLOW_UNSANDBOXED=1` only after reviewing that account's access.

The installer can add or update a managed cron block after `doctor --json` passes. It replaces only the block between `# BEGIN ECHOES DAILY` and `# END ECHOES DAILY`.

Optional runner environment:

- `CODEX_BIN`: path to the Codex executable, default `codex`
- `UV_BIN`: path to the `uv` executable, default `uv`
- `CODEX_MODEL`: Codex model override
- `CODEX_PROFILE`: Codex config profile override
- `ECHOES_LOG_DIR`: log directory override
- `ECHOES_CONFIG_DIR`: private runtime root override
- `NOTEBOOKLM_HOME`: NotebookLM storage root override
- `ECHOES_CRON_PATH`: installer cron PATH override

Example crontab:

```cron
SHELL=/bin/bash
PATH=/home/scholar/.nvm/versions/node/v22/bin:/home/scholar/.local/bin:/usr/local/bin:/usr/bin:/bin
ECHOES_ALLOW_UNSANDBOXED=1

15 7 * * * mkdir -p /srv/echoes/.echoes/logs && cd /srv/echoes && ECHOES_CONFIG_DIR=/srv/echoes/.echoes NOTEBOOKLM_HOME=/srv/echoes/.echoes/notebooklm CODEX_BIN=/home/scholar/.nvm/versions/node/v22/bin/codex UV_BIN=/home/scholar/.local/bin/uv /srv/echoes/scripts/run_daily_codex.sh >> /srv/echoes/.echoes/logs/cron-wrapper.log 2>&1
```

Before enabling cron, run the same command manually as the cron user:

```bash
ECHOES_ALLOW_UNSANDBOXED=1 scripts/run_daily_codex.sh
```

## Reset Or Move

Reset local private state:

```bash
./uninstall.sh
./uninstall.sh --dry-run --keep-checkout
./uninstall.sh --remove-checkout
```

`uninstall.sh` removes `.echoes/` or `ECHOES_CONFIG_DIR`, the configured NotebookLM home, the managed cron block, and the `chrome-devtools` MCP entry when present. It keeps global tools such as `codex`, `uv`, Node, Chrome, and your Codex login.

To move to another computer, clone the repo, run the install steps, then recreate or securely copy `.echoes/` through a private channel. Re-run NotebookLM login or Scholar Inbox auth capture if either auth state is machine-bound or expired.
