#!/usr/bin/env python3
"""Validate Scholar Inbox digest authentication."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from dns_fallback import request_with_dns_fallback


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIGEST_URL = "https://www.scholar-inbox.com/digest"
DEFAULT_API_URL = "https://api.scholar-inbox.com/api"
SAVED_REQUEST_HEADERS_KEY = "SCHOLAR_INBOX_REQUEST_HEADERS_JSON"
DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)
BLOCKED_SAVED_HEADER_NAMES = {
    "accept-encoding",
    "connection",
    "content-length",
    "host",
}
CANONICAL_HEADER_NAMES = {
    "accept": "Accept",
    "accept-language": "Accept-Language",
    "cookie": "Cookie",
    "origin": "Origin",
    "referer": "Referer",
    "sec-ch-ua": "Sec-CH-UA",
    "sec-ch-ua-mobile": "Sec-CH-UA-Mobile",
    "sec-ch-ua-platform": "Sec-CH-UA-Platform",
    "sec-fetch-dest": "Sec-Fetch-Dest",
    "sec-fetch-mode": "Sec-Fetch-Mode",
    "sec-fetch-site": "Sec-Fetch-Site",
    "user-agent": "User-Agent",
}


def config_dir(override: str | None) -> Path:
    if override:
        return Path(override).expanduser()
    env_override = os.environ.get("ECHOES_CONFIG_DIR")
    if env_override:
        return Path(env_override).expanduser()
    return ROOT / ".echoes"


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


def default_user_agent() -> str:
    return os.environ.get("ECHOES_USER_AGENT", "").strip() or DEFAULT_BROWSER_USER_AGENT


def origin_from_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return "https://www.scholar-inbox.com"
    return f"{parsed.scheme}://{parsed.netloc}"


def browser_default_headers(digest_url: str) -> dict[str, str]:
    return {
        "User-Agent": default_user_agent(),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
        "Referer": digest_url,
        "Origin": origin_from_url(digest_url),
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
    }


def canonical_header_name(name: str) -> str:
    lowered = name.strip().lower()
    return CANONICAL_HEADER_NAMES.get(lowered) or "-".join(part.capitalize() for part in lowered.split("-"))


def clean_request_headers(headers: dict[str, Any]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        name = str(raw_name or "").strip()
        if not name or name.startswith(":"):
            continue
        lowered = name.lower()
        if lowered in BLOCKED_SAVED_HEADER_NAMES:
            continue
        value = str(raw_value or "").strip()
        if not value:
            continue
        cleaned[canonical_header_name(name)] = value
    return cleaned


def request_headers_from_list(items: list[Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("key")
        value = item.get("value")
        if name is not None and value is not None:
            headers[str(name)] = str(value)
    return headers


def parse_request_headers_input(raw_value: str) -> dict[str, str]:
    raw = raw_value.strip()
    if not raw:
        raise ValueError("request headers input is empty")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        headers: dict[str, str] = {}
        for line in raw.splitlines():
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            headers[name.strip()] = value.strip()
        if not headers:
            raise ValueError("request headers input is neither JSON nor raw HTTP headers")
        return clean_request_headers(headers)

    candidate: Any = payload
    if isinstance(payload, dict):
        for key_path in (
            ("headers",),
            ("request", "headers"),
            ("requestHeaders",),
            ("request", "requestHeaders"),
        ):
            current: Any = payload
            for key in key_path:
                if not isinstance(current, dict) or key not in current:
                    current = None
                    break
                current = current[key]
            if isinstance(current, (dict, list)):
                candidate = current
                break

    if isinstance(candidate, list):
        candidate = request_headers_from_list(candidate)
    if not isinstance(candidate, dict):
        raise ValueError("request headers JSON must be an object or a list of header entries")
    return clean_request_headers(candidate)


def request_headers_json(headers: dict[str, str]) -> str:
    return json.dumps(clean_request_headers(headers), sort_keys=True, separators=(",", ":"))


def parse_saved_request_headers(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    return parse_request_headers_input(value)


def cookie_from_headers(headers: dict[str, str]) -> str:
    for key, value in headers.items():
        if key.lower() == "cookie" and value.strip():
            return value.strip()
    return ""


def build_scholar_inbox_headers(
    *,
    session_value: str | None,
    digest_url: str,
    request_headers_value: str | None = None,
) -> dict[str, str]:
    headers = browser_default_headers(digest_url)
    saved_headers = parse_saved_request_headers(request_headers_value)
    headers.update(saved_headers)
    if session_value:
        headers["Cookie"] = session_value
    if os.environ.get("ECHOES_USER_AGENT"):
        headers["User-Agent"] = default_user_agent()
    return headers


def login_like(url: str, body: str) -> bool:
    lowered_url = url.lower()
    lowered_body = body.lower()
    markers = ("login", "log in", "sign in", "sign-in")
    return any(marker in lowered_url or marker in lowered_body for marker in markers)


def validate_api(
    session_value: str,
    api_url: str,
    timeout: float,
    *,
    digest_url: str = DEFAULT_DIGEST_URL,
    request_headers_value: str | None = None,
) -> dict[str, object]:
    headers = build_scholar_inbox_headers(
        session_value=session_value,
        digest_url=digest_url,
        request_headers_value=request_headers_value,
    )
    response = request_with_dns_fallback(
        "GET",
        api_url,
        headers=headers,
        params={"p": 0},
        timeout=timeout,
    )
    ok = False
    api_success: object = None
    has_digest_df = False
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        api_success = payload.get("success")
        has_digest_df = isinstance(payload.get("digest_df"), list)
        ok = response.status_code < 400 and api_success is not False and has_digest_df
    return {
        "ok": ok,
        "surface": "api",
        "status_code": response.status_code,
        "api_url": api_url,
        "final_url": str(response.url),
        "api_success": api_success,
        "has_digest_df": has_digest_df,
    }


def validate(
    session_value: str,
    digest_url: str,
    timeout: float,
    api_url: str = DEFAULT_API_URL,
    request_headers_value: str | None = None,
) -> dict[str, object]:
    result = validate_api(
        session_value,
        api_url,
        timeout,
        digest_url=digest_url,
        request_headers_value=request_headers_value,
    )
    result["digest_url"] = digest_url
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", help="Override ECHOES_CONFIG_DIR")
    parser.add_argument("--session", help="Override SCHOLAR_INBOX_SESSION")
    parser.add_argument(
        "--session-stdin",
        action="store_true",
        help="Read SCHOLAR_INBOX_SESSION from standard input",
    )
    parser.add_argument("--digest-url", help="Override SCHOLAR_INBOX_DIGEST_URL")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="Scholar Inbox JSON API endpoint")
    parser.add_argument("--request-headers-json", help="Override saved Scholar Inbox request headers JSON")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    cfg_dir = config_dir(args.config_dir)
    values = parse_env_file(cfg_dir / "credentials.env")
    stdin_session = sys.stdin.read().strip() if args.session_stdin else ""
    request_headers_value = (
        args.request_headers_json
        or os.environ.get(SAVED_REQUEST_HEADERS_KEY)
        or values.get(SAVED_REQUEST_HEADERS_KEY)
    )
    saved_headers: dict[str, str] = {}
    try:
        saved_headers = parse_saved_request_headers(request_headers_value)
    except ValueError as exc:
        result = {
            "ok": False,
            "error": f"Invalid {SAVED_REQUEST_HEADERS_KEY}: {exc}",
            "credentials_path": str(cfg_dir / "credentials.env"),
        }
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(result["error"])
        return 1

    session_value = (
        args.session
        or stdin_session
        or os.environ.get("SCHOLAR_INBOX_SESSION")
        or values.get("SCHOLAR_INBOX_SESSION")
        or cookie_from_headers(saved_headers)
    )
    digest_url = (
        args.digest_url
        or os.environ.get("SCHOLAR_INBOX_DIGEST_URL")
        or values.get("SCHOLAR_INBOX_DIGEST_URL")
        or DEFAULT_DIGEST_URL
    )

    if not session_value:
        result = {
            "ok": False,
            "error": "SCHOLAR_INBOX_SESSION is missing.",
            "credentials_path": str(cfg_dir / "credentials.env"),
        }
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(result["error"])
            print(f"Expected credentials at {result['credentials_path']}")
        return 1

    try:
        result = validate(
            session_value,
            digest_url,
            args.timeout,
            args.api_url,
            request_headers_value=request_headers_value,
        )
    except requests.RequestException as exc:
        result = {
            "ok": False,
            "error": str(exc),
            "digest_url": digest_url,
        }
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"Scholar Inbox check failed: {exc}")
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if result["ok"]:
            print(
                "Scholar Inbox auth valid: "
                f"{result['status_code']} from {result['final_url']}"
            )
        else:
            print(
                "Scholar Inbox auth invalid: "
                f"{result['status_code']} from {result['final_url']}"
            )
            if result.get("api_success") is False:
                print("The Scholar Inbox API reported success=false.")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
