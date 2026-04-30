# Installer

Use this reference when maintaining `install.sh` or debugging a fresh-machine setup.

## Flow

`install.sh` is an interactive macOS/Linux installer. It is intentionally a hybrid installer:

1. Detect OS, package manager, and whether it is running inside an existing echoes checkout.
2. Clone the repo only when needed; existing checkouts are reused without auto-pulling or discarding local edits.
3. Install or check system tools: `git`, `curl`, Python 3.10+, `uv`, `ffmpeg`, optional `ghostscript`, Node/npm 20.19+, and Chrome/Chromium where practical.
4. Install/check Codex CLI, then offer `codex login`.
5. Run `uv sync` and `uv run python scripts/prepare_env.py setup`.
6. Configure Chrome DevTools MCP unless skipped.
7. Prompt for the Telegram bot token, generate a random code, discover the matching direct-chat ID through `prepare_env.py`, and save the result without echoing secrets.
8. Run NotebookLM login through `prepare_env.py`.
9. Launch Chrome/Chromium with remote debugging, pause for Scholar Inbox login, then invoke Codex to capture and save validated Scholar Inbox headers through MCP.
10. Either import an existing markdown researcher profile or collect profile evidence and invoke Codex to synthesize the active private profile.
11. Run `uv run python scripts/prepare_env.py doctor --json`.
12. Offer cron setup only after required checks pass.

`uninstall.sh` is the matching reset script:

1. Remove the echoes private runtime dir.
2. Remove the configured NotebookLM home, or default `<runtime>/notebooklm`.
3. Remove the managed echoes cron block when present.
4. Remove the `chrome-devtools` MCP entry when present.
5. Optionally remove the checkout after the script exits.

## Flags

- `--install-dir PATH`: clone/use checkout path when not running from the repo.
- `--repo-url URL`, `--branch NAME`: source checkout controls.
- `--config-dir PATH`: private echoes runtime state.
- `--notebooklm-home PATH`: NotebookLM storage root used during setup and cron.
- `--cron-time HH:MM`: non-interactive daily cron time.
- `--no-prompt`: skip interactive credential/profile/cron questions.
- `--dry-run`: print actions without applying changes.
- `--skip-system`, `--skip-codex-install`, `--skip-mcp`, `--skip-auth`, `--skip-profile`, `--skip-cron`: disable specific stages.

`uninstall.sh` supports:

- `--config-dir PATH`: echoes private runtime directory to remove.
- `--notebooklm-home PATH`: NotebookLM home to remove.
- `--remove-checkout`, `--keep-checkout`: explicit checkout cleanup behavior.
- `--no-prompt`, `--dry-run`, `--verbose`.

Environment variables mirror the main path flags: `ECHOES_INSTALL_DIR`, `ECHOES_REPO_URL`, `ECHOES_BRANCH`, `ECHOES_CONFIG_DIR`, `NOTEBOOKLM_HOME`, `ECHOES_CRON_TIME`, `ECHOES_CRON_PATH`, `ECHOES_NO_PROMPT`, and `ECHOES_VERBOSE`.

## Telegram Discovery

`scripts/prepare_env.py discover-telegram-chat-id` is the mechanical helper behind the installer flow.

- It accepts `--token` or `--token-stdin`, or reuses a saved `TELEGRAM_BOT_TOKEN`.
- It generates a random code unless `--code` is provided.
- It snapshots the current `getUpdates` stream, then polls only newer updates.
- It only accepts exact-text matches in `message.text` from `chat.type == "private"`.
- On success it saves `TELEGRAM_CHAT_ID` and preserves the rest of `credentials.env`.
- It must not print the bot token or resolved chat ID in normal output.

If discovery fails, the installer offers retry first and then manual chat-ID entry.

## Cron

The installer renders a marked block:

```cron
# BEGIN ECHOES DAILY
# Managed by echoes install.sh. Re-run install.sh to update safely.
15 7 * * * mkdir -p '<config>/logs' && cd '<repo>' && ECHOES_ALLOW_UNSANDBOXED=1 ECHOES_CONFIG_DIR='<config>' NOTEBOOKLM_HOME='<config>/notebooklm' PATH='<path>' CODEX_BIN='<codex-bin>' UV_BIN='<uv-bin>' '<repo>/scripts/run_daily_codex.sh' >> '<config>/logs/cron-wrapper.log' 2>&1
# END ECHOES DAILY
```

`<path>` is the configured cron PATH with the detected `codex` and `uv` directories prepended when needed. `cron-wrapper.log` captures shell-level failures that happen before the runner creates its timestamped log files.

Updating cron removes any previous block with the same markers before appending the new one. It does not modify unrelated crontab entries.

The uninstaller removes only the marked block and leaves unrelated crontab entries alone.

## Checkout Removal

`uninstall.sh` keeps the checkout by default in non-interactive mode. In interactive runs it asks whether to remove the checkout. When removal is requested for the currently running checkout, it schedules the deletion after the script exits so self-removal works reliably.

## Security

- Never print Scholar Inbox cookies, saved request headers, Telegram tokens, chat IDs, or NotebookLM auth state.
- Keep generated credentials, browser state, profile evidence, digests, rankings, media, and logs outside git.
- Codex handoff and the daily runner use full local access because they need MCP, private runtime state, network, and NotebookLM browser storage. Prefer a dedicated server user.
- `--dry-run` should not create files, install packages, save credentials, invoke Codex, or update cron.
- `uninstall.sh --dry-run` should not delete files, remove cron entries, modify MCP config, or remove the checkout.

## Troubleshooting

- If `codex` is missing, install the Codex CLI with Homebrew on macOS or `npm install -g @openai/codex`, then run `codex login`.
- If cron starts but no `daily-codex-*.log` file appears, check `<config>/logs/cron-wrapper.log` first. That usually means the shell could not find `codex`, `uv`, or another startup dependency before the runner initialized its own logs.
- If Chrome DevTools MCP cannot attach, confirm Chrome is running with `--remote-debugging-port=9222` and that `codex mcp get chrome-devtools` succeeds.
- If Node is too old for Chrome DevTools MCP, install Node.js 20.19+ or newer and rerun `./install.sh --skip-system` after the manual fix.
- If `doctor --json` fails, fix required checks first. Cron setup is intentionally skipped until doctor passes.
- If Telegram chat discovery keeps timing out, make sure the bot token is correct, send the code in a direct chat to the bot, and check for a conflicting Telegram webhook.
