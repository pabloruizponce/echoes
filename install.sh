#!/usr/bin/env bash
set -Eeuo pipefail

DEFAULT_REPO_URL="https://github.com/pabloruizponce/echoes.git"
DEFAULT_BRANCH="main"
DEFAULT_INSTALL_DIR="${HOME}/echoes"
DEFAULT_CRON_PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
DEFAULT_CHROME_DEBUG_PORT="9222"
CRON_BEGIN="# BEGIN ECHOES DAILY"
CRON_END="# END ECHOES DAILY"

REPO_URL="${ECHOES_REPO_URL:-$DEFAULT_REPO_URL}"
BRANCH="${ECHOES_BRANCH:-$DEFAULT_BRANCH}"
INSTALL_DIR="${ECHOES_INSTALL_DIR:-}"
CONFIG_DIR="${ECHOES_CONFIG_DIR:-}"
NOTEBOOKLM_HOME_VALUE="${NOTEBOOKLM_HOME:-}"
CRON_TIME="${ECHOES_CRON_TIME:-}"
CRON_PATH_VALUE="${ECHOES_CRON_PATH:-$DEFAULT_CRON_PATH}"

DRY_RUN=0
NO_PROMPT="${ECHOES_NO_PROMPT:-0}"
VERBOSE="${ECHOES_VERBOSE:-0}"
SKIP_SYSTEM=0
SKIP_CODEX_INSTALL=0
SKIP_MCP=0
SKIP_AUTH=0
SKIP_PROFILE=0
SKIP_CRON=0

OS_NAME=""
PACKAGE_MANAGER=""
REPO_DIR=""
UNSANDBOXED_CODEX_CONFIRMED=0
DOCTOR_OK=0
CODEX_BIN_VALUE=""
UV_BIN_VALUE=""

SCRIPT_SOURCE="${BASH_SOURCE[0]:-}"
SCRIPT_DIR=""
if [[ -n "$SCRIPT_SOURCE" && -f "$SCRIPT_SOURCE" ]]; then
  SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$SCRIPT_SOURCE")" && pwd)"
fi

ORIGINAL_PATH="${PATH:-}"
export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"

usage() {
  cat <<'EOF'
echoes installer

Usage:
  ./install.sh [options]
  curl -fsSL https://raw.githubusercontent.com/pabloruizponce/echoes/main/install.sh | bash -s -- [options]

Options:
  --install-dir PATH       Checkout directory when the repo is not already present
  --repo-url URL           Git repository URL to clone
  --branch NAME            Git branch to clone
  --config-dir PATH        Private runtime state directory (default: <checkout>/.echoes)
  --notebooklm-home PATH   NotebookLM storage root (default: <runtime>/notebooklm)
  --cron-time HH:MM        Daily cron time for scripts/run_daily_codex.sh
  --no-prompt              Do not ask interactive questions; skip auth/profile/cron prompts
  --dry-run                Print actions without changing the system
  --verbose                Print extra diagnostic messages
  --skip-system            Do not install/check OS packages
  --skip-codex-install     Do not install Codex CLI if missing
  --skip-mcp               Do not add Chrome DevTools MCP to Codex
  --skip-auth              Skip Telegram, NotebookLM, and Scholar Inbox auth setup
  --skip-profile           Skip researcher profile setup
  --skip-cron              Skip cron setup
  -h, --help               Show this help

Environment:
  ECHOES_INSTALL_DIR, ECHOES_REPO_URL, ECHOES_BRANCH
  ECHOES_CONFIG_DIR, NOTEBOOKLM_HOME, ECHOES_CRON_TIME
  ECHOES_CRON_PATH, ECHOES_NO_PROMPT=1, ECHOES_VERBOSE=1
EOF
}

log() {
  printf '%s\n' "$*"
}

info() {
  printf '==> %s\n' "$*"
}

warn() {
  printf 'Warning: %s\n' "$*" >&2
}

die() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

debug() {
  if [[ "$VERBOSE" == "1" ]]; then
    printf 'debug: %s\n' "$*" >&2
  fi
}

path_contains_entry() {
  local path_list="$1"
  local entry="$2"
  [[ -n "$entry" ]] || return 1
  case ":$path_list:" in
    *":$entry:"*) return 0 ;;
    *) return 1 ;;
  esac
}

prepend_path_entries() {
  local path_list="$1"
  shift

  local result="$path_list"
  local -a entries=("$@")
  local index
  local entry

  for ((index=${#entries[@]} - 1; index >= 0; index--)); do
    entry="${entries[$index]}"
    [[ -n "$entry" ]] || continue
    if ! path_contains_entry "$result" "$entry"; then
      if [[ -n "$result" ]]; then
        result="${entry}:${result}"
      else
        result="$entry"
      fi
    fi
  done

  printf '%s' "$result"
}

tool_dir() {
  local tool_path="$1"
  if [[ "$tool_path" == */* ]]; then
    dirname "$tool_path"
  fi
}

format_cmd() {
  local out=""
  local part
  for part in "$@"; do
    printf -v part '%q' "$part"
    out+="${part} "
  done
  printf '%s' "${out% }"
}

run_cmd() {
  info "$(format_cmd "$@")"
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '[dry-run] skipped\n'
    return 0
  fi
  "$@"
}

run_in_repo() {
  if [[ "$DRY_RUN" == "1" ]]; then
    info "cd $(shell_quote "$REPO_DIR") && $(format_cmd "$@")"
    printf '[dry-run] skipped\n'
    return 0
  fi
  (cd "$REPO_DIR" && "$@")
}

prompt_allowed() {
  [[ "$NO_PROMPT" != "1" && -t 0 ]]
}

ask_yes_no() {
  local prompt="$1"
  local default="${2:-y}"
  local suffix="[Y/n]"
  local answer
  if [[ "$default" == "n" ]]; then
    suffix="[y/N]"
  fi
  if ! prompt_allowed; then
    [[ "$default" == "y" ]]
    return
  fi
  while true; do
    read -r -p "$prompt $suffix " answer
    answer="${answer:-$default}"
    case "$answer" in
      y|Y|yes|YES) return 0 ;;
      n|N|no|NO) return 1 ;;
      *) warn "Please answer yes or no." ;;
    esac
  done
}

read_line() {
  local prompt="$1"
  local default="${2:-}"
  local answer
  if ! prompt_allowed; then
    printf '%s' "$default"
    return
  fi
  if [[ -n "$default" ]]; then
    read -r -p "$prompt [$default] " answer
    printf '%s' "${answer:-$default}"
  else
    read -r -p "$prompt " answer
    printf '%s' "$answer"
  fi
}

read_secret() {
  local prompt="$1"
  local answer
  if ! prompt_allowed; then
    printf ''
    return
  fi
  read -r -s -p "$prompt " answer
  printf '\n' >&2
  printf '%s' "$answer"
}

choose_option() {
  local prompt="$1"
  local default="$2"
  shift 2
  local -a options=("$@")
  local answer
  local index

  if ! prompt_allowed; then
    printf '%s' "$default"
    return
  fi

  while true; do
    printf '%s\n' "$prompt" >&2
    index=1
    for option in "${options[@]}"; do
      printf '  %d) %s\n' "$index" "$option" >&2
      index=$((index + 1))
    done
    read -r -p "Choose an option [$default]: " answer
    answer="${answer:-$default}"
    if [[ "$answer" =~ ^[0-9]+$ ]] && (( answer >= 1 && answer <= ${#options[@]} )); then
      printf '%s' "$answer"
      return
    fi
    warn "Please choose a valid option number."
  done
}

shell_quote() {
  local value="$1"
  printf "'"
  printf '%s' "$value" | sed "s/'/'\\\\''/g"
  printf "'"
}

trim() {
  local value="$*"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --install-dir) INSTALL_DIR="${2:?--install-dir requires a value}"; shift 2 ;;
      --install-dir=*) INSTALL_DIR="${1#*=}"; shift ;;
      --repo-url) REPO_URL="${2:?--repo-url requires a value}"; shift 2 ;;
      --repo-url=*) REPO_URL="${1#*=}"; shift ;;
      --branch) BRANCH="${2:?--branch requires a value}"; shift 2 ;;
      --branch=*) BRANCH="${1#*=}"; shift ;;
      --config-dir) CONFIG_DIR="${2:?--config-dir requires a value}"; shift 2 ;;
      --config-dir=*) CONFIG_DIR="${1#*=}"; shift ;;
      --notebooklm-home) NOTEBOOKLM_HOME_VALUE="${2:?--notebooklm-home requires a value}"; shift 2 ;;
      --notebooklm-home=*) NOTEBOOKLM_HOME_VALUE="${1#*=}"; shift ;;
      --cron-time) CRON_TIME="${2:?--cron-time requires a value}"; shift 2 ;;
      --cron-time=*) CRON_TIME="${1#*=}"; shift ;;
      --no-prompt) NO_PROMPT=1; shift ;;
      --dry-run) DRY_RUN=1; shift ;;
      --verbose) VERBOSE=1; shift ;;
      --skip-system) SKIP_SYSTEM=1; shift ;;
      --skip-codex-install) SKIP_CODEX_INSTALL=1; shift ;;
      --skip-mcp) SKIP_MCP=1; shift ;;
      --skip-auth) SKIP_AUTH=1; shift ;;
      --skip-profile) SKIP_PROFILE=1; shift ;;
      --skip-cron) SKIP_CRON=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) die "Unknown option: $1" ;;
    esac
  done
}

print_banner() {
  cat <<'EOF'

echoes Installer
------------------------
This installs local tooling, configures private runtime state, and hands
MCP/browser and profile synthesis steps to Codex where judgment is needed.

EOF
}

detect_os() {
  local raw_os="${ECHOES_OS_OVERRIDE:-$(uname -s)}"
  case "$raw_os" in
    Darwin) OS_NAME="macos" ;;
    Linux) OS_NAME="linux" ;;
    *) die "Unsupported OS '$raw_os'. Use macOS, Linux, or WSL2 Linux." ;;
  esac
  debug "os=$OS_NAME"
}

detect_package_manager() {
  PACKAGE_MANAGER="none"
  if [[ "$OS_NAME" == "macos" ]]; then
    if command -v brew >/dev/null 2>&1; then
      PACKAGE_MANAGER="brew"
    fi
  else
    for candidate in apt-get dnf yum pacman; do
      if command -v "$candidate" >/dev/null 2>&1; then
        PACKAGE_MANAGER="$candidate"
        break
      fi
    done
  fi
  debug "package_manager=$PACKAGE_MANAGER"
}

sudo_cmd() {
  if [[ "$EUID" -eq 0 ]]; then
    run_cmd "$@"
  else
    if ! command -v sudo >/dev/null 2>&1; then
      die "sudo is required to install system packages. Install dependencies manually or rerun with --skip-system."
    fi
    run_cmd sudo "$@"
  fi
}

install_packages() {
  if [[ $# -eq 0 ]]; then
    return 0
  fi
  case "$PACKAGE_MANAGER" in
    brew)
      run_cmd brew install "$@"
      ;;
    apt-get)
      sudo_cmd apt-get update
      sudo_cmd apt-get install -y "$@"
      ;;
    dnf)
      sudo_cmd dnf install -y "$@"
      ;;
    yum)
      sudo_cmd yum install -y "$@"
      ;;
    pacman)
      sudo_cmd pacman -Sy --needed --noconfirm "$@"
      ;;
    *)
      warn "No supported package manager found. Install manually: $*"
      ;;
  esac
}

python_ok() {
  command -v python3 >/dev/null 2>&1 && python3 - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
}

node_ok() {
  command -v node >/dev/null 2>&1 && node -e '
const parts = process.versions.node.split(".").map(Number);
process.exit(parts[0] > 20 || (parts[0] === 20 && parts[1] >= 19) ? 0 : 1);
' >/dev/null 2>&1
}

find_chrome() {
  if [[ "$OS_NAME" == "macos" ]]; then
    local mac_paths=(
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
      "/Applications/Chromium.app/Contents/MacOS/Chromium"
    )
    local path
    for path in "${mac_paths[@]}"; do
      if [[ -x "$path" ]]; then
        printf '%s' "$path"
        return 0
      fi
    done
  else
    local candidate
    for candidate in google-chrome google-chrome-stable chromium chromium-browser; do
      if command -v "$candidate" >/dev/null 2>&1; then
        command -v "$candidate"
        return 0
      fi
    done
  fi
  return 1
}

ensure_system_dependencies() {
  if [[ "$SKIP_SYSTEM" == "1" ]]; then
    info "Skipping system dependency installation."
    return 0
  fi

  info "Checking system dependencies."
  if [[ "$OS_NAME" == "macos" ]]; then
    if [[ "$PACKAGE_MANAGER" == "brew" ]]; then
      install_packages git curl python ffmpeg ghostscript node
    else
      warn "Homebrew was not found. Install git, curl, Python 3.10+, ffmpeg, ghostscript, and Node.js 20.19+ manually."
    fi
  else
    case "$PACKAGE_MANAGER" in
      apt-get)
        install_packages git curl python3 python3-venv ffmpeg ghostscript xdg-utils
        ;;
      dnf|yum)
        install_packages git curl python3 ffmpeg ghostscript xdg-utils nodejs npm
        ;;
      pacman)
        install_packages git curl python ffmpeg ghostscript xdg-utils nodejs npm
        ;;
      *)
        warn "Install git, curl, Python 3.10+, ffmpeg, ghostscript, xdg-utils, and Node.js 20.19+ manually."
        ;;
    esac
  fi

  if [[ "$DRY_RUN" == "1" ]]; then
    info "Dry run: skipping system dependency validation."
    return 0
  fi

  if ! python_ok; then
    die "Python 3.10+ is required. Install it, then rerun the installer."
  fi
  if ! command -v ffmpeg >/dev/null 2>&1; then
    die "ffmpeg is required for Telegram voice-note conversion."
  fi
  if ! command -v gs >/dev/null 2>&1; then
    warn "Ghostscript was not found. PDF compression will be skipped when unavailable."
  fi

  ensure_node_for_mcp

  if ! find_chrome >/dev/null 2>&1; then
    warn "Chrome/Chromium was not found. Install it before Scholar Inbox auth capture."
  fi
}

ensure_node_for_mcp() {
  if node_ok && command -v npm >/dev/null 2>&1; then
    return 0
  fi

  warn "Node.js 20.19+ and npm are required for Chrome DevTools MCP."
  if [[ "$PACKAGE_MANAGER" == "brew" ]]; then
    run_cmd brew install node
  elif [[ "$PACKAGE_MANAGER" == "apt-get" && "$DRY_RUN" == "1" ]]; then
    run_cmd bash -c "curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -"
    sudo_cmd apt-get install -y nodejs
  elif [[ "$PACKAGE_MANAGER" == "apt-get" ]] && prompt_allowed && ask_yes_no "Install Node.js 22 from NodeSource for Chrome DevTools MCP?" "y"; then
    run_cmd bash -c "curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -"
    sudo_cmd apt-get install -y nodejs
  elif [[ "$PACKAGE_MANAGER" == "apt-get" ]]; then
    warn "Skipping Node.js installation in non-interactive mode."
  else
    warn "Install Node.js 20.19+ and npm manually before configuring Chrome DevTools MCP."
  fi

  if [[ "$DRY_RUN" != "1" ]] && (! node_ok || ! command -v npm >/dev/null 2>&1); then
    die "Node.js 20.19+ with npm is still missing."
  fi
}

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    UV_BIN_VALUE="$(command -v uv)"
    return 0
  fi
  info "Installing uv."
  run_cmd bash -c "curl -LsSf https://astral.sh/uv/install.sh | sh"
  if [[ "$DRY_RUN" != "1" ]] && ! command -v uv >/dev/null 2>&1; then
    die "uv was installed but is not on PATH. Add ~/.local/bin or ~/.cargo/bin to PATH and rerun."
  fi
  UV_BIN_VALUE="$(command -v uv 2>/dev/null || true)"
}

ensure_codex_cli() {
  if command -v codex >/dev/null 2>&1; then
    info "Codex CLI is available."
  else
    if [[ "$SKIP_CODEX_INSTALL" == "1" ]]; then
      die "Codex CLI is missing. Install it or rerun without --skip-codex-install."
    fi
    info "Installing Codex CLI."
    if [[ "$PACKAGE_MANAGER" == "brew" ]]; then
      run_cmd brew install codex
    elif command -v npm >/dev/null 2>&1 || [[ "$DRY_RUN" == "1" ]]; then
      run_cmd npm install -g @openai/codex
    else
      die "Codex CLI is missing and npm is unavailable. Install Node/npm, then rerun."
    fi
  fi

  if [[ "$DRY_RUN" != "1" ]] && ! command -v codex >/dev/null 2>&1; then
    die "Codex CLI is still missing after installation."
  fi

  CODEX_BIN_VALUE="$(command -v codex 2>/dev/null || true)"

  if prompt_allowed && ask_yes_no "Run codex login now?" "y"; then
    run_cmd codex login
  else
    warn "Skipping codex login prompt. Ensure 'codex' is authenticated before auth/profile handoff steps."
  fi
}

is_checkout() {
  local path="$1"
  [[ -n "$path" && -f "$path/pyproject.toml" && -f "$path/scripts/prepare_env.py" ]]
}

ensure_checkout() {
  if [[ -z "$INSTALL_DIR" ]]; then
    if is_checkout "$SCRIPT_DIR"; then
      REPO_DIR="$SCRIPT_DIR"
    else
      REPO_DIR="$DEFAULT_INSTALL_DIR"
    fi
  else
    REPO_DIR="$INSTALL_DIR"
  fi

  if is_checkout "$REPO_DIR"; then
    info "Using echoes checkout at $REPO_DIR."
    return 0
  fi

  if [[ -e "$REPO_DIR" ]]; then
    die "$REPO_DIR exists but does not look like an echoes checkout."
  fi

  info "Cloning echoes into $REPO_DIR."
  run_cmd git clone --branch "$BRANCH" "$REPO_URL" "$REPO_DIR"
}

resolve_runtime_paths() {
  if [[ -z "$CONFIG_DIR" ]]; then
    CONFIG_DIR="$REPO_DIR/.echoes"
  fi
  if [[ -z "$NOTEBOOKLM_HOME_VALUE" ]]; then
    NOTEBOOKLM_HOME_VALUE="$CONFIG_DIR/notebooklm"
  fi
}

prepare_private_state() {
  export ECHOES_CONFIG_DIR="$CONFIG_DIR"
  export NOTEBOOKLM_HOME="$NOTEBOOKLM_HOME_VALUE"

  info "Private runtime directory: $CONFIG_DIR"
  info "NotebookLM home: $NOTEBOOKLM_HOME_VALUE"
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '[dry-run] would create private runtime directories with mode 700\n'
    return 0
  fi
  mkdir -p "$CONFIG_DIR" "$NOTEBOOKLM_HOME_VALUE"
  chmod 700 "$CONFIG_DIR"
  chmod 700 "$NOTEBOOKLM_HOME_VALUE"
}

prepare_project_environment() {
  ensure_uv
  info "Syncing project environment."
  run_in_repo uv sync
  info "Running project setup."
  run_in_repo uv run python scripts/prepare_env.py setup
}

ensure_chrome_mcp() {
  if [[ "$SKIP_MCP" == "1" ]]; then
    info "Skipping Chrome DevTools MCP setup."
    return 0
  fi
  if [[ "$SKIP_AUTH" == "1" && "$SKIP_PROFILE" == "1" ]]; then
    debug "MCP may not be needed when auth and profile are both skipped."
  fi
  if [[ "$DRY_RUN" != "1" ]] && ! command -v codex >/dev/null 2>&1; then
    die "Codex CLI is required to configure Chrome DevTools MCP."
  fi
  if [[ "$DRY_RUN" != "1" ]] && codex mcp get chrome-devtools >/dev/null 2>&1; then
    info "Chrome DevTools MCP is already configured."
    return 0
  fi
  run_cmd codex mcp add chrome-devtools -- npx -y chrome-devtools-mcp@latest --browser-url=http://127.0.0.1:${DEFAULT_CHROME_DEBUG_PORT}
}

discover_telegram_chat_id() {
  local token="$1"

  if [[ "$DRY_RUN" == "1" ]]; then
    if [[ -n "$token" ]]; then
      info "cd $(shell_quote "$REPO_DIR") && printf '[redacted]' | uv run python scripts/prepare_env.py discover-telegram-chat-id --token-stdin"
    else
      info "cd $(shell_quote "$REPO_DIR") && uv run python scripts/prepare_env.py discover-telegram-chat-id"
    fi
    printf '[dry-run] skipped\n'
    return 0
  fi

  if [[ -n "$token" ]]; then
    (cd "$REPO_DIR" && printf '%s' "$token" | uv run python scripts/prepare_env.py discover-telegram-chat-id --token-stdin)
  else
    run_in_repo uv run python scripts/prepare_env.py discover-telegram-chat-id
  fi
}

save_telegram_chat_id_manually() {
  local token="$1"
  local chat_id
  chat_id="$(read_line "Telegram chat ID:")"
  if [[ -z "$chat_id" ]]; then
    warn "Telegram chat ID was empty; skipping save."
    return 0
  fi

  if [[ "$DRY_RUN" == "1" ]]; then
    if [[ -n "$token" ]]; then
      info "cd $(shell_quote "$REPO_DIR") && printf '[redacted]' | uv run python scripts/prepare_env.py save-telegram-config --chat-id [redacted] --token-stdin"
    else
      info "cd $(shell_quote "$REPO_DIR") && uv run python scripts/prepare_env.py save-telegram-chat-id --chat-id [redacted]"
    fi
    printf '[dry-run] skipped\n'
    return 0
  fi

  if [[ -n "$token" ]]; then
    (cd "$REPO_DIR" && printf '%s' "$token" | uv run python scripts/prepare_env.py save-telegram-config --chat-id "$chat_id" --token-stdin)
  else
    run_in_repo uv run python scripts/prepare_env.py save-telegram-chat-id --chat-id "$chat_id"
  fi
}

save_telegram_credentials() {
  if [[ "$SKIP_AUTH" == "1" ]]; then
    info "Skipping Telegram credential setup."
    return 0
  fi
  if ! prompt_allowed; then
    warn "Skipping Telegram credential prompts in non-interactive mode."
    return 0
  fi

  local credentials_path="$CONFIG_DIR/credentials.env"
  local saved_token=""
  if [[ -f "$credentials_path" ]]; then
    saved_token="$(sed -n 's/^TELEGRAM_BOT_TOKEN=//p' "$credentials_path" | head -n 1)"
  fi

  if [[ -f "$credentials_path" ]] && grep -q '^TELEGRAM_BOT_TOKEN=' "$credentials_path" && grep -q '^TELEGRAM_CHAT_ID=' "$credentials_path"; then
    if ! ask_yes_no "Telegram credentials already exist. Update them?" "n"; then
      return 0
    fi
  elif ! ask_yes_no "Set up Telegram bot delivery now?" "y"; then
    return 0
  fi

  local token=""
  if [[ -n "$saved_token" ]] && ask_yes_no "Reuse the saved Telegram bot token for chat discovery?" "y"; then
    token=""
  else
    token="$(read_secret "Telegram bot token:")"
    if [[ -z "$token" && -z "$saved_token" ]]; then
      warn "Telegram bot token was empty; skipping setup."
      return 0
    fi
  fi

  while true; do
    if discover_telegram_chat_id "$token"; then
      return 0
    fi

    warn "Telegram chat ID discovery did not complete."
    if ask_yes_no "Retry Telegram chat ID discovery?" "y"; then
      continue
    fi
    if ! ask_yes_no "Enter Telegram chat ID manually instead?" "y"; then
      return 0
    fi
    save_telegram_chat_id_manually "$token"
    return 0
  done
}

run_notebooklm_login() {
  if [[ "$SKIP_AUTH" == "1" ]]; then
    info "Skipping NotebookLM login."
    return 0
  fi
  if ! prompt_allowed; then
    warn "Skipping NotebookLM login in non-interactive mode."
    return 0
  fi
  if ask_yes_no "Run NotebookLM browser login now?" "y"; then
    run_in_repo uv run python scripts/prepare_env.py notebooklm-login
    run_in_repo uv run python scripts/check_notebooklm_auth.py --json || warn "NotebookLM auth check failed; doctor will report details later."
  fi
}

confirm_unsandboxed_codex() {
  if [[ "$UNSANDBOXED_CODEX_CONFIRMED" == "1" ]]; then
    return 0
  fi
  if ! prompt_allowed; then
    warn "Skipping Codex handoff in non-interactive mode."
    return 1
  fi
  cat <<'EOF'
Codex handoff will run with full local access so it can use MCP, save private
credentials, and write the active researcher profile under your private runtime.
EOF
  if ask_yes_no "Allow this installer to invoke codex exec for setup handoff?" "y"; then
    UNSANDBOXED_CODEX_CONFIRMED=1
    return 0
  fi
  return 1
}

run_codex_handoff() {
  local label="$1"
  local prompt="$2"
  if ! confirm_unsandboxed_codex; then
    warn "Skipping Codex handoff: $label."
    return 0
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    info "codex exec --cd $(shell_quote "$REPO_DIR") --dangerously-bypass-approvals-and-sandbox -"
    printf '[dry-run] prompt: %s\n' "$label"
    return 0
  fi
  printf '%s\n' "$prompt" | codex exec --cd "$REPO_DIR" --dangerously-bypass-approvals-and-sandbox -
}

launch_chrome_for_scholar() {
  local chrome_bin
  chrome_bin="$(find_chrome || true)"
  if [[ -z "$chrome_bin" ]]; then
    warn "Chrome/Chromium was not found. Open https://www.scholar-inbox.com/digest manually in a browser with remote debugging on port ${DEFAULT_CHROME_DEBUG_PORT}."
    return 0
  fi

  local chrome_profile="$CONFIG_DIR/chrome-profile"
  info "Launching Chrome/Chromium with remote debugging on port ${DEFAULT_CHROME_DEBUG_PORT}."
  if [[ "$DRY_RUN" == "1" ]]; then
    info "$(shell_quote "$chrome_bin") --remote-debugging-port=${DEFAULT_CHROME_DEBUG_PORT} --user-data-dir=$(shell_quote "$chrome_profile") https://www.scholar-inbox.com/digest"
    printf '[dry-run] skipped\n'
    return 0
  fi
  mkdir -p "$chrome_profile"
  "$chrome_bin" --remote-debugging-port="${DEFAULT_CHROME_DEBUG_PORT}" --user-data-dir="$chrome_profile" "https://www.scholar-inbox.com/digest" >/tmp/echoes-chrome.log 2>&1 &
}

configure_scholar_inbox_auth() {
  if [[ "$SKIP_AUTH" == "1" ]]; then
    info "Skipping Scholar Inbox auth setup."
    return 0
  fi
  if ! prompt_allowed; then
    warn "Skipping Scholar Inbox auth capture in non-interactive mode."
    return 0
  fi
  if ! ask_yes_no "Capture Scholar Inbox auth through Chrome DevTools MCP now?" "y"; then
    return 0
  fi

  launch_chrome_for_scholar
  log "Log in at https://www.scholar-inbox.com/digest in the launched browser."
  read -r -p "Press Enter after Scholar Inbox is logged in and the digest page is open. " _

  local prompt
  prompt="$(cat <<'EOF'
Use `$echoes` to complete only the Scholar Inbox auth capture for this local setup.

Chrome should already be running with remote debugging on http://127.0.0.1:9222 and the user should already be logged in at https://www.scholar-inbox.com/digest.

Use Chrome DevTools MCP attached to that real browser session. Inspect the authenticated Scholar Inbox digest/API network request headers, then privately validate and save them with `uv run python scripts/prepare_env.py save-validated-scholar-headers --headers-stdin`. Do not print or reveal cookies, saved browser headers, tokens, chat IDs, or NotebookLM auth state. After saving, run `uv run python scripts/check_scholar_inbox_auth.py --json` and report only whether the check passed or the exact blocker.
EOF
)"
  run_codex_handoff "Scholar Inbox auth capture" "$prompt"
}

import_researcher_profile() {
  local source_path
  source_path="$(read_line "Path to the markdown profile to import:")"
  source_path="$(trim "$source_path")"
  source_path="${source_path/#\~/$HOME}"
  if [[ -z "$source_path" ]]; then
    warn "Profile import path was empty; skipping import."
    return 0
  fi
  run_in_repo uv run python scripts/researcher_profile.py import-markdown --source "$source_path" --overwrite
}

synthesize_researcher_profile() {
  local profile_path="$1"
  if [[ "$SKIP_PROFILE" == "1" ]]; then
    return 0
  fi

  local name role description webpages paper_urls paper_descriptions
  name="$(read_line "Researcher name (optional):")"
  role="$(read_line "Role/affiliation (optional):")"
  description="$(read_line "Current research interests, goals, or learning priorities:")"
  webpages="$(read_line "Researcher/lab/project webpage URLs, comma-separated (optional):")"
  paper_urls="$(read_line "Confirmed seed paper PDF URLs, comma-separated (optional):")"
  paper_descriptions="$(read_line "Seed paper or topic descriptions for Codex to consider, comma-separated (optional):")"

  local -a args
  args=(uv run python scripts/researcher_profile.py collect-evidence --json)
  if [[ -n "$(trim "$name")" ]]; then
    args+=(--description "Researcher name: $(trim "$name")")
  fi
  if [[ -n "$(trim "$role")" ]]; then
    args+=(--description "Role or affiliation: $(trim "$role")")
  fi
  if [[ -n "$(trim "$description")" ]]; then
    args+=(--description "$(trim "$description")")
  fi

  local item
  IFS=',' read -r -a webpage_items <<< "$webpages"
  for item in "${webpage_items[@]}"; do
    item="$(trim "$item")"
    [[ -n "$item" ]] && args+=(--webpage "$item")
  done
  IFS=',' read -r -a paper_url_items <<< "$paper_urls"
  for item in "${paper_url_items[@]}"; do
    item="$(trim "$item")"
    [[ -n "$item" ]] && args+=(--paper-url "$item")
  done
  IFS=',' read -r -a paper_description_items <<< "$paper_descriptions"
  for item in "${paper_description_items[@]}"; do
    item="$(trim "$item")"
    [[ -n "$item" ]] && args+=(--paper-description "$item")
  done

  if [[ "${#args[@]}" -eq 6 ]]; then
    warn "No researcher evidence was provided; skipping profile synthesis."
    return 0
  fi

  run_in_repo "${args[@]}"

  local evidence_path="$CONFIG_DIR/profile-evidence/latest.json"
  local prompt
  prompt="$(cat <<EOF
Use \$echoes to create or update only the active private researcher profile for this echoes setup.

Read the evidence bundle at:
$evidence_path

Write the active profile to:
$profile_path

Use the repository rules in references/researcher-profile.md. Do not write the filled profile to the repo-root template. Do not reveal private evidence in chat. This installer run is non-conversational, so ask no follow-up questions; instead, make conservative assumptions from the provided evidence and record uncertainty in the "Open Uncertainties Or Assumptions" section.
EOF
  )"
  run_codex_handoff "researcher profile synthesis" "$prompt"
}

configure_researcher_profile() {
  if [[ "$SKIP_PROFILE" == "1" ]]; then
    info "Skipping researcher profile setup."
    return 0
  fi
  if ! prompt_allowed; then
    warn "Skipping researcher profile prompts in non-interactive mode."
    return 0
  fi

  local profile_path="$CONFIG_DIR/PROFILE.md"
  if [[ -f "$profile_path" ]] && ! grep -q 'Status: Template' "$profile_path"; then
    if ! ask_yes_no "A filled researcher profile already exists. Update it?" "n"; then
      return 0
    fi
  elif ! ask_yes_no "Create the active researcher profile now?" "y"; then
    return 0
  fi

  local choice
  choice="$(choose_option \
    "How should we set up the active researcher profile?" \
    "2" \
    "Import existing markdown profile" \
    "Create or update it from evidence and Codex synthesis")"

  case "$choice" in
    1) import_researcher_profile ;;
    2) synthesize_researcher_profile "$profile_path" ;;
    *) die "Unsupported researcher profile option: $choice" ;;
  esac
}

run_doctor() {
  info "Running final doctor check."
  if run_in_repo uv run python scripts/prepare_env.py doctor --json; then
    DOCTOR_OK=1
  else
    DOCTOR_OK=0
    warn "Doctor reported required setup errors. Fix them before scheduling daily runs."
  fi
}

validate_cron_time() {
  local value="$1"
  [[ "$value" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]]
}

render_cron_block() {
  local repo_dir="$1"
  local config_dir="$2"
  local notebooklm_home="$3"
  local cron_time="$4"
  local cron_path="$5"
  local codex_bin="${6:-}"
  local uv_bin="${7:-}"
  validate_cron_time "$cron_time" || die "Invalid cron time '$cron_time'. Use HH:MM in 24-hour time."

  local hour="${cron_time%:*}"
  local minute="${cron_time#*:}"
  hour="$((10#$hour))"
  minute="$((10#$minute))"

  local log_dir="${config_dir}/logs"
  local wrapper_log="${log_dir}/cron-wrapper.log"
  local env_bits
  env_bits="ECHOES_ALLOW_UNSANDBOXED=1 ECHOES_CONFIG_DIR=$(shell_quote "$config_dir")"
  if [[ -n "$notebooklm_home" ]]; then
    env_bits+=" NOTEBOOKLM_HOME=$(shell_quote "$notebooklm_home")"
  fi
  env_bits+=" PATH=$(shell_quote "$cron_path")"
  if [[ -n "$codex_bin" ]]; then
    env_bits+=" CODEX_BIN=$(shell_quote "$codex_bin")"
  fi
  if [[ -n "$uv_bin" ]]; then
    env_bits+=" UV_BIN=$(shell_quote "$uv_bin")"
  fi

  cat <<EOF
$CRON_BEGIN
# Managed by echoes install.sh. Re-run install.sh to update safely.
$minute $hour * * * mkdir -p $(shell_quote "$log_dir") && cd $(shell_quote "$repo_dir") && $env_bits $(shell_quote "$repo_dir/scripts/run_daily_codex.sh") >> $(shell_quote "$wrapper_log") 2>&1
$CRON_END
EOF
}

remove_managed_cron_block() {
  awk -v begin="$CRON_BEGIN" -v end="$CRON_END" '
    $0 == begin {skip=1; next}
    $0 == end {skip=0; next}
    !skip {print}
  '
}

merge_cron_text() {
  local existing="$1"
  local block="$2"
  local cleaned
  cleaned="$(printf '%s\n' "$existing" | remove_managed_cron_block)"
  cleaned="$(printf '%s\n' "$cleaned" | sed '/^[[:space:]]*$/d')"
  if [[ -n "$cleaned" ]]; then
    printf '%s\n\n%s\n' "$cleaned" "$block"
  else
    printf '%s\n' "$block"
  fi
}

configure_cron() {
  if [[ "$SKIP_CRON" == "1" ]]; then
    info "Skipping cron setup."
    return 0
  fi

  if [[ "$DOCTOR_OK" != "1" && "$DRY_RUN" != "1" ]]; then
    warn "Skipping cron setup because doctor did not pass."
    return 0
  fi

  if [[ -z "$CRON_TIME" ]]; then
    if ! prompt_allowed; then
      warn "Skipping cron setup in non-interactive mode because --cron-time was not provided."
      return 0
    fi
    if ! ask_yes_no "Install or update a daily cron job?" "y"; then
      return 0
    fi
    CRON_TIME="$(read_line "Daily run time in 24-hour HH:MM format:" "07:15")"
  elif ! prompt_allowed && [[ "$NO_PROMPT" == "1" ]]; then
    info "Using --cron-time $CRON_TIME for non-interactive cron setup."
  fi

  validate_cron_time "$CRON_TIME" || die "Invalid cron time '$CRON_TIME'. Use HH:MM in 24-hour time."

  local cron_path="${CRON_PATH_VALUE}"
  local codex_dir=""
  local uv_dir=""
  codex_dir="$(tool_dir "$CODEX_BIN_VALUE")"
  uv_dir="$(tool_dir "$UV_BIN_VALUE")"
  cron_path="$(prepend_path_entries "$cron_path" "$codex_dir" "$uv_dir")"
  local block
  block="$(render_cron_block "$REPO_DIR" "$CONFIG_DIR" "$NOTEBOOKLM_HOME_VALUE" "$CRON_TIME" "$cron_path" "$CODEX_BIN_VALUE" "$UV_BIN_VALUE")"

  if [[ "$DRY_RUN" == "1" ]]; then
    info "Cron block that would be installed:"
    printf '%s\n' "$block"
    return 0
  fi

  if ! command -v crontab >/dev/null 2>&1; then
    warn "crontab command was not found. Add this block manually:"
    printf '%s\n' "$block"
    return 0
  fi

  local existing merged
  existing="$(crontab -l 2>/dev/null || true)"
  merged="$(merge_cron_text "$existing" "$block")"
  printf '%s\n' "$merged" | crontab -
  info "Installed echoes cron block."
}

main() {
  parse_args "$@"
  print_banner
  detect_os
  detect_package_manager
  ensure_checkout
  resolve_runtime_paths
  prepare_private_state
  ensure_system_dependencies
  ensure_codex_cli
  prepare_project_environment
  ensure_chrome_mcp
  save_telegram_credentials
  run_notebooklm_login
  configure_scholar_inbox_auth
  configure_researcher_profile
  run_doctor
  configure_cron

  local doctor_status="needs attention"
  if [[ "$DOCTOR_OK" == "1" ]]; then
    doctor_status="passed"
  fi

  local shell_path_note=""
  local note_dirs=""
  note_dirs="$(prepend_path_entries "" "$(tool_dir "$CODEX_BIN_VALUE")" "$(tool_dir "$UV_BIN_VALUE")")"
  if [[ -n "$note_dirs" ]]; then
    local -a note_dir_entries=()
    IFS=':' read -r -a note_dir_entries <<<"$note_dirs"
    local missing_dirs=()
    local dir
    for dir in "${note_dir_entries[@]}"; do
      if [[ -n "$dir" ]] && ! path_contains_entry "$ORIGINAL_PATH" "$dir"; then
        missing_dirs+=("$dir")
      fi
    done
    if (( ${#missing_dirs[@]} > 0 )); then
      shell_path_note="$(IFS=:; printf '%s' "${missing_dirs[*]}")"
    fi
  fi

  cat <<EOF

echoes installer finished.

Repo:        $REPO_DIR
Config:      $CONFIG_DIR
Doctor:      $doctor_status
Codex CLI:   ${CODEX_BIN_VALUE:-not found}
uv:          ${UV_BIN_VALUE:-not found}

Run readiness again with:
  cd $(shell_quote "$REPO_DIR") && uv run python scripts/prepare_env.py doctor --json
EOF

  if [[ -n "$shell_path_note" ]]; then
    cat <<EOF

Add this directory list to your shell profile if future terminals cannot find codex or uv:
  $shell_path_note
EOF
  fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
