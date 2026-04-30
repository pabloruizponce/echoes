#!/usr/bin/env python3
"""Prepare digest papers as a Codex review packet."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = "PROFILE.md"
PROFILE_ENV_KEY = "ECHOES_PROFILE"
RELEVANCE_SCORE_KEYS = (
    "api_relevance_score",
    "relevance_score",
    "scholar_inbox_score",
    "ranking_score",
)


def config_dir() -> Path:
    override = os.environ.get("ECHOES_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return ROOT / ".echoes"


def default_profile_path() -> Path:
    override = os.environ.get(PROFILE_ENV_KEY)
    if override:
        return Path(override).expanduser()
    return config_dir() / DEFAULT_PROFILE


def resolve_profile_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    return default_profile_path()


def is_template_profile(profile_text: str) -> bool:
    return "Status: Template" in profile_text


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise SystemExit(f"{path} does not contain a JSON object.")
    return payload


def coerce_score_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        match = re.search(r"\d+(?:\.\d+)?", stripped.replace(",", "."))
        if not match:
            return None
        number = float(match.group(0))
    else:
        return None

    if 0.0 <= number <= 1.0:
        number *= 100.0
    if 0.0 <= number <= 100.0:
        return round(number, 3)
    return None


def extract_api_relevance_score(paper: dict[str, Any]) -> float | None:
    for key in RELEVANCE_SCORE_KEYS:
        score = coerce_score_value(paper.get(key))
        if score is not None:
            return score
    return None


def abstract_from_paper(paper: dict[str, Any]) -> tuple[str, str]:
    abstract = str(paper.get("abstract") or "").strip()
    if abstract:
        return abstract, "abstract"

    description = str(paper.get("description") or "").strip()
    if description and paper.get("description_source") == "abstract":
        return description, "description"
    return "", "missing"


def evidence_entry(paper: dict[str, Any]) -> dict[str, Any]:
    normalized_paper = dict(paper)
    api_relevance_score = extract_api_relevance_score(paper)
    abstract, abstract_source = abstract_from_paper(paper)

    normalized_paper["abstract"] = abstract
    normalized_paper["api_relevance_score"] = api_relevance_score
    normalized_paper["relevance_score"] = api_relevance_score
    normalized_paper["scholar_inbox_score"] = api_relevance_score

    return {
        "paper": normalized_paper,
        "api_relevance_score": api_relevance_score,
        "relevance_score": api_relevance_score,
        "scholar_inbox_score": api_relevance_score,
        "abstract": abstract,
        "abstract_source": abstract_source,
        "description": str(paper.get("description") or "").strip(),
        "description_source": paper.get("description_source"),
        "digest_position": paper.get("digest_position"),
        "review_inputs": {
            "title": normalized_paper.get("title"),
            "url": normalized_paper.get("url"),
            "api_relevance_score": api_relevance_score,
            "abstract": abstract,
            "digest_position": paper.get("digest_position"),
        },
        "reasons": [],
        "codex_review": {"status": "pending"},
    }


def rank_key(entry: dict[str, Any]) -> tuple[float, int]:
    score = entry.get("api_relevance_score")
    score_key = float(score) if isinstance(score, (int, float)) else -1.0
    position = entry.get("digest_position")
    position_key = position if isinstance(position, int) and position > 0 else 10**9
    return (-score_key, position_key)


def markdown_report(
    *,
    digest_path: Path,
    profile_path: Path,
    ranked: list[dict[str, Any]],
    ignored_min_scholar_score: float | None,
) -> str:
    lines = [
        "# Codex Ranking Review Packet",
        "",
        f"- Digest: {digest_path}",
        f"- Researcher profile: {profile_path}",
        f"- Papers available for review: {len(ranked)}",
        "- Automatic shortlist: disabled",
        "- Review required: yes",
    ]
    if ignored_min_scholar_score is not None:
        lines.append(
            f"- Ignored legacy Scholar Inbox threshold argument: {ignored_min_scholar_score:.0f}"
        )
    lines.extend(
        [
            "",
            "Codex must read the active profile, API relevance score, abstract, and metadata before selecting papers.",
            "",
            "## Papers",
            "",
        ]
    )

    for idx, item in enumerate(ranked, start=1):
        paper = item["paper"]
        title = paper.get("title") or "Untitled paper"
        score = item["api_relevance_score"]
        score_text = f"{score:.1f}" if isinstance(score, (int, float)) else "missing"
        abstract = item["abstract"] or item["description"] or ""
        lines.append(f"{idx}. **{title}**")
        lines.append(f"   - API relevance score: {score_text}")
        lines.append(f"   - Digest position: {item.get('digest_position') or 'unknown'}")
        lines.append(f"   - URL: {paper.get('url') or 'missing'}")
        if abstract:
            lines.append(f"   - Abstract: {abstract}")
        else:
            lines.append("   - Abstract: missing")
    lines.append("")
    return "\n".join(lines)


def concise_stdout(
    *,
    digest_path: Path,
    profile_path: Path,
    ranked: list[dict[str, Any]],
) -> str:
    return "\n".join(
        [
            f"Prepared Codex ranking review packet for {digest_path}.",
            f"Profile: {profile_path}. Papers available: {len(ranked)}. Automatic shortlist disabled.",
        ]
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--digest", required=True, help="Path to the fetched digest JSON")
    parser.add_argument(
        "--profile",
        help=(
            "Path to the active researcher profile. Defaults to ECHOES_PROFILE "
            f"or {config_dir() / DEFAULT_PROFILE}."
        ),
    )
    parser.add_argument("--output-markdown", help="Write the review packet report to this path")
    parser.add_argument("--output-json", help="Write machine-readable review packet data to this path")
    parser.add_argument(
        "--min-scholar-score",
        type=float,
        default=None,
        help="Legacy argument accepted for compatibility but ignored; Codex decides relevance.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the default concise stdout summary",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    digest_path = Path(args.digest).expanduser()
    profile_path = resolve_profile_path(args.profile)
    if not digest_path.exists():
        raise SystemExit(f"Digest file not found: {digest_path}")
    if not profile_path.exists():
        raise SystemExit(
            f"Researcher profile not found: {profile_path}. Create it before preparing a ranking review packet."
        )

    digest = load_json(digest_path)
    papers = digest.get("papers")
    if not isinstance(papers, list):
        raise SystemExit(f"{digest_path} is missing a papers list.")

    profile_text = profile_path.read_text()
    if is_template_profile(profile_text):
        raise SystemExit(
            f"{profile_path} is still a template. Fill it with real researcher information before reranking."
        )

    ranked = [evidence_entry(paper) for paper in papers if isinstance(paper, dict)]
    ranked.sort(key=rank_key)

    confidence_notes = [
        "Automatic ranking and keyword fallbacks are disabled; Codex must select papers from this evidence packet."
    ]
    if any(item["api_relevance_score"] is None for item in ranked):
        confidence_notes.append("One or more papers are missing an API relevance score.")
    if any(not item["abstract"] for item in ranked):
        confidence_notes.append("One or more papers are missing an abstract.")

    result = {
        "digest_path": str(digest_path),
        "profile_path": str(profile_path),
        "paper_count": len(ranked),
        "shortlist_count": 0,
        "shortlist_titles": [],
        "ranking_mode": "codex_review_packet",
        "review_required": True,
        "confidence": "requires_codex_review",
        "confidence_notes": confidence_notes,
        "ranked": ranked,
    }

    if args.min_scholar_score is not None:
        result["ignored_min_scholar_score"] = args.min_scholar_score

    if args.output_json:
        output_json = Path(args.output_json).expanduser()
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")

    report = markdown_report(
        digest_path=digest_path,
        profile_path=profile_path,
        ranked=ranked,
        ignored_min_scholar_score=args.min_scholar_score,
    )
    if args.output_markdown:
        output_markdown = Path(args.output_markdown).expanduser()
        output_markdown.parent.mkdir(parents=True, exist_ok=True)
        output_markdown.write_text(report)
    if not args.quiet:
        print(concise_stdout(digest_path=digest_path, profile_path=profile_path, ranked=ranked))
    if not args.output_markdown and not args.output_json:
        print("")
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
