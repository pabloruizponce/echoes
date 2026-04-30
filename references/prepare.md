# Prepare

Use this section to bootstrap the local environment, capture Scholar Inbox auth, log in to NotebookLM, and run smoke tests.

## Default Conversation

Use these exact prompts in this order when the user asks to prepare the environment:

1. `Open chrome://inspect/#remote-debugging and enable remote debugging.`
2. `Open https://www.scholar-inbox.com/digest in that same Chrome session and log in.`
3. `Reply when both are done.`

Do not start with local probing, cookie-store inspection, or manual cookie instructions. Wait for the user to confirm the Chrome step first.

## Sequence

1. Tell the user to enable Chrome remote debugging at `chrome://inspect/#remote-debugging`.
2. Tell the user to open `https://www.scholar-inbox.com/digest` in that same Chrome session and log in.
3. Wait for the user to reply that both are done.
4. Run `uv run python scripts/prepare_env.py setup`.
5. Use Chrome MCP `list_pages` to verify that MCP is attached to the user's real Chrome session.
6. If attachment fails, stop and instruct the user to:
   - confirm `http://127.0.0.1:9222/json/version` returns JSON
   - confirm Chrome MCP is configured with `--browser-url=http://127.0.0.1:9222` or `--autoConnect`
   - restart Codex if needed
7. Once attached, select the Scholar Inbox digest tab, reload it if needed, and inspect authenticated network requests. Use network request headers as the source of truth for the Scholar Inbox session.
8. Prefer the atomic private path: validate and save the extracted request headers in one step without passing raw credentials on the command line. Prefer stdin, for example `uv run python scripts/prepare_env.py save-validated-scholar-headers --headers-stdin`.
9. Re-run `uv run python scripts/check_scholar_inbox_auth.py --json` against the saved config.
10. Move to NotebookLM only after Scholar Inbox is saved and validated.
11. Launch NotebookLM login with `uv run python scripts/prepare_env.py notebooklm-login` only if NotebookLM auth is missing or invalid.
12. After the user completes NotebookLM login, run `uv run python scripts/prepare_env.py smoke-test` as the final check.
13. Use `uv run python scripts/check_notebooklm_auth.py --json` only if the smoke test fails or if you need to debug NotebookLM specifically.
14. If the smoke test fails with DNS, network policy, or sandbox resolution errors, rerun the same checks in a real networked execution context before concluding the auth is bad.
15. Run `uv run python scripts/prepare_env.py doctor --json` before the first full run or before creating a scheduled automation.

## Active Profile

- The active researcher profile defaults to `<repo>/.echoes/PROFILE.md`.
- `ECHOES_PROFILE` overrides the default path.
- The repo-root `PROFILE.md` is a neutral template for GitHub users and must not be treated as a filled active profile.
- Create or update the active profile through [researcher-profile.md](researcher-profile.md): collect private evidence from user descriptions, webpages, and confirmed seed-paper PDFs, then let Codex synthesize the profile after clarifying questions.

## Doctor Check

Use `uv run python scripts/prepare_env.py doctor --json` to verify readiness without exposing secrets. It checks:

- Python and `uv`
- runtime imports
- active profile presence and template status
- Telegram credential presence without printing values
- Ghostscript availability
- saved Scholar Inbox and NotebookLM authentication

Stop on required `error` checks. Treat `warn` checks as recoverable unless the user needs that optional capability.

## Chrome MCP Guidance

- Chrome MCP attached to the user's real Chrome session is the normal path for Scholar Inbox prepare.
- If `list_pages` only shows a fresh automation tab, `about:blank`, or no real user tabs, assume MCP is not attached to the correct browser.
- A profile-lock error, `Transport closed`, or an isolated login page usually means MCP is using the wrong browser instance, not that Scholar Inbox auth failed.
- Do not repeatedly retry MCP blindly when the attach path is broken. Stop, explain the attach issue, and ask the user to fix the Chrome debugging or MCP configuration first.
- Inspect authenticated network requests to recover the full browser request headers, including the exact `Cookie` header needed by the validation script.
- Prefer the minimum Chrome MCP path needed for extraction: verify pages, reload the digest tab, list network requests, then inspect the authenticated API request.
- Do not use `document.cookie` as the main extraction path because the session may be `HttpOnly`.
- Do not default to asking the user to open DevTools and copy the cookie by hand.
- Do not echo the extracted session back to the user in the default prepare flow.
- Do not pass the extracted session as a visible command-line argument when validating or saving it.

## Save Rules

- Do not ask for save confirmation in the default prepare flow after a successful private validation.
- Store Scholar Inbox auth only in `<repo>/.echoes/credentials.env` unless `ECHOES_CONFIG_DIR` overrides the location. Save both `SCHOLAR_INBOX_SESSION` and `SCHOLAR_INBOX_REQUEST_HEADERS_JSON` when full browser request headers are available.
- Let `notebooklm login` manage NotebookLM storage. Do not mirror NotebookLM auth into `credentials.env`.

## Smoke Test Expectations

- `scripts/check_scholar_inbox_auth.py` must confirm that the Scholar Inbox API accepts the saved session and returns a digest payload.
- `scripts/check_notebooklm_auth.py` must confirm that `notebooklm auth check --json --test` succeeds and `notebooklm list --json` can read notebooks.

## Failure Handling

- If `setup` fails, fix the local Python or `uv` issue before attempting auth.
- If a direct script check fails due to missing imports or shell interpreter issues, rerun it with `uv run python ...` instead of diagnosing auth first.
- If Scholar Inbox auth fails, reacquire the session value and overwrite the saved credential automatically after successful private validation.
- If Chrome MCP hits Cloudflare inside its own browser profile, treat that as a tooling-path problem. Do not continue in the automation browser.
- If Chrome MCP shows `Transport closed`, profile-lock issues, or cannot attach to `127.0.0.1:9222`, stop and give the exact correction steps before continuing.
- If NotebookLM auth fails, rerun `uv run python scripts/prepare_env.py notebooklm-login`, complete the login in the opened Terminal window, and repeat the smoke test.
- If smoke tests fail because the execution environment cannot resolve or reach the target hosts, verify on a real network path before treating the credentials as invalid.
- If a first network validation fails but the same check later succeeds on the real network path, treat the first failure as an execution-environment issue, not an auth issue.
- If `Smoke test passed.` has already been emitted, treat that as terminal success and stop. Do not narrate additional waiting.

## Do Not Do This

- Do not start with local browser cookie-store inspection or cookie decryption.
- Do not ask the user to manually copy cookies unless they explicitly ask for a non-MCP fallback.
- Do not show the Scholar Inbox credential in chat during the default prepare flow.
- Do not put the Scholar Inbox credential directly in the visible shell command line.
- Do not use interactive background-terminal stdin to pass the Scholar Inbox credential.
- Do not infer bad auth from DNS, sandbox, or transport failures alone.
- Do not fall back to bare `python`.
- Do not move to NotebookLM before Scholar Inbox is saved and validated.
