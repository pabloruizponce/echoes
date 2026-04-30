#!/usr/bin/env python3
"""Apply a Codex-reviewed shortlist to a ranking artifact."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise SystemExit(f"{path} does not contain a JSON object.")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def ranked_title_map(ranking: dict[str, Any]) -> dict[str, dict[str, Any]]:
    ranked = ranking.get("ranked")
    if not isinstance(ranked, list):
        raise SystemExit("Ranking artifact is missing a ranked list.")

    titles: dict[str, dict[str, Any]] = {}
    for entry in ranked:
        if not isinstance(entry, dict):
            continue
        paper = entry.get("paper")
        if not isinstance(paper, dict):
            continue
        title = str(paper.get("title") or "").strip()
        if title:
            titles[title] = entry
    return titles


def parse_selected_titles(args: argparse.Namespace) -> list[str]:
    titles = [str(title).strip() for title in args.selected_title if str(title).strip()]
    if args.selected_titles_json:
        try:
            parsed = json.loads(args.selected_titles_json)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--selected-titles-json is not valid JSON: {exc}") from exc
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            raise SystemExit("--selected-titles-json must be a JSON array of strings.")
        titles.extend(item.strip() for item in parsed if item.strip())

    deduped: list[str] = []
    seen: set[str] = set()
    for title in titles:
        if title in seen:
            continue
        seen.add(title)
        deduped.append(title)
    return deduped


def load_review_payload(args: argparse.Namespace) -> dict[str, Any]:
    raw = ""
    if args.review_json:
        raw = args.review_json
    if args.review_json_path:
        raw = Path(args.review_json_path).expanduser().read_text()
    if not raw:
        return {}

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Structured review JSON is invalid: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("Structured review JSON must be an object.")
    return payload


def structured_titles(payload: dict[str, Any]) -> list[str]:
    titles: list[str] = []
    raw_titles = payload.get("selected_titles")
    if raw_titles is not None:
        if not isinstance(raw_titles, list) or not all(isinstance(item, str) for item in raw_titles):
            raise SystemExit("review selected_titles must be a JSON array of strings.")
        titles.extend(item.strip() for item in raw_titles if item.strip())

    raw_papers = payload.get("papers") or payload.get("paper_reviews")
    if raw_papers is not None:
        if not isinstance(raw_papers, list):
            raise SystemExit("review papers must be a JSON array of objects.")
        for item in raw_papers:
            if not isinstance(item, dict):
                raise SystemExit("review papers must be a JSON array of objects.")
            decision = str(item.get("decision") or item.get("status") or "").strip().lower()
            selected = item.get("selected")
            if selected is not True and decision not in {"select", "selected", "keep"}:
                continue
            title = str(item.get("title") or "").strip()
            if title:
                titles.append(title)

    deduped: list[str] = []
    seen: set[str] = set()
    for title in titles:
        if title in seen:
            continue
        seen.add(title)
        deduped.append(title)
    return deduped


def structured_rationales(payload: dict[str, Any]) -> dict[str, str]:
    rationales: dict[str, str] = {}
    raw_rationales = payload.get("rationales")
    if raw_rationales is not None:
        if not isinstance(raw_rationales, dict):
            raise SystemExit("review rationales must be a JSON object keyed by paper title.")
        for title, rationale in raw_rationales.items():
            clean_title = str(title).strip()
            clean_rationale = str(rationale or "").strip()
            if clean_title and clean_rationale:
                rationales[clean_title] = clean_rationale

    raw_papers = payload.get("papers") or payload.get("paper_reviews")
    if raw_papers is not None:
        if not isinstance(raw_papers, list):
            raise SystemExit("review papers must be a JSON array of objects.")
        for item in raw_papers:
            if not isinstance(item, dict):
                raise SystemExit("review papers must be a JSON array of objects.")
            title = str(item.get("title") or "").strip()
            rationale = str(item.get("rationale") or item.get("reason") or item.get("notes") or "").strip()
            if title and rationale:
                rationales[title] = rationale
    return rationales


def structured_notes(payload: dict[str, Any]) -> str:
    notes = payload.get("notes")
    return str(notes).strip() if notes is not None else ""


def reviewed_markdown(
    *,
    source_path: Path,
    reviewed: dict[str, Any],
    selected_entries: list[dict[str, Any]],
    notes: str,
) -> str:
    lines = [
        "# Reviewed Ranked Papers",
        "",
        f"- Source ranking: {source_path}",
        f"- Reviewed at: {reviewed['agent_review']['reviewed_at']}",
        f"- Reviewer: {reviewed['agent_review']['reviewer']}",
        f"- Selected papers: {len(selected_entries)}",
        "",
    ]
    if notes:
        lines.extend(["## Review Notes", "", notes.strip(), ""])

    lines.extend(["## Reviewed Shortlist", ""])
    if not selected_entries:
        lines.append("No papers were selected after agent review.")
    for idx, entry in enumerate(selected_entries, start=1):
        paper = entry["paper"]
        title = paper.get("title") or "Untitled paper"
        url = paper.get("url") or "missing"
        codex_review = entry.get("codex_review")
        rationale = ""
        if isinstance(codex_review, dict):
            rationale = str(codex_review.get("rationale") or "").strip()
        reasons = entry.get("reasons") or []
        lines.append(f"{idx}. **{title}**")
        lines.append(f"   - URL: {url}")
        if rationale:
            lines.append(f"   - Review basis: {rationale}")
        elif reasons:
            lines.append(f"   - Review basis: {'; '.join(str(reason) for reason in reasons)}")
    lines.append("")
    return "\n".join(lines)


def apply_review(
    ranking: dict[str, Any],
    *,
    ranking_path: Path,
    selected_titles: list[str],
    allow_empty: bool,
    reviewer: str,
    notes: str,
    rationales: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not selected_titles and not allow_empty:
        raise SystemExit("No selected titles were provided. Pass --allow-empty to confirm an empty shortlist.")

    rationales = rationales or {}
    title_map = ranked_title_map(ranking)
    missing = [title for title in selected_titles if title not in title_map]
    if missing:
        available = sorted(title_map)
        raise SystemExit(
            "Selected title(s) are not present in the ranking artifact: "
            + "; ".join(missing)
            + ". Available titles: "
            + "; ".join(available)
        )

    ranked_entries = []
    updated_title_map: dict[str, dict[str, Any]] = {}
    selected_set = set(selected_titles)
    for entry in ranking.get("ranked", []):
        if not isinstance(entry, dict):
            ranked_entries.append(entry)
            continue
        paper = entry.get("paper")
        title = str(paper.get("title") or "").strip() if isinstance(paper, dict) else ""
        next_entry = dict(entry)
        if title in rationales:
            next_entry["codex_review"] = {
                "status": "selected" if title in selected_set else "reviewed",
                "rationale": rationales[title],
            }
            if title in selected_set:
                next_entry["reasons"] = [rationales[title]]
        ranked_entries.append(next_entry)
        if title:
            updated_title_map[title] = next_entry
    selected_entries = [updated_title_map[title] for title in selected_titles]

    original_titles = [
        str(title)
        for title in ranking.get("shortlist_titles", [])
        if isinstance(title, str) and title.strip()
    ]

    reviewed = dict(ranking)
    reviewed["ranked"] = ranked_entries
    reviewed["shortlist_titles"] = selected_titles
    reviewed["shortlist_count"] = len(selected_titles)
    reviewed["agent_review"] = {
        "reviewed_at": iso_now(),
        "reviewer": reviewer,
        "source_ranking_path": str(ranking_path),
        "selected_titles": selected_titles,
        "original_shortlist_titles": original_titles,
        "removed_original_titles": [title for title in original_titles if title not in selected_titles],
        "added_titles": [title for title in selected_titles if title not in original_titles],
        "notes": notes,
        "paper_rationales": {
            title: rationales[title]
            for title in selected_titles
            if title in rationales
        },
    }
    return reviewed, selected_entries


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ranking", required=True, help="Input ranking JSON artifact")
    parser.add_argument(
        "--selected-title",
        action="append",
        default=[],
        help="Paper title to keep after agent review. Repeat for multiple papers.",
    )
    parser.add_argument(
        "--selected-titles-json",
        help="JSON array of paper titles to keep after agent review.",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Confirm that the reviewed shortlist should be empty.",
    )
    parser.add_argument(
        "--review-json",
        help=(
            "Structured Codex review JSON. Supports selected_titles, notes, rationales, "
            "and papers entries with title/decision/rationale."
        ),
    )
    parser.add_argument(
        "--review-json-path",
        help="Path to structured Codex review JSON.",
    )
    parser.add_argument("--reviewer", default="Codex", help="Name recorded in agent_review metadata")
    parser.add_argument("--notes", default="", help="Short review note to save with the artifact")
    parser.add_argument("--output-json", required=True, help="Output reviewed ranking JSON path")
    parser.add_argument("--output-markdown", help="Optional reviewed ranking Markdown path")
    parser.add_argument("--json", action="store_true", help="Print machine-readable status")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ranking_path = Path(args.ranking).expanduser()
    if not ranking_path.exists():
        raise SystemExit(f"Ranking artifact not found: {ranking_path}")

    ranking = load_json(ranking_path)
    review_payload = load_review_payload(args)
    selected_titles = parse_selected_titles(args) + structured_titles(review_payload)
    deduped_titles: list[str] = []
    seen: set[str] = set()
    for title in selected_titles:
        if title in seen:
            continue
        seen.add(title)
        deduped_titles.append(title)
    selected_titles = deduped_titles
    rationales = structured_rationales(review_payload)
    notes = args.notes or structured_notes(review_payload)
    reviewed, selected_entries = apply_review(
        ranking,
        ranking_path=ranking_path,
        selected_titles=selected_titles,
        allow_empty=args.allow_empty,
        reviewer=args.reviewer,
        notes=notes,
        rationales=rationales,
    )

    output_json = Path(args.output_json).expanduser()
    write_json(output_json, reviewed)
    if args.output_markdown:
        output_markdown = Path(args.output_markdown).expanduser()
        output_markdown.parent.mkdir(parents=True, exist_ok=True)
        output_markdown.write_text(
            reviewed_markdown(
                source_path=ranking_path,
                reviewed=reviewed,
                selected_entries=selected_entries,
                notes=notes,
            )
        )

    payload = {
        "ok": True,
        "output_json": str(output_json),
        "output_markdown": args.output_markdown,
        "shortlist_count": len(selected_titles),
        "shortlist_titles": selected_titles,
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Wrote reviewed ranking with {len(selected_titles)} selected paper(s) to {output_json}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
