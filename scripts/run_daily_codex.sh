#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${ECHOES_ALLOW_UNSANDBOXED:-}" != "1" ]]; then
  cat >&2 <<'EOF'
Refusing to run an unattended unsandboxed Codex job.
Set ECHOES_ALLOW_UNSANDBOXED=1 after you have reviewed the repo, the
machine-local credentials, and the cron user that will run this workflow.
EOF
  exit 64
fi

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)"
PROMPT_PATH="${REPO_ROOT}/references/daily-codex-prompt.md"
CODEX_BIN="${CODEX_BIN:-codex}"
UV_BIN="${UV_BIN:-uv}"
CONFIG_DIR="${ECHOES_CONFIG_DIR:-${REPO_ROOT}/.echoes}"
NOTEBOOKLM_HOME="${NOTEBOOKLM_HOME:-${CONFIG_DIR}/notebooklm}"
export ECHOES_CONFIG_DIR="$CONFIG_DIR"
export NOTEBOOKLM_HOME
LOG_DIR="${ECHOES_LOG_DIR:-${CONFIG_DIR}/logs}"
TIMESTAMP="$(date -u +"%Y%m%dT%H%M%SZ")"
RUN_LOG="${LOG_DIR}/daily-codex-${TIMESTAMP}.log"
LAST_MESSAGE="${LOG_DIR}/daily-codex-${TIMESTAMP}.last.md"

now_utc() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

have_executable() {
  local candidate="$1"
  [[ -x "$candidate" ]] || command -v "$candidate" >/dev/null 2>&1
}

if ! have_executable "${CODEX_BIN}"; then
  echo "codex executable not found: ${CODEX_BIN}" >&2
  exit 127
fi

if ! have_executable "${UV_BIN}"; then
  echo "uv executable not found: ${UV_BIN}" >&2
  exit 127
fi

if [[ ! -f "${PROMPT_PATH}" ]]; then
  echo "Daily Codex prompt not found: ${PROMPT_PATH}" >&2
  exit 66
fi

umask 077
mkdir -p "${LOG_DIR}"

codex_args=(
  exec
  --cd "${REPO_ROOT}"
  --dangerously-bypass-approvals-and-sandbox
  --output-last-message "${LAST_MESSAGE}"
)

if [[ -n "${CODEX_MODEL:-}" ]]; then
  codex_args+=(--model "${CODEX_MODEL}")
fi

if [[ -n "${CODEX_PROFILE:-}" ]]; then
  codex_args+=(--profile "${CODEX_PROFILE}")
fi

codex_args+=(-)

{
  echo "[$(now_utc)] Starting echoes Codex run."
  echo "Repository: ${REPO_ROOT}"
  echo "Prompt: ${PROMPT_PATH}"
  echo "Last message: ${LAST_MESSAGE}"
  "${CODEX_BIN}" "${codex_args[@]}" < "${PROMPT_PATH}"
  echo "[$(now_utc)] echoes Codex run finished."
} 2>&1 | tee "${RUN_LOG}"
