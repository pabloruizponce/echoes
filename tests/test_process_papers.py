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


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "process_papers.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("process_papers", MODULE_PATH)
assert SPEC and SPEC.loader
process_papers = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = process_papers
SPEC.loader.exec_module(process_papers)


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


class ProcessPapersTests(unittest.TestCase):
    def test_create_notebook_with_recovery_adopts_newly_created_notebook(self) -> None:
        notebook = mock.Mock()
        notebook.id = "nb-recovered"
        notebook.title = "Paper"
        notebook.created_at = None

        with (
            mock.patch.object(process_papers, "list_notebooks", side_effect=[[], [notebook]]),
            mock.patch.object(process_papers, "create_notebook", side_effect=RuntimeError("timeout")) as create_notebook,
            mock.patch.object(process_papers.time, "sleep"),
        ):
            result = process_papers.create_notebook_with_recovery(
                "Paper",
                timeout=120,
                retries=2,
                retry_delay=0,
            )

        self.assertEqual(result["id"], "nb-recovered")
        self.assertEqual(create_notebook.call_count, 1)

    def test_add_file_source_with_recovery_adopts_new_source_by_filename(self) -> None:
        source = mock.Mock()
        source.id = "src-recovered"
        source.title = "compressed.pdf"
        source.status = 2
        source.created_at = None

        with tempfile.TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "compressed.pdf"
            file_path.write_bytes(b"pdf")

            with (
                mock.patch.object(process_papers, "list_sources", side_effect=[[], [source]]),
                mock.patch.object(process_papers, "add_file_source", side_effect=RuntimeError("timeout")) as add_file_source,
                mock.patch.object(process_papers.time, "sleep"),
            ):
                result = process_papers.add_file_source_with_recovery(
                    "nb-1",
                    file_path,
                    timeout=120,
                    retries=2,
                    retry_delay=0,
                )

        self.assertEqual(result["id"], "src-recovered")
        self.assertEqual(add_file_source.call_count, 1)

    def test_process_paper_reuses_existing_ok_result_after_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work_dir = root / "paper"
            work_dir.mkdir(parents=True)
            upload = work_dir / "compressed.pdf"
            upload.write_bytes(b"pdf")
            existing = process_papers.PaperResult(
                title="Paper",
                slug="paper",
                paper_url="https://example.com/paper.pdf",
                status="ok",
                notebook_id="nb-1",
                notebook_title="Paper",
                source_id="src-1",
                work_dir=str(work_dir),
                original_pdf_path=str(work_dir / "original.pdf"),
                compressed_pdf_path=str(upload),
                uploaded_pdf_path=str(upload),
                wait_result={"status": "ready"},
            )

            with (
                mock.patch.object(process_papers, "write_json"),
                mock.patch.object(process_papers, "download_pdf") as download_pdf,
                mock.patch.object(process_papers, "create_notebook_with_recovery") as create_notebook,
                mock.patch.object(process_papers, "add_file_source_with_recovery") as add_file_source,
                mock.patch.object(process_papers, "wait_for_source", return_value={"status": "ready"}) as wait_for_source,
            ):
                result = process_papers.process_paper(
                    {"title": "Paper", "url": "https://example.com/paper.pdf"},
                    ranking_path=root / "ranking.json",
                    digest_path=root / "digest.json",
                    digest_date="2026-03-27",
                    out_dir=root,
                    timeout=120,
                    compression_profile="printer",
                    existing=existing,
                    notebooklm_retries=2,
                    retry_delay=0,
                )

            self.assertEqual(result.status, "ok")
            download_pdf.assert_not_called()
            create_notebook.assert_not_called()
            add_file_source.assert_not_called()
            wait_for_source.assert_called_once()

    def test_process_paper_resumes_existing_source_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work_dir = root / "paper"
            work_dir.mkdir(parents=True)
            upload = work_dir / "compressed.pdf"
            upload.write_bytes(b"pdf")
            existing = process_papers.PaperResult(
                title="Paper",
                slug="paper",
                paper_url="https://example.com/paper.pdf",
                status="failed",
                notebook_id="nb-1",
                notebook_title="Paper",
                source_id="src-1",
                work_dir=str(work_dir),
                original_pdf_path=str(work_dir / "original.pdf"),
                compressed_pdf_path=str(upload),
                uploaded_pdf_path=str(upload),
                error="timeout",
            )

            with (
                mock.patch.object(process_papers, "write_json"),
                mock.patch.object(process_papers, "wait_for_source", return_value={"status": "ready"}) as wait_for_source,
                mock.patch.object(process_papers, "download_pdf") as download_pdf,
                mock.patch.object(process_papers, "create_notebook_with_recovery") as create_notebook,
                mock.patch.object(process_papers, "add_file_source_with_recovery") as add_file_source,
            ):
                result = process_papers.process_paper(
                    {"title": "Paper", "url": "https://example.com/paper.pdf"},
                    ranking_path=root / "ranking.json",
                    digest_path=root / "digest.json",
                    digest_date="2026-03-27",
                    out_dir=root,
                    timeout=120,
                    compression_profile="printer",
                    existing=existing,
                    notebooklm_retries=2,
                    retry_delay=0,
                )

            self.assertEqual(result.status, "ok")
            wait_for_source.assert_called_once()
            download_pdf.assert_not_called()
            create_notebook.assert_not_called()
            add_file_source.assert_not_called()

    def test_process_paper_recreates_invalid_existing_notebook_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work_dir = root / "paper"
            work_dir.mkdir(parents=True)
            upload = work_dir / "compressed.pdf"
            upload.write_bytes(b"pdf")
            existing = process_papers.PaperResult(
                title="Paper",
                slug="paper",
                paper_url="https://example.com/paper.pdf",
                status="ok",
                notebook_id="nb-invalid",
                notebook_title="Paper",
                source_id="src-invalid",
                work_dir=str(work_dir),
                original_pdf_path=str(work_dir / "original.pdf"),
                compressed_pdf_path=str(upload),
                uploaded_pdf_path=str(upload),
                wait_result={"status": "ready"},
            )

            with (
                mock.patch.object(process_papers, "write_json"),
                mock.patch.object(
                    process_papers,
                    "wait_for_source",
                    side_effect=[
                        RuntimeError("RPC rLM1Ne returned null result data"),
                        {"status": "ready"},
                    ],
                ) as wait_for_source,
                mock.patch.object(process_papers, "download_pdf") as download_pdf,
                mock.patch.object(
                    process_papers,
                    "create_notebook_with_recovery",
                    return_value={"id": "nb-fresh", "title": "Paper"},
                ) as create_notebook,
                mock.patch.object(
                    process_papers,
                    "add_file_source_with_recovery",
                    return_value={"id": "src-fresh"},
                ) as add_file_source,
            ):
                result = process_papers.process_paper(
                    {"title": "Paper", "url": "https://example.com/paper.pdf"},
                    ranking_path=root / "ranking.json",
                    digest_path=root / "digest.json",
                    digest_date="2026-03-27",
                    out_dir=root,
                    timeout=120,
                    compression_profile="printer",
                    existing=existing,
                    notebooklm_retries=1,
                    retry_delay=0,
                )

            self.assertEqual(result.status, "ok")
            self.assertEqual(result.notebook_id, "nb-fresh")
            self.assertEqual(result.source_id, "src-fresh")
            self.assertEqual(wait_for_source.call_count, 2)
            download_pdf.assert_not_called()
            create_notebook.assert_called_once()
            add_file_source.assert_called_once()

    def test_process_paper_retries_notebook_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "paper" / "original.pdf"
            compressed = root / "paper" / "compressed.pdf"
            original.parent.mkdir(parents=True)
            original.write_bytes(b"orig")
            compressed.write_bytes(b"cmp")

            with (
                mock.patch.object(process_papers, "write_json"),
                mock.patch.object(
                    process_papers,
                    "download_pdf",
                    return_value={"path": str(original), "size_bytes": 4},
                ),
                mock.patch.object(
                    process_papers,
                    "compress_pdf",
                    return_value={"used_output": True, "status": "ok"},
                ),
                mock.patch.object(
                    process_papers,
                    "create_notebook_with_recovery",
                    return_value={"id": "nb-2", "title": "Paper"},
                ) as create_notebook,
                mock.patch.object(
                    process_papers,
                    "add_file_source_with_recovery",
                    return_value={"id": "src-2"},
                ),
                mock.patch.object(
                    process_papers,
                    "wait_for_source",
                    return_value={"status": "ready"},
                ),
            ):
                result = process_papers.process_paper(
                    {"title": "Paper", "url": "https://example.com/paper.pdf"},
                    ranking_path=root / "ranking.json",
                    digest_path=root / "digest.json",
                    digest_date="2026-03-27",
                    out_dir=root,
                    timeout=120,
                    compression_profile="printer",
                    notebooklm_retries=2,
                    retry_delay=0,
                )

            self.assertEqual(result.status, "ok")
            create_notebook.assert_called_once()

    def test_main_rejects_unreviewed_ranking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ranking_path = root / "ranking.json"
            digest_path = root / "digest.json"

            write_json(
                ranking_path,
                {
                    "digest_path": str(digest_path),
                    "shortlist_titles": ["Paper One"],
                    "ranked": [{"paper": {"title": "Paper One", "url": "https://example.com/p1.pdf"}}],
                },
            )
            write_json(
                digest_path,
                {
                    "effective_digest_date": "2026-03-27",
                    "papers": [{"title": "Paper One", "url": "https://example.com/p1.pdf"}],
                },
            )

            with self.assertRaises(SystemExit) as exc:
                process_papers.main(
                    [
                        "--ranking",
                        str(ranking_path),
                        "--digest",
                        str(digest_path),
                        "--out-dir",
                        str(root / "processed" / "2026-03-27"),
                        "--json",
                    ]
                )

            self.assertIn("Codex-reviewed", str(exc.exception))

    def test_main_resumes_previous_manifest_without_reprocessing_ok_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ranking_path = root / "ranking.json"
            digest_path = root / "digest.json"
            out_dir = root / "processed" / "2026-03-27"
            out_dir.mkdir(parents=True)
            work_dir = out_dir / "paper-one"
            work_dir.mkdir()
            upload = work_dir / "compressed.pdf"
            upload.write_bytes(b"pdf")

            write_json(
                ranking_path,
                {
                    "digest_path": str(digest_path),
                    "shortlist_titles": ["Paper One"],
                    "agent_review": {
                        "reviewer": "Codex",
                        "selected_titles": ["Paper One"],
                    },
                    "ranked": [{"paper": {"title": "Paper One", "url": "https://example.com/p1.pdf"}}],
                },
            )
            write_json(
                digest_path,
                {
                    "effective_digest_date": "2026-03-27",
                    "papers": [{"title": "Paper One", "url": "https://example.com/p1.pdf"}],
                },
            )
            write_json(
                out_dir / "manifest.json",
                {
                    "results": [
                        {
                            "title": "Paper One",
                            "slug": "paper-one",
                            "paper_url": "https://example.com/p1.pdf",
                            "status": "ok",
                            "notebook_id": "nb-1",
                            "notebook_title": "Paper One",
                            "source_id": "src-1",
                            "work_dir": str(work_dir),
                            "original_pdf_path": str(work_dir / "original.pdf"),
                            "compressed_pdf_path": str(upload),
                            "uploaded_pdf_path": str(upload),
                            "wait_result": {"status": "ready"},
                        }
                    ]
                },
            )

            with (
                mock.patch.object(process_papers, "write_json"),
                mock.patch.object(process_papers, "download_pdf") as download_pdf,
                mock.patch.object(process_papers, "wait_for_source", return_value={"status": "ready"}) as wait_for_source,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                exit_code = process_papers.main(
                    [
                        "--ranking",
                        str(ranking_path),
                        "--digest",
                        str(digest_path),
                        "--out-dir",
                        str(out_dir),
                        "--json",
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["ok_count"], 1)
            download_pdf.assert_not_called()
            wait_for_source.assert_called_once()


if __name__ == "__main__":
    unittest.main()
