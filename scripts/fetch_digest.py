#!/usr/bin/env python3
"""Fetch the current Scholar Inbox digest and persist a JSON snapshot."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from check_scholar_inbox_auth import (
    DEFAULT_DIGEST_URL,
    SAVED_REQUEST_HEADERS_KEY,
    build_scholar_inbox_headers,
    cookie_from_headers,
    parse_env_file,
    parse_saved_request_headers,
)
from dns_fallback import request_with_dns_fallback


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_API_URL = "https://api.scholar-inbox.com/api"
DEFAULT_TIMEOUT = 30.0
AUTOMATION_TIME_ZONE = "Europe/Madrid"


def config_dir(override: str | None = None) -> Path:
    if override:
        return Path(override).expanduser()
    env_override = os.environ.get("ECHOES_CONFIG_DIR")
    if env_override:
        return Path(env_override).expanduser()
    return ROOT / ".echoes"


def credentials_path(cfg_dir: Path) -> Path:
    return cfg_dir / "credentials.env"


def default_output_path(cfg_dir: Path, digest_date: str) -> Path:
    return cfg_dir / "digests" / f"{digest_date}.json"


def api_digest_date(digest_date: str) -> str:
    try:
        parsed = datetime.strptime(digest_date, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("Digest date must use YYYY-MM-DD format.") from exc
    return parsed.strftime("%m/%d/%Y")


def yesterday_digest_date(now: datetime | None = None) -> str:
    local_now = now or datetime.now(ZoneInfo(AUTOMATION_TIME_ZONE))
    if local_now.tzinfo is None:
        local_now = local_now.replace(tzinfo=ZoneInfo(AUTOMATION_TIME_ZONE))
    else:
        local_now = local_now.astimezone(ZoneInfo(AUTOMATION_TIME_ZONE))
    return (local_now.date() - timedelta(days=1)).isoformat()


def load_credentials(cfg_dir: Path) -> dict[str, str]:
    return parse_env_file(credentials_path(cfg_dir))


def build_headers(
    session_value: str,
    *,
    digest_url: str = DEFAULT_DIGEST_URL,
    request_headers_value: str | None = None,
) -> dict[str, str]:
    return build_scholar_inbox_headers(
        session_value=session_value,
        digest_url=digest_url,
        request_headers_value=request_headers_value,
    )


def description_from_paper(paper: dict[str, Any]) -> tuple[str, str]:
    abstract = str(paper.get("abstract") or "").strip()
    if abstract:
        return abstract, "abstract"

    summaries = paper.get("summaries")
    if isinstance(summaries, dict):
        for key in (
            "problem_definition_question",
            "method_explanation_question",
            "contributions_question",
            "evaluation_question",
        ):
            value = str(summaries.get(key) or "").strip()
            if value:
                return value, f"summaries.{key}"

    return "", "missing"


def normalized_api_relevance_score(paper: dict[str, Any]) -> float | None:
    raw = paper.get("ranking_score")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        score = float(raw)
    elif isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return None
        try:
            score = float(stripped)
        except ValueError:
            return None
    else:
        return None

    if 0.0 <= score <= 1.0:
        score *= 100.0
    if 0.0 <= score <= 100.0:
        return round(score, 3)
    return None


def normalized_scholar_inbox_score(paper: dict[str, Any]) -> float | None:
    return normalized_api_relevance_score(paper)


def normalize_paper(paper: dict[str, Any], index: int) -> tuple[dict[str, Any], list[str]]:
    record = dict(paper)
    warnings: list[str] = []

    title = str(paper.get("title") or "").strip()
    url = str(paper.get("url") or paper.get("html_link") or "").strip()
    abstract = str(paper.get("abstract") or "").strip()
    description, description_source = description_from_paper(paper)
    api_relevance_score = normalized_api_relevance_score(paper)

    if not title:
        warnings.append("missing title")
    if not url:
        warnings.append("missing url")
    if not description:
        warnings.append("missing description")

    record["title"] = title
    record["url"] = url
    record["abstract"] = abstract
    record["description"] = description
    record["description_source"] = description_source
    record["digest_position"] = index
    record["paper_id"] = paper.get("paper_id")
    record["api_relevance_score"] = api_relevance_score
    record["relevance_score"] = api_relevance_score
    record["scholar_inbox_score"] = api_relevance_score

    return record, warnings


def fetch_digest_payload(
    *,
    session_value: str,
    digest_date: str | None,
    timeout: float,
    api_url: str,
    digest_url: str = DEFAULT_DIGEST_URL,
    request_headers_value: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"p": 0}
    if digest_date:
        params["date"] = api_digest_date(digest_date)

    response = request_with_dns_fallback(
        "GET",
        api_url,
        headers=build_headers(
            session_value,
            digest_url=digest_url,
            request_headers_value=request_headers_value,
        ),
        params=params,
        timeout=timeout,
    )
    response.raise_for_status()

    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Scholar Inbox API returned a non-object payload.")
    return payload


def build_snapshot(
    payload: dict[str, Any],
    *,
    requested_digest_date: str | None,
    source_url: str,
    api_url: str,
) -> dict[str, Any]:
    papers = payload.get("digest_df")
    if not isinstance(papers, list):
        raise ValueError("Scholar Inbox API response is missing digest_df.")

    normalized_papers: list[dict[str, Any]] = []
    warnings: list[str] = []
    missing_fields: list[dict[str, Any]] = []

    for index, paper in enumerate(papers, start=1):
        if not isinstance(paper, dict):
            warnings.append(f"paper at position {index} is not an object")
            continue

        record, paper_warnings = normalize_paper(paper, index)
        normalized_papers.append(record)
        if paper_warnings:
            missing_fields.append(
                {
                    "digest_position": index,
                    "paper_id": record.get("paper_id"),
                    "title": record.get("title"),
                    "warnings": paper_warnings,
                }
            )

    source_current_digest_date = str(payload.get("current_digest_date") or "").strip()
    if (
        requested_digest_date
        and source_current_digest_date
        and source_current_digest_date != requested_digest_date
    ):
        raise ValueError(
            "Scholar Inbox API returned digest date "
            f"{source_current_digest_date} for requested date {requested_digest_date}."
        )

    effective_digest_date = str(
        source_current_digest_date or requested_digest_date or ""
    ).strip()
    if not effective_digest_date:
        raise ValueError("Could not determine the effective digest date.")

    if not normalized_papers and not payload.get("empty_digest"):
        warnings.append("Digest returned no papers and did not mark itself as empty.")

    return {
        "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "requested_digest_date": requested_digest_date,
        "source_current_digest_date": source_current_digest_date or None,
        "effective_digest_date": effective_digest_date,
        "source_url": source_url,
        "api_url": api_url,
        "raw_count": len(normalized_papers),
        "total_papers": payload.get("total_papers"),
        "empty_digest": bool(payload.get("empty_digest")),
        "parse_warnings": warnings,
        "missing_field_notes": missing_fields,
        "papers": normalized_papers,
    }


def write_snapshot(path: Path, snapshot: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n")


def http_error_payload(exc: requests.HTTPError, *, api_url: str) -> dict[str, Any]:
    response = exc.response
    status_code = response.status_code if response is not None else None
    message = "Scholar Inbox digest fetch failed with an HTTP error."
    if status_code in {401, 403}:
        message = (
            f"Scholar Inbox API returned {status_code}. "
            "Re-run the prepare flow to refresh the saved Scholar Inbox session."
        )
    return {
        "ok": False,
        "error": message,
        "status_code": status_code,
        "api_url": api_url,
    }


def request_error_payload(exc: requests.RequestException, *, api_url: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": f"Scholar Inbox digest fetch failed: {exc}",
        "api_url": api_url,
    }


def parse_error_payload(exc: ValueError, *, api_url: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": f"Scholar Inbox digest parse failed: {exc}",
        "api_url": api_url,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    date_group = parser.add_mutually_exclusive_group()
    date_group.add_argument("--date", help="Digest date in YYYY-MM-DD format")
    date_group.add_argument(
        "--yesterday",
        action="store_true",
        help=f"Fetch the previous {AUTOMATION_TIME_ZONE} calendar day",
    )
    parser.add_argument("--output", help="Override the output JSON path")
    parser.add_argument("--config-dir", help="Override ECHOES_CONFIG_DIR")
    parser.add_argument("--session", help="Override SCHOLAR_INBOX_SESSION")
    parser.add_argument(
        "--session-stdin",
        action="store_true",
        help="Read SCHOLAR_INBOX_SESSION from standard input",
    )
    parser.add_argument(
        "--digest-url",
        help="Override SCHOLAR_INBOX_DIGEST_URL for snapshot metadata",
    )
    parser.add_argument(
        "--api-url",
        default=DEFAULT_API_URL,
        help="Scholar Inbox JSON API endpoint",
    )
    parser.add_argument(
        "--request-headers-json",
        help="Override saved Scholar Inbox request headers JSON",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable status instead of a prose summary",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    digest_date = yesterday_digest_date() if args.yesterday else args.date

    cfg_dir = config_dir(args.config_dir)
    values = load_credentials(cfg_dir)
    stdin_session = os.sys.stdin.read().strip() if args.session_stdin else ""
    request_headers_value = (
        args.request_headers_json
        or os.environ.get(SAVED_REQUEST_HEADERS_KEY)
        or values.get(SAVED_REQUEST_HEADERS_KEY)
    )
    try:
        saved_headers = parse_saved_request_headers(request_headers_value)
    except ValueError as exc:
        error_payload = {
            "ok": False,
            "error": f"Invalid {SAVED_REQUEST_HEADERS_KEY}: {exc}",
            "credentials_path": str(credentials_path(cfg_dir)),
        }
        if args.json:
            print(json.dumps(error_payload, indent=2))
        else:
            print(error_payload["error"])
        return 1
    session_value = (
        args.session
        or stdin_session
        or os.environ.get("SCHOLAR_INBOX_SESSION")
        or values.get("SCHOLAR_INBOX_SESSION")
        or cookie_from_headers(saved_headers)
    )
    source_url = (
        args.digest_url
        or os.environ.get("SCHOLAR_INBOX_DIGEST_URL")
        or values.get("SCHOLAR_INBOX_DIGEST_URL")
        or DEFAULT_DIGEST_URL
    )

    if not session_value:
        error_payload = {
            "ok": False,
            "error": (
                "SCHOLAR_INBOX_SESSION is missing and no Cookie header was saved. "
                "Run the prepare flow before fetching a digest."
            ),
            "credentials_path": str(credentials_path(cfg_dir)),
        }
        if args.json:
            print(json.dumps(error_payload, indent=2))
            return 1
        parser.error(error_payload["error"])

    error_payload: dict[str, Any] | None = None
    try:
        payload = fetch_digest_payload(
            session_value=session_value,
            digest_date=digest_date,
            timeout=args.timeout,
            api_url=args.api_url,
            digest_url=source_url,
            request_headers_value=request_headers_value,
        )
        if payload.get("success") is False:
            raise ValueError("Scholar Inbox API reported success=false for this request.")
        snapshot = build_snapshot(
            payload,
            requested_digest_date=digest_date,
            source_url=source_url,
            api_url=args.api_url,
        )
    except requests.HTTPError as exc:
        error_payload = http_error_payload(exc, api_url=args.api_url)
    except requests.RequestException as exc:
        error_payload = request_error_payload(exc, api_url=args.api_url)
    except ValueError as exc:
        error_payload = parse_error_payload(exc, api_url=args.api_url)

    if error_payload is not None:
        if args.json:
            print(json.dumps(error_payload, indent=2))
        else:
            print(error_payload["error"])
        return 1

    output_path = (
        Path(args.output).expanduser()
        if args.output
        else default_output_path(cfg_dir, snapshot["effective_digest_date"])
    )
    write_snapshot(output_path, snapshot)

    result = {
        "ok": True,
        "output_path": str(output_path),
        "requested_digest_date": snapshot["requested_digest_date"],
        "source_current_digest_date": snapshot["source_current_digest_date"],
        "effective_digest_date": snapshot["effective_digest_date"],
        "raw_count": snapshot["raw_count"],
        "empty_digest": snapshot["empty_digest"],
        "parse_warning_count": len(snapshot["parse_warnings"]),
        "missing_field_count": len(snapshot["missing_field_notes"]),
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(
            "Fetched Scholar Inbox digest "
            f"{snapshot['effective_digest_date']} with {snapshot['raw_count']} papers."
        )
        print(f"Saved JSON snapshot to {output_path}")
        if snapshot["parse_warnings"]:
            print(f"Parse warnings: {len(snapshot['parse_warnings'])}")
        if snapshot["missing_field_notes"]:
            print(f"Papers with missing required fields: {len(snapshot['missing_field_notes'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
