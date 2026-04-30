from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "fetch_digest.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("fetch_digest", MODULE_PATH)
assert SPEC and SPEC.loader
fetch_digest = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fetch_digest
SPEC.loader.exec_module(fetch_digest)


class FetchDigestTests(unittest.TestCase):
    def test_normalize_paper_copies_normalized_api_relevance_score(self) -> None:
        record, warnings = fetch_digest.normalize_paper(
            {
                "title": "Paper",
                "url": "https://example.com/paper.pdf",
                "abstract": "Abstract",
                "ranking_score": 0.985613867,
                "paper_id": 123,
            },
            1,
        )

        self.assertEqual(warnings, [])
        self.assertEqual(record["abstract"], "Abstract")
        self.assertEqual(record["api_relevance_score"], 98.561)
        self.assertEqual(record["relevance_score"], 98.561)
        self.assertEqual(record["scholar_inbox_score"], 98.561)
        self.assertEqual(record["digest_position"], 1)

    def test_abstract_remains_primary_description_source(self) -> None:
        record, warnings = fetch_digest.normalize_paper(
            {
                "title": "Paper",
                "url": "https://example.com/paper.pdf",
                "abstract": "Primary abstract",
                "summaries": {"problem_definition_question": "Summary fallback"},
                "ranking_score": "73.5",
            },
            1,
        )

        self.assertEqual(warnings, [])
        self.assertEqual(record["description"], "Primary abstract")
        self.assertEqual(record["description_source"], "abstract")
        self.assertEqual(record["api_relevance_score"], 73.5)

    def test_http_unauthorized_payload_points_to_prepare(self) -> None:
        response = mock.Mock()
        response.status_code = 401
        exc = fetch_digest.requests.HTTPError(response=response)

        payload = fetch_digest.http_error_payload(exc, api_url=fetch_digest.DEFAULT_API_URL)

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status_code"], 401)
        self.assertIn("prepare", payload["error"])

    def test_api_digest_date_uses_scholar_inbox_format(self) -> None:
        self.assertEqual(fetch_digest.api_digest_date("2026-04-21"), "04/21/2026")

    def test_requested_date_must_match_api_current_digest_date(self) -> None:
        with self.assertRaisesRegex(ValueError, "returned digest date 2026-04-21"):
            fetch_digest.build_snapshot(
                {
                    "digest_df": [],
                    "current_digest_date": "2026-04-21",
                    "empty_digest": True,
                },
                requested_digest_date="2026-04-20",
                source_url=fetch_digest.DEFAULT_DIGEST_URL,
                api_url=fetch_digest.DEFAULT_API_URL,
            )

    def test_matching_requested_date_sets_effective_digest_date(self) -> None:
        snapshot = fetch_digest.build_snapshot(
            {
                "digest_df": [],
                "current_digest_date": "2026-04-20",
                "empty_digest": True,
            },
            requested_digest_date="2026-04-20",
            source_url=fetch_digest.DEFAULT_DIGEST_URL,
            api_url=fetch_digest.DEFAULT_API_URL,
        )

        self.assertEqual(snapshot["effective_digest_date"], "2026-04-20")
        self.assertEqual(snapshot["source_current_digest_date"], "2026-04-20")

    def test_fetch_payload_sends_scholar_inbox_date_format(self) -> None:
        response = mock.Mock()
        response.json.return_value = {"success": True, "digest_df": []}

        with mock.patch.object(fetch_digest, "request_with_dns_fallback", return_value=response) as request:
            fetch_digest.fetch_digest_payload(
                session_value="session=value",
                digest_date="2026-04-21",
                timeout=30.0,
                api_url=fetch_digest.DEFAULT_API_URL,
            )

        self.assertEqual(request.call_args.kwargs["params"]["date"], "04/21/2026")

    def test_yesterday_digest_date_uses_europe_madrid_calendar(self) -> None:
        # 2026-04-21 22:30 UTC is already 2026-04-22 in Europe/Madrid.
        now = datetime(2026, 4, 21, 22, 30, tzinfo=timezone.utc)

        self.assertEqual(fetch_digest.yesterday_digest_date(now), "2026-04-21")

    def test_yesterday_flag_requests_previous_europe_madrid_digest(self) -> None:
        payload = {
            "success": True,
            "digest_df": [],
            "current_digest_date": "2026-04-21",
            "empty_digest": True,
        }

        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(fetch_digest, "load_credentials", return_value={"SCHOLAR_INBOX_SESSION": "session=value"}),
                mock.patch.object(fetch_digest, "yesterday_digest_date", return_value="2026-04-21"),
                mock.patch.object(fetch_digest, "fetch_digest_payload", return_value=payload) as fetch_payload,
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                exit_code = fetch_digest.main(["--yesterday", "--config-dir", tmp, "--json"])

        self.assertEqual(exit_code, 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["requested_digest_date"], "2026-04-21")
        self.assertEqual(result["effective_digest_date"], "2026-04-21")
        self.assertEqual(fetch_payload.call_args.kwargs["digest_date"], "2026-04-21")

    def test_date_and_yesterday_are_mutually_exclusive(self) -> None:
        with self.assertRaises(SystemExit) as exc:
            fetch_digest.main(["--date", "2026-04-20", "--yesterday"])

        self.assertEqual(exc.exception.code, 2)

    def test_json_fetch_error_is_machine_readable(self) -> None:
        with (
            mock.patch.object(fetch_digest, "load_credentials", return_value={"SCHOLAR_INBOX_SESSION": "session=value"}),
            mock.patch.object(
                fetch_digest,
                "fetch_digest_payload",
                side_effect=fetch_digest.requests.ConnectionError("Failed to resolve api.scholar-inbox.com"),
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            exit_code = fetch_digest.main(["--date", "2026-04-20", "--json"])

        self.assertEqual(exit_code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["ok"])
        self.assertIn("Failed to resolve", payload["error"])

    def test_json_missing_auth_error_is_machine_readable(self) -> None:
        with (
            mock.patch.object(fetch_digest, "load_credentials", return_value={}),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            exit_code = fetch_digest.main(["--date", "2026-04-20", "--json"])

        self.assertEqual(exit_code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["ok"])
        self.assertIn("SCHOLAR_INBOX_SESSION is missing", payload["error"])

    def test_saved_request_headers_cookie_can_drive_fetch(self) -> None:
        request_headers = fetch_digest.json.dumps({"Cookie": "session=from-headers"})
        payload = {
            "success": True,
            "digest_df": [],
            "current_digest_date": "2026-04-20",
            "empty_digest": True,
        }

        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(
                    fetch_digest,
                    "load_credentials",
                    return_value={fetch_digest.SAVED_REQUEST_HEADERS_KEY: request_headers},
                ),
                mock.patch.object(fetch_digest, "fetch_digest_payload", return_value=payload) as fetch_payload,
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                exit_code = fetch_digest.main(["--date", "2026-04-20", "--config-dir", tmp, "--json"])

        self.assertEqual(exit_code, 0)
        result = json.loads(stdout.getvalue())
        self.assertTrue(result["ok"])
        self.assertEqual(fetch_payload.call_args.kwargs["session_value"], "session=from-headers")


if __name__ == "__main__":
    unittest.main()
