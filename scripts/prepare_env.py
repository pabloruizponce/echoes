#!/usr/bin/env python3
"""Bootstrap the echoes environment and run prepare-stage checks."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import requests

from check_scholar_inbox_auth import DEFAULT_DIGEST_URL as SCHOLAR_DEFAULT_DIGEST_URL
from check_scholar_inbox_auth import SAVED_REQUEST_HEADERS_KEY
from check_scholar_inbox_auth import cookie_from_headers
from check_scholar_inbox_auth import parse_request_headers_input
from check_scholar_inbox_auth import request_headers_json
from check_scholar_inbox_auth import validate as validate_scholar_session
from fetch_digest import main as fetch_digest_main


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIGEST_URL = SCHOLAR_DEFAULT_DIGEST_URL
DEFAULT_CHROME_DEBUGGING_URL = "chrome://inspect/#remote-debugging"
DEFAULT_CHROME_MCP_ATTACH = "http://127.0.0.1:9222"
DEFAULT_TELEGRAM_API_BASE_URL = "https://api.telegram.org/bot"
DEFAULT_TELEGRAM_DISCOVERY_TIMEOUT = 90.0
DEFAULT_TELEGRAM_DISCOVERY_POLL_TIMEOUT = 10.0
PROFILE_ENV_KEY = "ECHOES_PROFILE"
PROFILE_FILE_NAME = "PROFILE.md"


def config_dir() -> Path:
    override = os.environ.get("ECHOES_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return ROOT / ".echoes"


def notebooklm_home() -> Path:
    override = os.environ.get("NOTEBOOKLM_HOME")
    if override:
        return Path(override).expanduser()
    return config_dir() / "notebooklm"


def ensure_notebooklm_home() -> Path:
    home = notebooklm_home()
    os.environ.setdefault("NOTEBOOKLM_HOME", str(home))
    return home


def credentials_path() -> Path:
    return config_dir() / "credentials.env"


def default_profile_path() -> Path:
    override = os.environ.get(PROFILE_ENV_KEY)
    if override:
        return Path(override).expanduser()
    return config_dir() / PROFILE_FILE_NAME


def is_template_profile(profile_text: str) -> bool:
    return "Status: Template" in profile_text


def run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    print(f"$ {' '.join(command)}")
    return subprocess.run(
        command,
        cwd=cwd or ROOT,
        text=True,
    )


def doctor_check(
    name: str,
    status: str,
    message: str,
    *,
    required: bool = True,
    details: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": name,
        "status": status,
        "required": required,
        "message": message,
    }
    if details:
        payload["details"] = details
    return payload


def run_json_doctor_command(command: list[str], *, timeout: float) -> tuple[int, dict[str, object] | None, str]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    output = completed.stdout.strip() or completed.stderr.strip()
    parsed: dict[str, object] | None = None
    if output:
        try:
            candidate = json.loads(output)
        except json.JSONDecodeError:
            candidate = None
        if isinstance(candidate, dict):
            parsed = candidate
    return completed.returncode, parsed, output


def require_uv() -> str:
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("uv is required but was not found in PATH.")
    return uv


def uv_run_command(*args: str) -> list[str]:
    return [require_uv(), "run", *args]


def ensure_supported_python() -> None:
    if sys.version_info < (3, 10):
        raise SystemExit(
            f"Python 3.10+ is required, found {sys.version.split()[0]}."
        )


def check_python() -> dict[str, object]:
    version = sys.version.split()[0]
    if sys.version_info >= (3, 10):
        return doctor_check("python", "ok", f"Python {version} is supported.", details={"version": version})
    return doctor_check("python", "error", f"Python 3.10+ is required, found {version}.", details={"version": version})


def check_uv() -> dict[str, object]:
    uv = shutil.which("uv")
    if uv:
        return doctor_check("uv", "ok", "uv is available.", details={"path": uv})
    return doctor_check("uv", "error", "uv is required but was not found in PATH.")


def check_runtime_imports() -> dict[str, object]:
    modules = ("requests", "notebooklm", "telegram")
    missing: list[str] = []
    for module in modules:
        try:
            __import__(module)
        except Exception:  # noqa: BLE001 - doctor reports dependency health, not stack traces.
            missing.append(module)
    if missing:
        return doctor_check(
            "runtime_dependencies",
            "error",
            "Missing runtime Python dependencies. Run `uv sync`.",
            details={"missing_modules": missing},
        )
    return doctor_check("runtime_dependencies", "ok", "Runtime Python dependencies import successfully.")


def check_profile() -> dict[str, object]:
    path = default_profile_path()
    details = {"path": str(path)}
    if not path.exists():
        return doctor_check(
            "researcher_profile",
            "error",
            f"Active researcher profile is missing at {path}.",
            details=details,
        )
    profile_text = path.read_text()
    if is_template_profile(profile_text):
        return doctor_check(
            "researcher_profile",
            "error",
            f"Active researcher profile is still a template at {path}.",
            details=details,
        )
    return doctor_check("researcher_profile", "ok", "Active researcher profile is filled.", details=details)


def check_telegram_config() -> dict[str, object]:
    values = parse_env_file(credentials_path())
    missing = []
    if not (os.environ.get("TELEGRAM_BOT_TOKEN") or values.get("TELEGRAM_BOT_TOKEN")):
        missing.append("TELEGRAM_BOT_TOKEN")
    if not (os.environ.get("TELEGRAM_CHAT_ID") or values.get("TELEGRAM_CHAT_ID")):
        missing.append("TELEGRAM_CHAT_ID")
    details = {
        "credentials_path": str(credentials_path()),
        "missing": missing,
        "transport_overrides_present": {
            "TELEGRAM_PROXY_URL": bool(os.environ.get("TELEGRAM_PROXY_URL") or values.get("TELEGRAM_PROXY_URL")),
            "TELEGRAM_API_BASE_URL": bool(os.environ.get("TELEGRAM_API_BASE_URL") or values.get("TELEGRAM_API_BASE_URL")),
            "TELEGRAM_API_IP": bool(os.environ.get("TELEGRAM_API_IP") or values.get("TELEGRAM_API_IP")),
        },
    }
    if missing:
        return doctor_check(
            "telegram_config",
            "error",
            "Telegram delivery configuration is incomplete.",
            details=details,
        )
    return doctor_check("telegram_config", "ok", "Telegram delivery configuration is present.", details=details)


def check_ghostscript() -> dict[str, object]:
    gs = shutil.which("gs")
    if gs:
        return doctor_check(
            "ghostscript",
            "ok",
            "Ghostscript is available for optional PDF compression.",
            required=False,
            details={"path": gs},
        )
    return doctor_check(
        "ghostscript",
        "warn",
        "Ghostscript is not installed; original PDFs will be uploaded if compression is unavailable.",
        required=False,
    )


def check_ffmpeg() -> dict[str, object]:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return doctor_check(
            "ffmpeg",
            "ok",
            "ffmpeg is available for Telegram voice-note audio conversion.",
            details={"path": ffmpeg},
        )
    return doctor_check(
        "ffmpeg",
        "error",
        "ffmpeg is required to convert generated MP3 audio into Telegram voice notes.",
    )


def check_auth_script(script_name: str, check_name: str, ok_message: str, *, timeout: float) -> dict[str, object]:
    uv = shutil.which("uv")
    if not uv:
        return doctor_check(check_name, "error", "Cannot run auth check because uv is missing.")
    if script_name == "check_notebooklm_auth.py":
        ensure_notebooklm_home()
    command = [uv, "run", "python", str(ROOT / "scripts" / script_name), "--json"]
    try:
        returncode, payload, output = run_json_doctor_command(command, timeout=timeout)
    except subprocess.TimeoutExpired:
        return doctor_check(check_name, "error", f"{check_name} timed out after {timeout:.0f} seconds.")
    except Exception as exc:  # noqa: BLE001
        return doctor_check(check_name, "error", f"{check_name} could not run: {exc}")

    if returncode == 0:
        return doctor_check(check_name, "ok", ok_message)
    message = ""
    if payload:
        message = str(payload.get("message") or payload.get("error") or "")
    if not message:
        message = output or f"{check_name} failed."
    return doctor_check(check_name, "error", message)


def ensure_pyproject() -> None:
    if not (ROOT / "pyproject.toml").exists():
        raise SystemExit(f"pyproject.toml not found at {ROOT}.")


def sync_environment() -> None:
    uv = require_uv()
    ensure_supported_python()
    ensure_pyproject()

    result = run([uv, "sync"])
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def ensure_playwright_chromium() -> None:
    print("Ensuring Playwright Chromium is available.")
    result = run(uv_run_command("python", "-m", "playwright", "install", "chromium"))
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def parse_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def write_env_file(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(f"{key}={value}" for key, value in sorted(values.items())) + "\n"
    path.write_text(content)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def resolve_secret_input(
    *,
    value: str | None,
    value_stdin: bool,
    label: str,
) -> str:
    if value_stdin:
        resolved = sys.stdin.read().strip()
    else:
        resolved = (value or "").strip()
    if not resolved:
        raise SystemExit(f"{label} cannot be empty.")
    return resolved


def telegram_api_base_url(values: dict[str, str]) -> str:
    return (
        os.environ.get("TELEGRAM_API_BASE_URL")
        or values.get("TELEGRAM_API_BASE_URL")
        or DEFAULT_TELEGRAM_API_BASE_URL
    ).rstrip("/")


def telegram_proxy_url(values: dict[str, str]) -> str:
    return (
        os.environ.get("TELEGRAM_PROXY_URL")
        or values.get("TELEGRAM_PROXY_URL")
        or ""
    ).strip()


def telegram_api_error_message(method: str, payload: object, status_code: int) -> str:
    if not isinstance(payload, dict):
        return f"Telegram {method} failed with HTTP {status_code}."

    error_code = payload.get("error_code") or status_code
    description = str(payload.get("description") or "").strip()
    lowered = description.lower()

    if error_code == 401 or "unauthorized" in lowered:
        return f"Telegram {method} failed: invalid bot token or bot API access denied."
    if error_code == 404 or lowered == "not found":
        return (
            f"Telegram {method} failed: the bot token is invalid, incomplete, or prefixed incorrectly, "
            "or TELEGRAM_API_BASE_URL does not point to a valid Telegram Bot API endpoint."
        )
    if error_code == 409 or "webhook" in lowered:
        return f"Telegram {method} failed: a webhook is active for this bot. Remove the webhook and retry."
    if description:
        return f"Telegram {method} failed: {description}"
    return f"Telegram {method} failed with HTTP {status_code}."


def telegram_api_get(
    token: str,
    method: str,
    *,
    params: dict[str, object],
    request_timeout: float,
    values: dict[str, str],
) -> dict[str, object]:
    url = f"{telegram_api_base_url(values)}{token}/{method}"
    kwargs: dict[str, object] = {
        "params": params,
        "timeout": request_timeout,
    }
    proxy_url = telegram_proxy_url(values)
    if proxy_url:
        kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}

    try:
        response = requests.get(url, **kwargs)
    except requests.RequestException as exc:
        raise SystemExit(f"Telegram {method} failed: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise SystemExit(
            f"Telegram {method} failed: the Bot API returned a non-JSON response."
        ) from exc

    if response.status_code >= 400:
        raise SystemExit(telegram_api_error_message(method, payload, response.status_code))
    if not isinstance(payload, dict):
        raise SystemExit(f"Telegram {method} failed: unexpected response shape.")
    if not payload.get("ok"):
        raise SystemExit(telegram_api_error_message(method, payload, response.status_code))
    return payload


def telegram_updates(
    token: str,
    *,
    values: dict[str, str],
    offset: int | None,
    poll_timeout: float,
) -> list[dict[str, object]]:
    timeout_value = max(0, int(poll_timeout))
    params: dict[str, object] = {
        "allowed_updates": json.dumps(["message"]),
        "timeout": timeout_value,
        "limit": 100,
    }
    if offset is not None:
        params["offset"] = offset

    payload = telegram_api_get(
        token,
        "getUpdates",
        params=params,
        request_timeout=max(10.0, poll_timeout + 10.0),
        values=values,
    )
    result = payload.get("result")
    if not isinstance(result, list):
        raise SystemExit("Telegram getUpdates failed: unexpected result shape.")
    return [item for item in result if isinstance(item, dict)]


def telegram_update_id(update: dict[str, object]) -> int | None:
    value = update.get("update_id")
    if isinstance(value, int):
        return value
    return None


def max_telegram_update_id(updates: list[dict[str, object]]) -> int | None:
    update_ids = [value for item in updates if (value := telegram_update_id(item)) is not None]
    if not update_ids:
        return None
    return max(update_ids)


def matching_private_chat_id(update: dict[str, object], code: str) -> str | None:
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    if message.get("text") != code:
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict) or chat.get("type") != "private":
        return None
    chat_id = chat.get("id")
    if isinstance(chat_id, int):
        return str(chat_id)
    if isinstance(chat_id, str) and chat_id.strip():
        return chat_id.strip()
    return None


def resolve_telegram_token(args: argparse.Namespace, values: dict[str, str]) -> str:
    if args.token or args.token_stdin:
        return resolve_secret_input(
            value=args.token,
            value_stdin=args.token_stdin,
            label="Telegram bot token",
        )

    token = (
        os.environ.get("TELEGRAM_BOT_TOKEN")
        or values.get("TELEGRAM_BOT_TOKEN")
        or ""
    ).strip()
    if not token:
        raise SystemExit(
            "Telegram bot token is missing. Pass --token/--token-stdin or save TELEGRAM_BOT_TOKEN first."
        )
    return token


def discover_telegram_chat_id(
    *,
    token: str,
    code: str,
    timeout: float,
    poll_timeout: float,
    values: dict[str, str],
    emit_progress: bool,
) -> dict[str, object]:
    baseline_updates = telegram_updates(token, values=values, offset=None, poll_timeout=0.0)
    next_offset = None
    baseline_id = max_telegram_update_id(baseline_updates)
    if baseline_id is not None:
        next_offset = baseline_id + 1

    if emit_progress:
        print("Send this exact code to your Telegram bot in a direct chat:")
        print(code)
        print("")
        print(f"Waiting up to {int(timeout)} seconds for a matching message...")

    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SystemExit(
                "Telegram chat ID discovery timed out. Send the code to the bot in a direct chat and retry."
            )

        wait_timeout = min(poll_timeout, max(1.0, remaining))
        updates = telegram_updates(
            token,
            values=values,
            offset=next_offset,
            poll_timeout=wait_timeout,
        )
        for update in updates:
            if chat_id := matching_private_chat_id(update, code):
                return {
                    "ok": True,
                    "code": code,
                    "chat_id": chat_id,
                }

        latest_id = max_telegram_update_id(updates)
        if latest_id is not None:
            next_offset = latest_id + 1


def cmd_setup(_: argparse.Namespace) -> int:
    sync_environment()
    ensure_playwright_chromium()
    print("Environment setup complete.")
    return 0


def cmd_prepare_checklist(_: argparse.Namespace) -> int:
    print("echoes prepare checklist")
    print("")
    print(f"1. Open {DEFAULT_CHROME_DEBUGGING_URL} and enable remote debugging.")
    print(f"2. Open {DEFAULT_DIGEST_URL} in that same Chrome session and log in.")
    print("3. Make sure Chrome MCP is configured to attach to your real Chrome.")
    print(f"   Expected attach mode: --browser-url={DEFAULT_CHROME_MCP_ATTACH} or --autoConnect")
    print("4. Reply when both are done.")
    return 0


def cmd_save_scholar_session(args: argparse.Namespace) -> int:
    value = resolve_secret_input(
        value=args.value,
        value_stdin=args.value_stdin,
        label="Scholar Inbox session value",
    )

    target = credentials_path()

    values = parse_env_file(target)
    values["SCHOLAR_INBOX_DIGEST_URL"] = args.digest_url
    values["SCHOLAR_INBOX_SESSION"] = value
    write_env_file(target, values)
    print("Scholar Inbox credential saved.")
    return 0


def cmd_save_validated_scholar_session(args: argparse.Namespace) -> int:
    value = resolve_secret_input(
        value=args.value,
        value_stdin=args.value_stdin,
        label="Scholar Inbox session value",
    )

    try:
        result = validate_scholar_session(value, args.digest_url, args.timeout)
    except Exception as exc:
        raise SystemExit(f"Scholar Inbox validation failed: {exc}") from exc

    if not result["ok"]:
        if result.get("redirected_to_login"):
            raise SystemExit("Scholar Inbox validation failed: digest request redirected to login.")
        raise SystemExit(
            "Scholar Inbox validation failed: "
            f"{result['status_code']} from {result['final_url']}"
        )

    target = credentials_path()
    values = parse_env_file(target)
    values["SCHOLAR_INBOX_DIGEST_URL"] = args.digest_url
    values["SCHOLAR_INBOX_SESSION"] = value
    write_env_file(target, values)
    print("Scholar Inbox credential validated and saved.")
    return 0


def cmd_save_validated_scholar_headers(args: argparse.Namespace) -> int:
    raw_headers = resolve_secret_input(
        value=args.headers,
        value_stdin=args.headers_stdin,
        label="Scholar Inbox request headers",
    )
    try:
        headers = parse_request_headers_input(raw_headers)
    except ValueError as exc:
        raise SystemExit(f"Scholar Inbox request headers were invalid: {exc}") from exc

    cookie = cookie_from_headers(headers)
    if not cookie:
        raise SystemExit("Scholar Inbox request headers did not include a Cookie header.")

    headers_value = request_headers_json(headers)
    try:
        result = validate_scholar_session(
            cookie,
            args.digest_url,
            args.timeout,
            request_headers_value=headers_value,
        )
    except Exception as exc:
        raise SystemExit(f"Scholar Inbox validation failed: {exc}") from exc

    if not result["ok"]:
        raise SystemExit(
            "Scholar Inbox validation failed: "
            f"{result['status_code']} from {result['final_url']}"
        )

    target = credentials_path()
    values = parse_env_file(target)
    values["SCHOLAR_INBOX_DIGEST_URL"] = args.digest_url
    values["SCHOLAR_INBOX_SESSION"] = cookie
    values[SAVED_REQUEST_HEADERS_KEY] = headers_value
    write_env_file(target, values)
    print("Scholar Inbox request headers validated and saved.")
    return 0


def cmd_save_telegram_config(args: argparse.Namespace) -> int:
    token = resolve_secret_input(
        value=args.token,
        value_stdin=args.token_stdin,
        label="Telegram bot token",
    )
    chat_id = resolve_secret_input(
        value=args.chat_id,
        value_stdin=args.chat_id_stdin,
        label="Telegram chat ID",
    )

    target = credentials_path()
    values = parse_env_file(target)
    values["TELEGRAM_BOT_TOKEN"] = token
    values["TELEGRAM_CHAT_ID"] = chat_id
    write_env_file(target, values)
    print("Telegram bot configuration saved.")
    return 0


def cmd_save_telegram_chat_id(args: argparse.Namespace) -> int:
    chat_id = resolve_secret_input(
        value=args.chat_id,
        value_stdin=args.chat_id_stdin,
        label="Telegram chat ID",
    )

    target = credentials_path()
    values = parse_env_file(target)
    values["TELEGRAM_CHAT_ID"] = chat_id
    write_env_file(target, values)
    print("Telegram chat ID saved.")
    return 0


def cmd_save_telegram_transport(args: argparse.Namespace) -> int:
    target = credentials_path()
    values = parse_env_file(target)

    if args.api_ip:
        values["TELEGRAM_API_IP"] = args.api_ip.strip()
        values["TELEGRAM_API_HOST_HEADER"] = (args.api_host_header or "api.telegram.org").strip()
    elif args.api_host_header:
        values["TELEGRAM_API_HOST_HEADER"] = args.api_host_header.strip()
    if args.proxy_url:
        values["TELEGRAM_PROXY_URL"] = args.proxy_url.strip()
    if args.api_base_url:
        values["TELEGRAM_API_BASE_URL"] = args.api_base_url.strip()

    if not any(args_value for args_value in (args.api_ip, args.api_host_header, args.proxy_url, args.api_base_url)):
        raise SystemExit("At least one Telegram transport override must be provided.")

    write_env_file(target, values)
    print("Telegram transport configuration saved.")
    return 0


def cmd_discover_telegram_chat_id(args: argparse.Namespace) -> int:
    target = credentials_path()
    values = parse_env_file(target)
    token = resolve_telegram_token(args, values)
    code = (args.code or f"echoes-{secrets.token_hex(4)}").strip()
    if not code:
        raise SystemExit("Telegram discovery code cannot be empty.")

    try:
        result = discover_telegram_chat_id(
            token=token,
            code=code,
            timeout=args.timeout,
            poll_timeout=args.poll_timeout,
            values=values,
            emit_progress=not args.json,
        )
    except SystemExit as exc:
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "code": code,
                        "message": str(exc),
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 1
        raise

    values["TELEGRAM_BOT_TOKEN"] = token
    values["TELEGRAM_CHAT_ID"] = str(result["chat_id"])
    write_env_file(target, values)

    payload = {
        "ok": True,
        "code": code,
        "message": "Telegram chat ID saved from a matching direct message.",
        "credentials_path": str(target),
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print("Telegram chat ID saved from a matching direct message.")
    return 0


def scholar_inbox_open_command(digest_url: str, *, app: str | None = None) -> list[str] | None:
    if sys.platform == "darwin":
        opener = shutil.which("open")
        if not opener:
            return None
        if app:
            return [opener, "-a", app, digest_url]
        return [opener, digest_url]

    if sys.platform.startswith("linux"):
        opener = shutil.which("xdg-open")
        if not opener:
            return None
        return [opener, digest_url]

    return None


def cmd_open_scholar_inbox(args: argparse.Namespace) -> int:
    target = credentials_path()
    values = parse_env_file(target)
    digest_url = values.get("SCHOLAR_INBOX_DIGEST_URL", DEFAULT_DIGEST_URL)
    command = scholar_inbox_open_command(digest_url, app=args.app)
    if not command:
        print("No supported browser opener was found for this platform.")
        print(f"Open this URL manually: {digest_url}")
        return 1
    print(f"$ {' '.join(command)}")
    return subprocess.run(command, cwd=ROOT).returncode


def launch_notebooklm_login_in_terminal() -> int:
    storage = ensure_notebooklm_home()
    command = " ".join(
        shlex.quote(part)
        for part in uv_run_command("notebooklm", "--storage", str(storage), "login")
    )
    command = f"cd {shlex.quote(str(ROOT))} && {command}"
    script_lines = [
        'tell application "Terminal"',
        f'  do script "{command}"',
        "  activate",
        "end tell",
    ]
    for line in script_lines:
        print(line)
    result = subprocess.run(
        ["osascript", *sum((["-e", line] for line in script_lines), [])],
        cwd=ROOT,
    )
    if result.returncode == 0:
        print("Opened Terminal.app for interactive NotebookLM login.")
    return result.returncode


def cmd_notebooklm_login(_: argparse.Namespace) -> int:
    storage = ensure_notebooklm_home()
    if sys.platform == "darwin":
        return launch_notebooklm_login_in_terminal()

    command = uv_run_command("notebooklm", "--storage", str(storage), "login")
    print(f"$ {' '.join(command)}")
    return subprocess.run(command, cwd=ROOT).returncode


def cmd_smoke_test(args: argparse.Namespace) -> int:
    failures = 0
    if not args.skip_scholar:
        scholar = subprocess.run(
            uv_run_command("python", str(ROOT / "scripts" / "check_scholar_inbox_auth.py")),
            cwd=ROOT,
        )
        if scholar.returncode != 0:
            failures += 1

    if not args.skip_notebooklm:
        ensure_notebooklm_home()
        notebooklm = subprocess.run(
            uv_run_command("python", str(ROOT / "scripts" / "check_notebooklm_auth.py")),
            cwd=ROOT,
        )
        if notebooklm.returncode != 0:
            failures += 1

    if failures:
        print(f"Smoke test finished with {failures} failing check(s).")
        return 1

    print("Smoke test passed.")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    ensure_notebooklm_home()
    checks: list[dict[str, object]] = [
        check_python(),
        check_uv(),
        check_runtime_imports(),
        check_profile(),
        check_telegram_config(),
        check_ghostscript(),
        check_ffmpeg(),
    ]

    if args.skip_auth:
        checks.append(
            doctor_check(
                "scholar_inbox_auth",
                "warn",
                "Scholar Inbox auth check skipped by --skip-auth.",
                required=False,
            )
        )
        checks.append(
            doctor_check(
                "notebooklm_auth",
                "warn",
                "NotebookLM auth check skipped by --skip-auth.",
                required=False,
            )
        )
    else:
        checks.append(
            check_auth_script(
                "check_scholar_inbox_auth.py",
                "scholar_inbox_auth",
                "Scholar Inbox saved authentication works.",
                timeout=args.timeout,
            )
        )
        checks.append(
            check_auth_script(
                "check_notebooklm_auth.py",
                "notebooklm_auth",
                "NotebookLM saved authentication works.",
                timeout=args.timeout,
            )
        )

    ok = not any(item["required"] and item["status"] == "error" for item in checks)
    payload = {
        "ok": ok,
        "config_dir": str(config_dir()),
        "notebooklm_home": str(notebooklm_home()),
        "profile_path": str(default_profile_path()),
        "checks": checks,
    }

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        for item in checks:
            print(f"[{item['status']}] {item['name']}: {item['message']}")
    return 0 if ok else 1


def cmd_fetch_digest(args: argparse.Namespace) -> int:
    argv: list[str] = []
    if args.date:
        argv.extend(["--date", args.date])
    if getattr(args, "yesterday", False):
        argv.append("--yesterday")
    if args.output:
        argv.extend(["--output", args.output])
    if args.config_dir:
        argv.extend(["--config-dir", args.config_dir])
    if args.digest_url:
        argv.extend(["--digest-url", args.digest_url])
    if args.api_url:
        argv.extend(["--api-url", args.api_url])
    if args.timeout is not None:
        argv.extend(["--timeout", str(args.timeout)])
    if args.json:
        argv.append("--json")
    return fetch_digest_main(argv)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    checklist = subparsers.add_parser(
        "prepare-checklist",
        help="Print the Chrome-first checklist for echoes prepare",
    )
    checklist.set_defaults(func=cmd_prepare_checklist)

    setup = subparsers.add_parser("setup", help="Create or update the local uv environment")
    setup.set_defaults(func=cmd_setup)

    save = subparsers.add_parser(
        "save-scholar-session",
        help="Persist the Scholar Inbox session value without interactive confirmation",
    )
    save.add_argument("--value", help="Exact cookie string or session value")
    save.add_argument(
        "--value-stdin",
        action="store_true",
        help="Read the Scholar Inbox session value from standard input",
    )
    save.add_argument(
        "--digest-url",
        default=DEFAULT_DIGEST_URL,
        help="Digest URL to validate later",
    )
    save.set_defaults(func=cmd_save_scholar_session)

    save_validated = subparsers.add_parser(
        "save-validated-scholar-session",
        help="Validate and persist the Scholar Inbox session value in one step",
    )
    save_validated.add_argument("--value", help="Exact cookie string or session value")
    save_validated.add_argument(
        "--value-stdin",
        action="store_true",
        help="Read the Scholar Inbox session value from standard input",
    )
    save_validated.add_argument(
        "--digest-url",
        default=DEFAULT_DIGEST_URL,
        help="Digest URL to validate later",
    )
    save_validated.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="Validation timeout in seconds",
    )
    save_validated.set_defaults(func=cmd_save_validated_scholar_session)

    save_validated_headers = subparsers.add_parser(
        "save-validated-scholar-headers",
        help="Validate and persist Scholar Inbox request headers captured from Chrome MCP",
    )
    save_validated_headers.add_argument("--headers", help="Request headers JSON or raw HTTP headers")
    save_validated_headers.add_argument(
        "--headers-stdin",
        action="store_true",
        help="Read request headers JSON or raw HTTP headers from standard input",
    )
    save_validated_headers.add_argument(
        "--digest-url",
        default=DEFAULT_DIGEST_URL,
        help="Digest URL to validate later",
    )
    save_validated_headers.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="Validation timeout in seconds",
    )
    save_validated_headers.set_defaults(func=cmd_save_validated_scholar_headers)

    save_telegram = subparsers.add_parser(
        "save-telegram-config",
        help="Save TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to the private credentials file",
    )
    save_telegram.add_argument("--token", help="Telegram bot token")
    save_telegram.add_argument(
        "--token-stdin",
        action="store_true",
        help="Read TELEGRAM_BOT_TOKEN from standard input",
    )
    save_telegram.add_argument("--chat-id", help="Telegram chat ID")
    save_telegram.add_argument(
        "--chat-id-stdin",
        action="store_true",
        help="Read TELEGRAM_CHAT_ID from standard input",
    )
    save_telegram.set_defaults(func=cmd_save_telegram_config)

    save_telegram_chat = subparsers.add_parser(
        "save-telegram-chat-id",
        help="Save TELEGRAM_CHAT_ID to the private credentials file",
    )
    save_telegram_chat.add_argument("--chat-id", help="Telegram chat ID")
    save_telegram_chat.add_argument(
        "--chat-id-stdin",
        action="store_true",
        help="Read TELEGRAM_CHAT_ID from standard input",
    )
    save_telegram_chat.set_defaults(func=cmd_save_telegram_chat_id)

    save_telegram_transport = subparsers.add_parser(
        "save-telegram-transport",
        help="Save optional Telegram transport overrides for blocked networks",
    )
    save_telegram_transport.add_argument("--api-ip", help="Official Telegram API IP for SNI-blocked networks")
    save_telegram_transport.add_argument(
        "--api-host-header",
        help="Host name to validate and send in the HTTP Host header when --api-ip is used",
    )
    save_telegram_transport.add_argument("--proxy-url", help="Optional Telegram proxy URL")
    save_telegram_transport.add_argument("--api-base-url", help="Optional custom Bot API base URL ending in /bot")
    save_telegram_transport.set_defaults(func=cmd_save_telegram_transport)

    discover_telegram_chat = subparsers.add_parser(
        "discover-telegram-chat-id",
        help="Generate a random code, poll Telegram updates, and save TELEGRAM_CHAT_ID from a matching direct message",
    )
    discover_telegram_chat.add_argument("--token", help="Telegram bot token")
    discover_telegram_chat.add_argument(
        "--token-stdin",
        action="store_true",
        help="Read TELEGRAM_BOT_TOKEN from standard input",
    )
    discover_telegram_chat.add_argument(
        "--code",
        help="Exact code to look for instead of generating a random one",
    )
    discover_telegram_chat.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TELEGRAM_DISCOVERY_TIMEOUT,
        help="Overall discovery timeout in seconds",
    )
    discover_telegram_chat.add_argument(
        "--poll-timeout",
        type=float,
        default=DEFAULT_TELEGRAM_DISCOVERY_POLL_TIMEOUT,
        help="Telegram long-poll timeout in seconds for each getUpdates request",
    )
    discover_telegram_chat.add_argument("--json", action="store_true")
    discover_telegram_chat.set_defaults(func=cmd_discover_telegram_chat_id)

    open_digest = subparsers.add_parser(
        "open-scholar-inbox",
        help="Open the Scholar Inbox digest in a normal browser for Cloudflare or manual login",
    )
    open_digest.add_argument(
        "--app",
        help="Optional macOS application name, for example 'Google Chrome'",
    )
    open_digest.set_defaults(func=cmd_open_scholar_inbox)

    notebooklm_login = subparsers.add_parser(
        "notebooklm-login",
        help="Launch notebooklm login using the uv-managed project environment",
    )
    notebooklm_login.set_defaults(func=cmd_notebooklm_login)

    smoke = subparsers.add_parser("smoke-test", help="Run Scholar Inbox and NotebookLM checks")
    smoke.add_argument("--skip-scholar", action="store_true", help="Skip the Scholar Inbox check")
    smoke.add_argument(
        "--skip-notebooklm",
        action="store_true",
        help="Skip the NotebookLM check",
    )
    smoke.set_defaults(func=cmd_smoke_test)

    doctor = subparsers.add_parser(
        "doctor",
        help="Check local setup readiness without exposing saved secrets",
    )
    doctor.add_argument("--json", action="store_true", help="Print machine-readable status")
    doctor.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Timeout in seconds for each auth check",
    )
    doctor.add_argument(
        "--skip-auth",
        action="store_true",
        help="Skip network-backed Scholar Inbox and NotebookLM auth checks",
    )
    doctor.set_defaults(func=cmd_doctor)

    fetch = subparsers.add_parser(
        "fetch-digest",
        help="Fetch the current Scholar Inbox digest and save a JSON snapshot",
    )
    fetch_date = fetch.add_mutually_exclusive_group()
    fetch_date.add_argument("--date", help="Digest date in YYYY-MM-DD format")
    fetch_date.add_argument(
        "--yesterday",
        action="store_true",
        help="Fetch the previous Europe/Madrid calendar day",
    )
    fetch.add_argument("--output", help="Override the output JSON path")
    fetch.add_argument("--config-dir", help="Override ECHOES_CONFIG_DIR")
    fetch.add_argument("--digest-url", help="Override SCHOLAR_INBOX_DIGEST_URL")
    fetch.add_argument(
        "--api-url",
        help="Override the Scholar Inbox JSON API endpoint",
    )
    fetch.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="Digest fetch timeout in seconds",
    )
    fetch.add_argument("--json", action="store_true", help="Print machine-readable status")
    fetch.set_defaults(func=cmd_fetch_digest)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
