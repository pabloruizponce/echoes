from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

RERANK_PATH = ROOT / "scripts" / "rerank_papers.py"
RERANK_SPEC = importlib.util.spec_from_file_location("rerank_papers", RERANK_PATH)
assert RERANK_SPEC and RERANK_SPEC.loader
rerank_papers = importlib.util.module_from_spec(RERANK_SPEC)
sys.modules[RERANK_SPEC.name] = rerank_papers
RERANK_SPEC.loader.exec_module(rerank_papers)

APPLY_PATH = ROOT / "scripts" / "apply_ranking_review.py"
APPLY_SPEC = importlib.util.spec_from_file_location("apply_ranking_review", APPLY_PATH)
assert APPLY_SPEC and APPLY_SPEC.loader
apply_ranking_review = importlib.util.module_from_spec(APPLY_SPEC)
sys.modules[APPLY_SPEC.name] = apply_ranking_review
APPLY_SPEC.loader.exec_module(apply_ranking_review)

PROCESS_PATH = ROOT / "scripts" / "process_papers.py"
PROCESS_SPEC = importlib.util.spec_from_file_location("process_papers", PROCESS_PATH)
assert PROCESS_SPEC and PROCESS_SPEC.loader
process_papers = importlib.util.module_from_spec(PROCESS_SPEC)
sys.modules[PROCESS_SPEC.name] = process_papers
PROCESS_SPEC.loader.exec_module(process_papers)


PROFILE_TEXT = """# Researcher Profile

Status: Created from direct answers.

## Current Priorities And Active Questions

- Controllable human motion generation with text conditions.

## Usually Skip Signals

- Pure image editing without motion relevance.
"""


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n")


def make_digest(*papers: dict[str, object]) -> dict[str, object]:
    return {
        "effective_digest_date": "2026-03-26",
        "papers": list(papers),
    }


class RerankPapersTests(unittest.TestCase):
    def test_default_profile_path_uses_private_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(rerank_papers.os.environ, {"ECHOES_CONFIG_DIR": tmp}, clear=False):
                self.assertEqual(
                    rerank_papers.default_profile_path(),
                    Path(tmp) / "PROFILE.md",
                )

    def run_rerank(
        self,
        *,
        profile_text: str,
        digest_payload: dict[str, object],
        extra_args: list[str] | None = None,
    ) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            digest_path = root / "digest.json"
            profile_path = root / "PROFILE.md"
            output_path = root / "ranking.json"
            write_json(digest_path, digest_payload)
            profile_path.write_text(profile_text)

            args = [
                "--digest",
                str(digest_path),
                "--profile",
                str(profile_path),
                "--output-json",
                str(output_path),
            ]
            if extra_args:
                args.extend(extra_args)
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = rerank_papers.main(args)

            self.assertEqual(exit_code, 0)
            return json.loads(output_path.read_text())

    def test_review_packet_contains_api_relevance_and_abstract_without_shortlist(self) -> None:
        result = self.run_rerank(
            profile_text=PROFILE_TEXT,
            digest_payload=make_digest(
                {
                    "title": "Low Score Motion",
                    "abstract": "Relevant motion paper with a low API relevance score.",
                    "description": "Relevant motion paper with a low API relevance score.",
                    "description_source": "abstract",
                    "url": "https://example.com/low",
                    "digest_position": 1,
                    "ranking_score": 0.12,
                },
                {
                    "title": "High Score Image",
                    "abstract": "Image editing paper with a high API relevance score.",
                    "description": "Image editing paper with a high API relevance score.",
                    "description_source": "abstract",
                    "url": "https://example.com/high",
                    "digest_position": 2,
                    "api_relevance_score": 82,
                },
            ),
        )

        self.assertEqual(result["shortlist_titles"], [])
        self.assertEqual(result["shortlist_count"], 0)
        self.assertTrue(result["review_required"])
        self.assertEqual(result["ranked"][0]["paper"]["title"], "High Score Image")
        self.assertEqual(result["ranked"][0]["api_relevance_score"], 82.0)
        self.assertEqual(result["ranked"][1]["api_relevance_score"], 12.0)
        self.assertEqual(result["ranked"][1]["abstract"], "Relevant motion paper with a low API relevance score.")
        self.assertEqual(result["ranked"][0]["reasons"], [])

    def test_profile_keywords_do_not_change_ranking(self) -> None:
        digest_payload = make_digest(
            {
                "title": "Motion Diffusion",
                "abstract": "Controllable human motion generation with text conditions.",
                "url": "https://example.com/motion",
                "digest_position": 1,
                "api_relevance_score": 45,
            },
            {
                "title": "Image Restoration",
                "abstract": "Restoration benchmark for image corruption and denoising.",
                "url": "https://example.com/image",
                "digest_position": 2,
                "api_relevance_score": 80,
            },
        )
        image_profile = """# Researcher Profile

Status: Created from direct answers.

## Current Priorities And Active Questions

- Image restoration for corruption and denoising.

## Usually Skip Signals

- Human motion generation.
"""

        motion_result = self.run_rerank(profile_text=PROFILE_TEXT, digest_payload=digest_payload)
        image_result = self.run_rerank(profile_text=image_profile, digest_payload=digest_payload)

        self.assertEqual(
            [entry["paper"]["title"] for entry in motion_result["ranked"]],
            ["Image Restoration", "Motion Diffusion"],
        )
        self.assertEqual(
            [entry["paper"]["title"] for entry in image_result["ranked"]],
            ["Image Restoration", "Motion Diffusion"],
        )

    def test_low_and_missing_scores_remain_available_for_codex_review(self) -> None:
        result = self.run_rerank(
            profile_text=PROFILE_TEXT,
            digest_payload=make_digest(
                {
                    "title": "Low Score Paper",
                    "abstract": "A low-score paper still visible to Codex.",
                    "url": "https://example.com/low",
                    "digest_position": 1,
                    "ranking_score": 0.01,
                },
                {
                    "title": "Missing Score Paper",
                    "abstract": "A paper with no API score.",
                    "url": "https://example.com/missing",
                    "digest_position": 2,
                },
            ),
        )

        self.assertEqual([entry["paper"]["title"] for entry in result["ranked"]], ["Low Score Paper", "Missing Score Paper"])
        self.assertEqual(result["shortlist_titles"], [])
        self.assertTrue(any("missing an API relevance score" in note for note in result["confidence_notes"]))

    def test_legacy_min_scholar_score_argument_is_ignored(self) -> None:
        result = self.run_rerank(
            profile_text=PROFILE_TEXT,
            digest_payload=make_digest(
                {
                    "title": "Below Legacy Gate",
                    "abstract": "Still available because Codex reviews the evidence.",
                    "url": "https://example.com/low",
                    "digest_position": 1,
                    "ranking_score": 0.05,
                }
            ),
            extra_args=["--min-scholar-score", "99"],
        )

        self.assertEqual(result["ranked"][0]["paper"]["title"], "Below Legacy Gate")
        self.assertEqual(result["shortlist_titles"], [])
        self.assertEqual(result["ignored_min_scholar_score"], 99.0)

    def test_process_papers_can_consume_reviewed_evidence_packet(self) -> None:
        result = self.run_rerank(
            profile_text=PROFILE_TEXT,
            digest_payload=make_digest(
                {
                    "title": "Process Me",
                    "abstract": "Codex selects this paper after reviewing the evidence packet.",
                    "url": "https://example.com/process.pdf",
                    "digest_position": 1,
                    "ranking_score": 77,
                }
            ),
        )
        reviewed, _selected = apply_ranking_review.apply_review(
            result,
            ranking_path=Path("/tmp/ranking.json"),
            selected_titles=["Process Me"],
            allow_empty=False,
            reviewer="Codex",
            notes="Selected from the evidence packet.",
            rationales={"Process Me": "Best fit for the active profile after Codex review."},
        )

        papers = process_papers.shortlisted_papers(reviewed, limit=None)
        self.assertEqual([paper["title"] for paper in papers], ["Process Me"])
        self.assertEqual(reviewed["ranked"][0]["reasons"], ["Best fit for the active profile after Codex review."])


if __name__ == "__main__":
    unittest.main()
