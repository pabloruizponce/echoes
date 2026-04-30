from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_scholar_inbox_auth.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("check_scholar_inbox_auth", MODULE_PATH)
assert SPEC and SPEC.loader
check_scholar_inbox_auth = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = check_scholar_inbox_auth
SPEC.loader.exec_module(check_scholar_inbox_auth)


class CheckScholarInboxAuthTests(unittest.TestCase):
    def test_validate_uses_api_surface(self) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.url = "https://api.scholar-inbox.com/api?p=0"
        response.json.return_value = {"success": True, "digest_df": []}

        with mock.patch.object(check_scholar_inbox_auth, "request_with_dns_fallback", return_value=response) as request:
            result = check_scholar_inbox_auth.validate(
                "session=value",
                "https://www.scholar-inbox.com/digest",
                20,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["surface"], "api")
        request.assert_called_once()
        self.assertEqual(request.call_args.args[:2], ("GET", "https://api.scholar-inbox.com/api"))
        self.assertEqual(request.call_args.kwargs["params"], {"p": 0})
        headers = request.call_args.kwargs["headers"]
        self.assertIn("Mozilla/5.0", headers["User-Agent"])
        self.assertEqual(headers["Referer"], "https://www.scholar-inbox.com/digest")

    def test_validate_rejects_api_auth_failure(self) -> None:
        response = mock.Mock()
        response.status_code = 401
        response.url = "https://api.scholar-inbox.com/api?p=0"
        response.json.return_value = {"success": False, "error": "Authentication required"}

        with mock.patch.object(check_scholar_inbox_auth, "request_with_dns_fallback", return_value=response):
            result = check_scholar_inbox_auth.validate(
                "session=value",
                "https://www.scholar-inbox.com/digest",
                20,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["api_success"], False)

    def test_saved_request_headers_are_cleaned_and_used(self) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.url = "https://api.scholar-inbox.com/api?p=0"
        response.json.return_value = {"success": True, "digest_df": []}
        raw_headers = """
        {
          "request": {
            "headers": {
              "Host": "api.scholar-inbox.com",
              "Cookie": "session=from-browser",
              "User-Agent": "Browser UA",
              "Accept-Language": "es-ES,es;q=0.9"
            }
          }
        }
        """

        with mock.patch.object(check_scholar_inbox_auth, "request_with_dns_fallback", return_value=response) as request:
            result = check_scholar_inbox_auth.validate(
                "session=override",
                "https://www.scholar-inbox.com/digest",
                20,
                request_headers_value=raw_headers,
            )

        self.assertTrue(result["ok"])
        headers = request.call_args.kwargs["headers"]
        self.assertNotIn("Host", headers)
        self.assertEqual(headers["Cookie"], "session=override")
        self.assertEqual(headers["User-Agent"], "Browser UA")
        self.assertEqual(headers["Accept-Language"], "es-ES,es;q=0.9")

    def test_cookie_can_be_read_from_saved_header_json(self) -> None:
        headers = check_scholar_inbox_auth.parse_request_headers_input(
            '[{"name":"Cookie","value":"session=abc"},{"name":"Accept-Encoding","value":"gzip"}]'
        )

        self.assertEqual(check_scholar_inbox_auth.cookie_from_headers(headers), "session=abc")
        self.assertNotIn("Accept-Encoding", headers)


if __name__ == "__main__":
    unittest.main()
