#!/usr/bin/env python3
"""Initialize PROFILE.md and inspect public profile links."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import re
import shutil
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

import requests
import urllib3


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = "PROFILE.md"
PROFILE_ENV_KEY = "ECHOES_PROFILE"
DEFAULT_TIMEOUT = 20.0
DEFAULT_PDF_MAX_PAGES = 0
MAX_WEBPAGE_TEXT_CHARS = 12000
MAX_PDF_EXCERPT_CHARS = 12000


@dataclass
class LinkSnapshot:
    url: str
    final_url: str
    status_code: int
    title: str
    description: str
    signals: list[str]


def config_dir() -> Path:
    override = os.environ.get("ECHOES_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return ROOT / ".echoes"


def default_profile_path() -> Path:
    override = os.environ.get(PROFILE_ENV_KEY)
    if override:
        return Path(override).expanduser()
    return config_dir() / DEFAULT_OUTPUT


def default_evidence_path() -> Path:
    return config_dir() / "profile-evidence" / "latest.json"


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_items(values: Iterable[str]) -> list[str]:
    cleaned: list[str] = []
    for value in values:
        item = value.strip()
        if item:
            cleaned.append(item)
    return cleaned


def is_template_profile(profile_text: str) -> bool:
    return "Status: Template" in profile_text


def section(title: str, items: list[str]) -> str:
    body = "\n".join(f"- {item}" for item in items) if items else "- "
    return f"## {title}\n\n{body}\n"


def profile_markdown(args: argparse.Namespace) -> str:
    lines = ["# Researcher Profile", ""]

    status = "Created from direct answers and reviewed links."
    if args.template:
        status = "Template. Replace this placeholder only when creating the active researcher profile."
    lines.extend([f"Status: {status}", ""])
    lines.extend(
        [
            "Do not modify this file automatically once it contains real researcher information unless the user explicitly asks for an update.",
            "",
        ]
    )

    identity = [
        f"Name: {args.name or ''}".rstrip(),
        f"Role: {args.role or ''}".rstrip(),
        f"Affiliation: {args.affiliation or ''}".rstrip(),
        f"Career stage: {args.career_stage or ''}".rstrip(),
    ]
    lines.append(section("Identity And Role", identity))

    direction = []
    if args.domain:
        direction.append("Primary domains: " + "; ".join(args.domain))
    if args.theme:
        direction.append("Recurring themes: " + "; ".join(args.theme))
    lines.append(section("Current Research Direction", direction))

    lines.append(section("Learning Priorities And Active Questions", args.priority))

    methods = []
    if args.method:
        methods.append("Preferred methods: " + "; ".join(args.method))
    if args.data_type:
        methods.append("Data types: " + "; ".join(args.data_type))
    if args.model:
        methods.append("Models or benchmarks: " + "; ".join(args.model))
    if args.evidence:
        methods.append(f"Evidence expectations: {args.evidence}")
    if args.novelty:
        methods.append(f"Novelty preference: {args.novelty}")
    if args.practicality:
        methods.append(f"Practicality preference: {args.practicality}")
    lines.append(section("Methods, Data, Benchmarks, And Evidence Preferences", methods))

    useful_signals = []
    if args.must_include:
        useful_signals.append("Paper signals: " + "; ".join(args.must_include))
    if args.venue:
        useful_signals.append("Venues: " + "; ".join(args.venue))
    if args.lab:
        useful_signals.append("Labs: " + "; ".join(args.lab))
    if args.author:
        useful_signals.append("Authors: " + "; ".join(args.author))
    if args.community:
        useful_signals.append("Communities: " + "; ".join(args.community))
    lines.append(section("Useful Paper Signals", useful_signals))

    lines.append(section("Usually Out Of Scope", args.usually_skip))
    lines.append(section("Source Evidence Reviewed", args.link))
    lines.append(section("Clarifying Answers", []))
    lines.append(section("Open Uncertainties Or Assumptions", args.uncertainty))

    updated = []
    if args.updated:
        updated.append(f"Date: {args.updated}")
    if args.update_source:
        updated.append(f"Source: {args.update_source}")
    lines.append(section("Last Updated", updated))

    return "\n".join(lines).rstrip() + "\n"


def detect_signals(text: str) -> list[str]:
    lowered = text.lower()
    patterns = {
        "benchmarking": ("benchmark", "leaderboard", "evaluation"),
        "llm": ("llm", "language model", "gpt", "transformer"),
        "multimodal": ("multimodal", "vision-language", "vision language"),
        "retrieval": ("retrieval", "rag", "indexing"),
        "biology": ("biology", "bioinformatics", "genomics", "protein"),
        "robotics": ("robotics", "robot", "control"),
        "causal inference": ("causal", "intervention", "counterfactual"),
        "efficient models": ("efficiency", "distillation", "compression", "quantization"),
        "industry focus": ("product", "deployed", "production", "real-world"),
    }

    signals: list[str] = []
    for label, markers in patterns.items():
        if any(marker in lowered for marker in markers):
            signals.append(label)
    return signals


def clean_html_text(text: str) -> str:
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", text)
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def compact_text(text: str, limit: int) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip() + "..."


def extract_meta(html: str, name: str) -> str:
    patterns = [
        rf'<meta[^>]+name=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+property=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']{re.escape(name)}["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(name)}["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return clean_html_text(match.group(1))
    return ""


def extract_title(html: str) -> str:
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    if not match:
        return ""
    return clean_html_text(match.group(1))


def fetch_snapshot(url: str, timeout: float, verify: bool) -> LinkSnapshot:
    headers = {"User-Agent": "echoes-researcher-profile/0.1"}
    if not verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    response = requests.get(url, headers=headers, timeout=timeout, verify=verify)
    response.raise_for_status()

    html = response.text
    title = extract_title(html)
    description = (
        extract_meta(html, "description")
        or extract_meta(html, "og:description")
        or clean_html_text(html)[:400]
    )
    signals = detect_signals(" ".join(part for part in (title, description, response.url) if part))
    return LinkSnapshot(
        url=url,
        final_url=str(response.url),
        status_code=response.status_code,
        title=title,
        description=description,
        signals=signals,
    )


def fetch_webpage_evidence(url: str, timeout: float, verify: bool) -> dict[str, Any]:
    headers = {"User-Agent": "echoes-researcher-profile/0.1"}
    if not verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    response = requests.get(url, headers=headers, timeout=timeout, verify=verify)
    response.raise_for_status()

    html = response.text
    text = clean_html_text(html)
    return {
        "ok": True,
        "url": url,
        "final_url": str(response.url),
        "status_code": response.status_code,
        "content_type": response.headers.get("Content-Type", ""),
        "title": extract_title(html),
        "description": extract_meta(html, "description") or extract_meta(html, "og:description"),
        "text_excerpt": compact_text(text, MAX_WEBPAGE_TEXT_CHARS),
    }


def safe_slug(value: str, default: str = "paper") -> str:
    value = unquote(value)
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    value = re.sub(r"-{2,}", "-", value)
    return value[:90] or default


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 1000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not choose a unique path for {path}")


def filename_for_url(url: str, default: str = "paper") -> str:
    parsed = urlparse(url)
    name = Path(parsed.path).name or default
    slug = safe_slug(name, default=default)
    if not Path(slug).suffix:
        slug = f"{slug}.pdf"
    return slug


def pdf_metadata_dict(metadata: object) -> dict[str, str]:
    if not metadata:
        return {}
    result: dict[str, str] = {}
    for key, value in dict(metadata).items():
        clean_key = str(key).lstrip("/")
        if clean_key and value is not None:
            result[clean_key] = str(value)
    return result


def extract_pdf_text(pdf_path: Path, max_pages: int) -> dict[str, Any]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise SystemExit("pypdf is required for collect-evidence PDF extraction.") from exc

    reader = PdfReader(str(pdf_path))
    page_count = len(reader.pages)
    page_limit = page_count if max_pages <= 0 else min(page_count, max_pages)
    page_texts: list[str] = []
    for index in range(page_limit):
        text = reader.pages[index].extract_text() or ""
        page_texts.append(f"--- Page {index + 1} ---\n{text.strip()}")
    full_text = "\n\n".join(page_texts).strip()
    return {
        "page_count": page_count,
        "pages_extracted": page_limit,
        "metadata": pdf_metadata_dict(getattr(reader, "metadata", None)),
        "text": full_text,
        "text_excerpt": compact_text(full_text, MAX_PDF_EXCERPT_CHARS),
    }


def download_paper_evidence(
    url: str,
    *,
    downloads_dir: Path,
    timeout: float,
    verify: bool,
    max_pages: int,
) -> dict[str, Any]:
    headers = {
        "User-Agent": "echoes-researcher-profile/0.1",
        "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
    }
    if not verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    response = requests.get(url, headers=headers, timeout=timeout, verify=verify)
    response.raise_for_status()

    downloads_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = unique_path(downloads_dir / filename_for_url(url))
    pdf_path.write_bytes(response.content)

    extraction = extract_pdf_text(pdf_path, max_pages=max_pages)
    text_path = pdf_path.with_suffix(".txt")
    text_path.write_text(extraction.pop("text"), encoding="utf-8")

    return {
        "ok": True,
        "url": url,
        "final_url": str(response.url),
        "status_code": response.status_code,
        "content_type": response.headers.get("Content-Type", ""),
        "pdf_path": str(pdf_path),
        "text_path": str(text_path),
        **extraction,
    }


def cmd_init(args: argparse.Namespace) -> int:
    output = Path(args.output).expanduser() if args.output else default_profile_path()
    if output.exists() and not args.overwrite:
        raise SystemExit(
            f"{output} already exists. Refuse to overwrite without --overwrite."
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(profile_markdown(args))
    print(f"Wrote researcher profile to {output}")
    return 0


def cmd_inspect_links(args: argparse.Namespace) -> int:
    results: list[dict[str, object]] = []
    for url in normalize_items(args.link):
        try:
            snapshot = fetch_snapshot(url, args.timeout, verify=not args.insecure)
            results.append(
                {
                    "ok": True,
                    "url": snapshot.url,
                    "final_url": snapshot.final_url,
                    "status_code": snapshot.status_code,
                    "title": snapshot.title,
                    "description": snapshot.description,
                    "signals": snapshot.signals,
                }
            )
        except requests.RequestException as exc:
            results.append({"ok": False, "url": url, "error": str(exc)})

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        for entry in results:
            if not entry["ok"]:
                print(f"- {entry['url']}: error: {entry['error']}")
                continue
            print(f"- {entry['url']}")
            print(f"  title: {entry['title']}")
            print(f"  description: {entry['description']}")
            if entry["signals"]:
                print(f"  signals: {', '.join(entry['signals'])}")
    return 0


def cmd_collect_evidence(args: argparse.Namespace) -> int:
    output = Path(args.output).expanduser() if args.output else default_evidence_path()
    downloads_dir = (
        Path(args.downloads_dir).expanduser()
        if args.downloads_dir
        else output.parent / "papers"
    )
    output.parent.mkdir(parents=True, exist_ok=True)

    webpages: list[dict[str, Any]] = []
    for url in normalize_items(args.webpage):
        try:
            webpages.append(fetch_webpage_evidence(url, args.timeout, verify=not args.insecure))
        except requests.RequestException as exc:
            webpages.append({"ok": False, "url": url, "error": str(exc)})

    papers: list[dict[str, Any]] = []
    for url in normalize_items(args.paper_url):
        try:
            papers.append(
                download_paper_evidence(
                    url,
                    downloads_dir=downloads_dir,
                    timeout=args.timeout,
                    verify=not args.insecure,
                    max_pages=args.max_pdf_pages,
                )
            )
        except Exception as exc:
            papers.append({"ok": False, "url": url, "error": str(exc)})

    paper_descriptions = [
        {
            "description": description,
            "downloaded": False,
            "requires_search_and_confirmation": True,
        }
        for description in normalize_items(args.paper_description)
    ]

    payload = {
        "created_at": iso_now(),
        "profile_path": str(default_profile_path()),
        "source_descriptions": normalize_items(args.description),
        "webpages": webpages,
        "papers": papers,
        "paper_descriptions": paper_descriptions,
        "codex_next_steps": [
            "Read this evidence bundle and any extracted paper text files.",
            "Write a short source brief before asking clarifying questions.",
            "Ask multiple-choice clarification questions with room for free-form answers.",
            "Create or update the private researcher profile from the evidence and answers.",
        ],
    }
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

    if args.json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "output_path": str(output),
                    "webpage_count": len(webpages),
                    "paper_count": len(papers),
                    "paper_description_count": len(paper_descriptions),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print(f"Wrote researcher evidence bundle to {output}")
        print(f"Webpages: {len(webpages)}. Downloaded papers: {len(papers)}. Paper descriptions: {len(paper_descriptions)}.")
    return 0


def cmd_import_markdown(args: argparse.Namespace) -> int:
    source = Path(args.source).expanduser()
    if not source.exists():
        raise SystemExit(f"Imported profile file does not exist: {source}")
    if not source.is_file():
        raise SystemExit(f"Imported profile path is not a file: {source}")

    profile_text = source.read_text(encoding="utf-8")
    if not profile_text.strip():
        raise SystemExit("Imported profile file is empty.")
    if is_template_profile(profile_text):
        raise SystemExit("Imported profile file still looks like a template. Provide a filled profile instead.")

    output = Path(args.output).expanduser() if args.output else default_profile_path()
    same_path = source.resolve() == output.resolve() if output.exists() else False
    if output.exists() and not args.overwrite and not same_path:
        raise SystemExit(
            f"{output} already exists. Refuse to overwrite without --overwrite."
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    if not same_path:
        shutil.copyfile(source, output)

    payload = {
        "ok": True,
        "output_path": str(output),
        "source_path": str(source),
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Imported researcher profile to {output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="Create a researcher profile markdown file")
    init.add_argument(
        "--output",
        help=(
            "Output profile path. Defaults to ECHOES_PROFILE "
            f"or {config_dir() / DEFAULT_OUTPUT}."
        ),
    )
    init.add_argument("--overwrite", action="store_true")
    init.add_argument("--template", action="store_true")
    init.add_argument("--name")
    init.add_argument("--role")
    init.add_argument("--affiliation")
    init.add_argument("--career-stage")
    init.add_argument("--domain", action="append", default=[])
    init.add_argument("--theme", action="append", default=[])
    init.add_argument("--priority", action="append", default=[])
    init.add_argument("--method", action="append", default=[])
    init.add_argument("--data-type", action="append", default=[])
    init.add_argument("--model", action="append", default=[])
    init.add_argument("--venue", action="append", default=[])
    init.add_argument("--lab", action="append", default=[])
    init.add_argument("--author", action="append", default=[])
    init.add_argument("--community", action="append", default=[])
    init.add_argument("--must-include", action="append", default=[])
    init.add_argument("--usually-skip", action="append", default=[])
    init.add_argument("--evidence")
    init.add_argument("--novelty")
    init.add_argument("--practicality")
    init.add_argument("--link", action="append", default=[])
    init.add_argument("--uncertainty", action="append", default=[])
    init.add_argument("--updated")
    init.add_argument("--update-source")
    init.set_defaults(func=cmd_init)

    inspect_links = subparsers.add_parser(
        "inspect-links",
        help="Fetch public links and extract lightweight profile signals",
    )
    inspect_links.add_argument("--link", action="append", required=True)
    inspect_links.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    inspect_links.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS verification as a last resort for local certificate issues",
    )
    inspect_links.add_argument("--json", action="store_true")
    inspect_links.set_defaults(func=cmd_inspect_links)

    collect = subparsers.add_parser(
        "collect-evidence",
        help="Collect private researcher/profile evidence from webpages, descriptions, and confirmed paper URLs",
    )
    collect.add_argument(
        "--output",
        help=(
            "Output evidence JSON path. Defaults to "
            f"{default_evidence_path()}."
        ),
    )
    collect.add_argument(
        "--downloads-dir",
        help="Directory for downloaded seed paper PDFs and extracted text. Defaults next to the evidence JSON.",
    )
    collect.add_argument(
        "--webpage",
        action="append",
        default=[],
        help="Researcher, lab, project, publication-list, or related public webpage.",
    )
    collect.add_argument(
        "--description",
        action="append",
        default=[],
        help="Free-form researcher self-description, goals, or context.",
    )
    collect.add_argument(
        "--paper-url",
        action="append",
        default=[],
        help="Confirmed seed paper PDF URL to download and extract.",
    )
    collect.add_argument(
        "--paper-description",
        action="append",
        default=[],
        help="Paper/topic description that still requires Codex search and user confirmation before download.",
    )
    collect.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    collect.add_argument(
        "--max-pdf-pages",
        type=int,
        default=DEFAULT_PDF_MAX_PAGES,
        help="Maximum pages to extract from each PDF; 0 means all pages.",
    )
    collect.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS verification as a last resort for local certificate issues",
    )
    collect.add_argument("--json", action="store_true")
    collect.set_defaults(func=cmd_collect_evidence)

    import_markdown = subparsers.add_parser(
        "import-markdown",
        help="Copy an existing markdown researcher profile into the active private profile path",
    )
    import_markdown.add_argument("--source", required=True, help="Source markdown file to import")
    import_markdown.add_argument(
        "--output",
        help=(
            "Output profile path. Defaults to ECHOES_PROFILE "
            f"or {config_dir() / DEFAULT_OUTPUT}."
        ),
    )
    import_markdown.add_argument("--overwrite", action="store_true")
    import_markdown.add_argument("--json", action="store_true")
    import_markdown.set_defaults(func=cmd_import_markdown)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    for field in (
        "domain",
        "theme",
        "priority",
        "method",
        "data_type",
        "model",
        "venue",
        "lab",
        "author",
        "community",
        "must_include",
        "usually_skip",
        "link",
        "uncertainty",
        "webpage",
        "description",
        "paper_url",
        "paper_description",
    ):
        if hasattr(args, field):
            setattr(args, field, normalize_items(getattr(args, field)))

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
