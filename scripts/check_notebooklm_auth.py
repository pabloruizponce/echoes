#!/usr/bin/env python3
"""Validate notebooklm-py authentication with read-only checks."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def default_storage() -> Path:
    override = os.environ.get("NOTEBOOKLM_HOME")
    if override:
        return Path(override).expanduser()
    config_override = os.environ.get("ECHOES_CONFIG_DIR")
    if config_override:
        return Path(config_override).expanduser() / "notebooklm"
    return ROOT / ".echoes" / "notebooklm"


def notebooklm_binary() -> str:
    if os.name == "nt":
        candidate = ROOT / ".venv" / "Scripts" / "notebooklm.exe"
    else:
        candidate = ROOT / ".venv" / "bin" / "notebooklm"
    if candidate.exists():
        return str(candidate)
    return "notebooklm"


def run_json(command: list[str]) -> tuple[int, dict[str, object] | list[object] | str]:
    result = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    payload = result.stdout.strip() or result.stderr.strip()
    try:
        parsed: dict[str, object] | list[object] | str = json.loads(payload)
    except json.JSONDecodeError:
        parsed = payload
    return result.returncode, parsed


def transient_notebooklm_failure(payload: dict[str, object] | list[object] | str) -> bool:
    if isinstance(payload, dict):
        text = " ".join(str(payload.get(key) or "") for key in ("code", "message", "error"))
    else:
        text = str(payload)
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "502",
            "503",
            "504",
            "bad gateway",
            "gateway timeout",
            "timeout",
            "timed out",
            "temporarily unavailable",
            "server error",
        )
    )


def run_json_with_retries(
    command: list[str],
    *,
    retries: int,
    retry_delay: float,
) -> tuple[int, dict[str, object] | list[object] | str, int]:
    attempts = max(1, retries)
    last_code = 1
    last_payload: dict[str, object] | list[object] | str = ""
    for attempt in range(1, attempts + 1):
        last_code, last_payload = run_json(command)
        if last_code == 0 or not transient_notebooklm_failure(last_payload) or attempt == attempts:
            return last_code, last_payload, attempt
        time.sleep(max(0.0, retry_delay))
    return last_code, last_payload, attempts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage", help="Optional notebooklm storage path override")
    parser.add_argument("--skip-network-test", action="store_true")
    parser.add_argument("--list-retries", type=int, default=3)
    parser.add_argument("--retry-delay", type=float, default=2.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    storage = Path(args.storage).expanduser() if args.storage else default_storage()
    os.environ.setdefault("NOTEBOOKLM_HOME", str(storage))

    binary = notebooklm_binary()
    auth_command = [binary, "auth", "check", "--json"]
    if not args.skip_network_test:
        auth_command.append("--test")
    auth_command[1:1] = ["--storage", str(storage)]

    auth_code, auth_payload = run_json(auth_command)
    auth_ok = auth_code == 0 and isinstance(auth_payload, dict) and auth_payload.get("status") == "ok"

    list_command = [binary, "list", "--json"]
    list_command[1:1] = ["--storage", str(storage)]
    list_code, list_payload, list_attempts = run_json_with_retries(
        list_command,
        retries=args.list_retries,
        retry_delay=args.retry_delay,
    )
    list_ok = list_code == 0

    if isinstance(list_payload, list):
        notebook_count = len(list_payload)
    elif isinstance(list_payload, dict) and isinstance(list_payload.get("items"), list):
        notebook_count = len(list_payload["items"])
    elif isinstance(list_payload, dict) and isinstance(list_payload.get("notebooks"), list):
        notebook_count = len(list_payload["notebooks"])
    else:
        notebook_count = None

    result = {
        "ok": auth_ok and list_ok,
        "auth_check": auth_payload,
        "list_result": list_payload,
        "list_attempts": list_attempts,
        "notebook_count": notebook_count,
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if result["ok"]:
            suffix = "" if notebook_count is None else f" ({notebook_count} notebook(s) visible)"
            print(f"NotebookLM auth valid{suffix}.")
        else:
            print("NotebookLM auth invalid.")
            print("Auth check output:")
            print(json.dumps(auth_payload, indent=2) if not isinstance(auth_payload, str) else auth_payload)
            print("List output:")
            print(json.dumps(list_payload, indent=2) if not isinstance(list_payload, str) else list_payload)

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
