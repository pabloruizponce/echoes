from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "apply_ranking_review.py"
SPEC = importlib.util.spec_from_file_location("apply_ranking_review", MODULE_PATH)
assert SPEC and SPEC.loader
apply_ranking_review = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = apply_ranking_review
SPEC.loader.exec_module(apply_ranking_review)


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n")


def ranking_payload() -> dict[str, object]:
    return {
        "shortlist_titles": ["Original Paper"],
        "ranked": [
            {
                "paper": {
                    "title": "Original Paper",
                    "url": "https://example.com/original",
                },
                "reasons": ["matches current priorities"],
            },
            {
                "paper": {
                    "title": "Near Miss Paper",
                    "url": "https://example.com/near",
                },
                "reasons": ["passes score gate"],
            },
        ],
    }


class ApplyRankingReviewTests(unittest.TestCase):
    def test_writes_reviewed_shortlist_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ranking = root / "ranking.json"
            output = root / "reviewed.json"
            markdown = root / "reviewed.md"
            write_json(ranking, ranking_payload())

            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = apply_ranking_review.main(
                    [
                        "--ranking",
                        str(ranking),
                        "--selected-title",
                        "Near Miss Paper",
                        "--notes",
                        "Codex review preferred the near miss.",
                        "--output-json",
                        str(output),
                        "--output-markdown",
                        str(markdown),
                        "--json",
                    ]
                )

            payload = json.loads(output.read_text())
            markdown_exists = markdown.exists()

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["shortlist_titles"], ["Near Miss Paper"])
        self.assertEqual(payload["shortlist_count"], 1)
        self.assertEqual(payload["agent_review"]["original_shortlist_titles"], ["Original Paper"])
        self.assertEqual(payload["agent_review"]["removed_original_titles"], ["Original Paper"])
        self.assertEqual(payload["agent_review"]["added_titles"], ["Near Miss Paper"])
        self.assertIn("Codex review", payload["agent_review"]["notes"])
        self.assertTrue(markdown_exists)

    def test_rejects_unknown_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ranking = root / "ranking.json"
            output = root / "reviewed.json"
            write_json(ranking, ranking_payload())

            with self.assertRaises(SystemExit) as exc:
                apply_ranking_review.main(
                    [
                        "--ranking",
                        str(ranking),
                        "--selected-title",
                        "Missing Paper",
                        "--output-json",
                        str(output),
                    ]
                )

        self.assertIn("Missing Paper", str(exc.exception))

    def test_empty_shortlist_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ranking = root / "ranking.json"
            output = root / "reviewed.json"
            write_json(ranking, ranking_payload())

            with self.assertRaises(SystemExit):
                apply_ranking_review.main(
                    [
                        "--ranking",
                        str(ranking),
                        "--output-json",
                        str(output),
                    ]
                )

            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = apply_ranking_review.main(
                    [
                        "--ranking",
                        str(ranking),
                        "--allow-empty",
                        "--output-json",
                        str(output),
                        "--json",
                    ]
                )

            payload = json.loads(output.read_text())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["shortlist_titles"], [])
        self.assertEqual(payload["shortlist_count"], 0)

    def test_structured_review_json_records_per_paper_rationale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ranking = root / "ranking.json"
            output = root / "reviewed.json"
            write_json(ranking, ranking_payload())

            review = {
                "notes": "Codex selected the paper from profile and abstract evidence.",
                "papers": [
                    {
                        "title": "Near Miss Paper",
                        "decision": "select",
                        "rationale": "The abstract is closer to the current research priority.",
                    }
                ],
            }
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = apply_ranking_review.main(
                    [
                        "--ranking",
                        str(ranking),
                        "--review-json",
                        json.dumps(review),
                        "--output-json",
                        str(output),
                        "--json",
                    ]
                )

            payload = json.loads(output.read_text())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["shortlist_titles"], ["Near Miss Paper"])
        self.assertEqual(
            payload["agent_review"]["paper_rationales"]["Near Miss Paper"],
            "The abstract is closer to the current research priority.",
        )
        reviewed_entry = next(
            entry for entry in payload["ranked"] if entry["paper"]["title"] == "Near Miss Paper"
        )
        self.assertEqual(reviewed_entry["reasons"], ["The abstract is closer to the current research priority."])
        self.assertEqual(reviewed_entry["codex_review"]["status"], "selected")


if __name__ == "__main__":
    unittest.main()
