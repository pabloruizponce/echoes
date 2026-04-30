#!/usr/bin/env bash
set -Eeuo pipefail

CRON_BEGIN="# BEGIN ECHOES DAILY"
CRON_END="# END ECHOES DAILY"

CONFIG_DIR="${ECHOES_CONFIG_DIR:-}"
NOTEBOOKLM_HOME_VALUE="${NOTEBOOKLM_HOME:-}"

DRY_RUN=0
NO_PROMPT="${ECHOES_NO_PROMPT:-0}"
VERBOSE="${ECHOES_VERBOSE:-0}"
CHECKOUT_ACTION="ask"

SCRIPT_SOURCE="${BASH_SOURCE[0]:-}"
SCRIPT_DIR=""
if [[ -n "$SCRIPT_SOURCE" && -f "$SCRIPT_SOURCE" ]]; then
  SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$SCRIPT_SOURCE")" && pwd)"
fi

usage() {
  cat <<'EOF'
echoes uninstaller

Usage:
  ./uninstall.sh [options]

Options:
  --config-dir PATH        Private echoes runtime directory to remove (default: <checkout>/.echoes)
  --notebooklm-home PATH   NotebookLM home to remove (default: <runtime>/notebooklm)
  --remove-checkout        Delete this echoes checkout after uninstall finishes
  --keep-checkout          Keep this echoes checkout without prompting
  --no-prompt              Do not ask interactive questions
  --dry-run                Print actions without changing the system
  --verbose                Print extra diagnostic messages
  -h, --help               Show this help

Environment:
  ECHOES_CONFIG_DIR, NOTEBOOKLM_HOME,
  ECHOES_NO_PROMPT=1, ECHOES_VERBOSE=1
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

shell_quote() {
  local value="$1"
  printf "'"
  printf '%s' "$value" | sed "s/'/'\\\\''/g"
  printf "'"
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --config-dir) CONFIG_DIR="${2:?--config-dir requires a value}"; shift 2 ;;
      --config-dir=*) CONFIG_DIR="${1#*=}"; shift ;;
      --notebooklm-home) NOTEBOOKLM_HOME_VALUE="${2:?--notebooklm-home requires a value}"; shift 2 ;;
      --notebooklm-home=*) NOTEBOOKLM_HOME_VALUE="${1#*=}"; shift ;;
      --remove-checkout) CHECKOUT_ACTION="remove"; shift ;;
      --keep-checkout) CHECKOUT_ACTION="keep"; shift ;;
      --no-prompt) NO_PROMPT=1; shift ;;
      --dry-run) DRY_RUN=1; shift ;;
      --verbose) VERBOSE=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) die "Unknown option: $1" ;;
    esac
  done
}

resolve_runtime_paths() {
  if [[ -z "$CONFIG_DIR" ]]; then
    CONFIG_DIR="$SCRIPT_DIR/.echoes"
  fi
  if [[ -z "$NOTEBOOKLM_HOME_VALUE" ]]; then
    NOTEBOOKLM_HOME_VALUE="$CONFIG_DIR/notebooklm"
  fi
}

print_banner() {
  cat <<'EOF'

echoes Uninstaller
--------------------------
This resets echoes private state so you can rerun the installer on a
clean machine profile without removing global tools or your Codex login.

EOF
}

remove_managed_cron_block() {
  awk -v begin="$CRON_BEGIN" -v end="$CRON_END" '
    $0 == begin {skip=1; next}
    $0 == end {skip=0; next}
    !skip {print}
  '
}

remove_path_tree() {
  local label="$1"
  local target="$2"
  if [[ -z "$target" || "$target" == "/" ]]; then
    warn "Refusing to remove unsafe $label path: $target"
    return 0
  fi
  if [[ ! -e "$target" ]]; then
    info "$label not found at $target."
    return 0
  fi
  info "Removing $label at $target."
  run_cmd rm -rf -- "$target"
}

remove_cron_block() {
  if ! command -v crontab >/dev/null 2>&1; then
    warn "crontab command was not found; skipping echoes cron cleanup."
    return 0
  fi

  local existing cleaned
  existing="$(crontab -l 2>/dev/null || true)"
  cleaned="$(printf '%s\n' "$existing" | remove_managed_cron_block)"
  if [[ "$cleaned" == "$existing" ]]; then
    info "No managed echoes cron block was found."
    return 0
  fi

  if [[ "$DRY_RUN" == "1" ]]; then
    info "Would remove the managed echoes cron block."
    printf '[dry-run] skipped\n'
    return 0
  fi

  if [[ -n "$cleaned" ]]; then
    printf '%s\n' "$cleaned" | crontab -
  else
    crontab -r
  fi
  info "Removed the managed echoes cron block."
}

remove_chrome_mcp() {
  if ! command -v codex >/dev/null 2>&1; then
    warn "Codex CLI was not found; skipping chrome-devtools MCP cleanup."
    return 0
  fi
  if ! codex mcp get chrome-devtools >/dev/null 2>&1; then
    info "chrome-devtools MCP entry is not configured."
    return 0
  fi
  info "Removing chrome-devtools MCP entry from Codex."
  run_cmd codex mcp remove chrome-devtools
}

should_remove_checkout() {
  case "$CHECKOUT_ACTION" in
    remove) return 0 ;;
    keep) return 1 ;;
    ask)
      if ! prompt_allowed; then
        return 1
      fi
      ask_yes_no "Remove this echoes checkout too?" "n"
      return
      ;;
    *)
      die "Unsupported checkout action: $CHECKOUT_ACTION"
      ;;
  esac
}

schedule_checkout_removal() {
  local target="$1"
  if [[ -z "$target" || "$target" == "/" ]]; then
    warn "Refusing to remove unsafe checkout path: $target"
    return 0
  fi
  if [[ ! -e "$target" ]]; then
    info "Checkout path not found at $target."
    return 0
  fi

  local command
  command="sleep 1; rm -rf -- $(shell_quote "$target")"
  if [[ "$DRY_RUN" == "1" ]]; then
    info "cd / && nohup bash -c $(shell_quote "$command") >/dev/null 2>&1 &"
    printf '[dry-run] skipped\n'
    return 0
  fi

  cd /
  nohup bash -c "$command" >/dev/null 2>&1 &
  info "Scheduled checkout removal for $target."
}

main() {
  parse_args "$@"
  resolve_runtime_paths
  print_banner

  info "echoes config: $CONFIG_DIR"
  info "NotebookLM home:      $NOTEBOOKLM_HOME_VALUE"
  debug "checkout_dir=$SCRIPT_DIR"

  remove_path_tree "echoes private state" "$CONFIG_DIR"
  remove_path_tree "NotebookLM auth state" "$NOTEBOOKLM_HOME_VALUE"
  remove_cron_block
  remove_chrome_mcp

  if should_remove_checkout; then
    schedule_checkout_removal "$SCRIPT_DIR"
  else
    info "Keeping the echoes checkout at $SCRIPT_DIR."
  fi

  cat <<EOF

echoes uninstall finished.

Removed private echoes state, NotebookLM state, the managed cron block,
and the chrome-devtools MCP entry when present.
Kept global tools such as codex, uv, Node, Chrome, and your Codex login.
EOF
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
