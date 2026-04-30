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


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "researcher_profile.py"
SPEC = importlib.util.spec_from_file_location("researcher_profile", MODULE_PATH)
assert SPEC and SPEC.loader
researcher_profile = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = researcher_profile
SPEC.loader.exec_module(researcher_profile)


def fake_response(
    *,
    url: str = "https://example.com/profile",
    text: str = "",
    content: bytes = b"",
    content_type: str = "text/html",
) -> mock.Mock:
    response = mock.Mock()
    response.url = url
    response.status_code = 200
    response.text = text
    response.content = content
    response.headers = {"Content-Type": content_type}
    response.raise_for_status.return_value = None
    return response


class ResearcherProfileEvidenceTests(unittest.TestCase):
    def test_default_evidence_path_uses_private_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(researcher_profile.os.environ, {"ECHOES_CONFIG_DIR": tmp}, clear=False):
                self.assertEqual(
                    researcher_profile.default_evidence_path(),
                    Path(tmp) / "profile-evidence" / "latest.json",
                )

    def test_collect_evidence_captures_pages_and_descriptions_without_signals(self) -> None:
        html = """
        <html>
          <head>
            <title>Researcher Page</title>
            <meta name="description" content="Works on controllable generation.">
          </head>
          <body>Current projects include motion generation and evaluation.</body>
        </html>
        """
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.dict(researcher_profile.os.environ, {"ECHOES_CONFIG_DIR": tmp}, clear=False),
                mock.patch.object(researcher_profile.requests, "get", return_value=fake_response(text=html)),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                exit_code = researcher_profile.main(
                    [
                        "collect-evidence",
                        "--webpage",
                        "https://example.com/profile",
                        "--description",
                        "I want to learn about controllable motion generation.",
                        "--paper-description",
                        "Recent papers on text-conditioned motion synthesis.",
                        "--json",
                    ]
                )

            output = Path(tmp) / "profile-evidence" / "latest.json"
            payload = json.loads(output.read_text())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["source_descriptions"], ["I want to learn about controllable motion generation."])
        self.assertEqual(payload["paper_descriptions"][0]["requires_search_and_confirmation"], True)
        self.assertEqual(payload["webpages"][0]["title"], "Researcher Page")
        self.assertNotIn("signals", payload["webpages"][0])

    def test_collect_evidence_downloads_paper_and_writes_extracted_text(self) -> None:
        extraction = {
            "page_count": 2,
            "pages_extracted": 2,
            "metadata": {"Title": "Seed Paper"},
            "text": "Full extracted paper text.",
            "text_excerpt": "Full extracted paper text.",
        }

        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.dict(researcher_profile.os.environ, {"ECHOES_CONFIG_DIR": tmp}, clear=False),
                mock.patch.object(
                    researcher_profile.requests,
                    "get",
                    return_value=fake_response(
                        url="https://example.com/paper.pdf",
                        content=b"%PDF-1.4\n",
                        content_type="application/pdf",
                    ),
                ),
                mock.patch.object(researcher_profile, "extract_pdf_text", return_value=extraction),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                exit_code = researcher_profile.main(
                    [
                        "collect-evidence",
                        "--paper-url",
                        "https://example.com/paper.pdf",
                        "--json",
                    ]
                )

            output = Path(tmp) / "profile-evidence" / "latest.json"
            payload = json.loads(output.read_text())
            paper = payload["papers"][0]
            pdf_exists = Path(paper["pdf_path"]).exists()
            text_exists = Path(paper["text_path"]).exists()
            text_value = Path(paper["text_path"]).read_text()

        self.assertEqual(exit_code, 0)
        self.assertTrue(pdf_exists)
        self.assertTrue(text_exists)
        self.assertEqual(text_value, "Full extracted paper text.")
        self.assertEqual(paper["metadata"], {"Title": "Seed Paper"})
        self.assertNotIn("signals", paper)

    def test_import_markdown_copies_filled_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            source = config_dir / "imported.md"
            source_text = "# Researcher Profile\n\nStatus: Created from direct answers.\n"
            source.write_text(source_text)

            with (
                mock.patch.dict(researcher_profile.os.environ, {"ECHOES_CONFIG_DIR": tmp}, clear=False),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                exit_code = researcher_profile.main(
                    [
                        "import-markdown",
                        "--source",
                        str(source),
                        "--json",
                    ]
                )

            output = config_dir / "PROFILE.md"
            imported_text = output.read_text()

        self.assertEqual(exit_code, 0)
        self.assertEqual(imported_text, source_text)

    def test_import_markdown_rejects_template_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "template.md"
            source.write_text("# Researcher Profile\n\nStatus: Template.\n")

            with self.assertRaises(SystemExit) as exc:
                researcher_profile.main(["import-markdown", "--source", str(source)])

        self.assertIn("template", str(exc.exception).lower())

    def test_import_markdown_refuses_overwrite_without_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            source = config_dir / "filled.md"
            source.write_text("# Researcher Profile\n\nStatus: Created from direct answers.\n")
            destination = config_dir / "PROFILE.md"
            destination.write_text("Existing profile\n")

            with mock.patch.dict(researcher_profile.os.environ, {"ECHOES_CONFIG_DIR": tmp}, clear=False):
                with self.assertRaises(SystemExit) as exc:
                    researcher_profile.main(["import-markdown", "--source", str(source)])

        self.assertIn("Refuse to overwrite", str(exc.exception))


if __name__ == "__main__":
    unittest.main()
