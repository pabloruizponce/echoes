from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

import requests


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "dns_fallback.py"
SPEC = importlib.util.spec_from_file_location("dns_fallback", MODULE_PATH)
assert SPEC and SPEC.loader
dns_fallback = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dns_fallback
SPEC.loader.exec_module(dns_fallback)


class DnsFallbackTests(unittest.TestCase):
    def test_request_retries_dns_failure_with_doh_resolution(self) -> None:
        calls: list[tuple[str, str]] = []
        response = mock.Mock(spec=requests.Response)

        def fake_request(method: str, url: str, **_kwargs: object) -> requests.Response:
            calls.append((method, url))
            if len(calls) == 1:
                raise requests.ConnectionError(
                    "HTTPSConnectionPool(host='api.scholar-inbox.com', port=443): "
                    "Failed to resolve 'api.scholar-inbox.com'"
                )
            return response

        with (
            mock.patch.object(dns_fallback.requests, "request", side_effect=fake_request),
            mock.patch.object(dns_fallback, "resolve_host_with_doh", return_value=["203.0.113.10"]) as resolve,
        ):
            result = dns_fallback.request_with_dns_fallback("GET", "https://api.scholar-inbox.com/api")

        self.assertIs(result, response)
        self.assertEqual(calls, [("GET", "https://api.scholar-inbox.com/api")] * 2)
        resolve.assert_called_once_with("api.scholar-inbox.com")

    def test_request_uses_static_ips_when_doh_is_blocked(self) -> None:
        calls: list[tuple[str, str]] = []
        response = mock.Mock(spec=requests.Response)

        def fake_request(method: str, url: str, **_kwargs: object) -> requests.Response:
            calls.append((method, url))
            if len(calls) == 1:
                raise requests.ConnectionError("Failed to resolve 'api.scholar-inbox.com'")
            return response

        with (
            mock.patch.object(dns_fallback.requests, "request", side_effect=fake_request),
            mock.patch.object(dns_fallback, "resolve_host_with_doh", side_effect=RuntimeError("Operation not permitted")),
            mock.patch.object(dns_fallback, "patched_getaddrinfo") as patched,
        ):
            patched.return_value.__enter__.return_value = None
            patched.return_value.__exit__.return_value = None
            result = dns_fallback.request_with_dns_fallback("GET", "https://api.scholar-inbox.com/api")

        self.assertIs(result, response)
        patched.assert_called_once_with(
            "api.scholar-inbox.com",
            ["104.21.78.3", "172.67.214.65"],
        )
        self.assertEqual(calls, [("GET", "https://api.scholar-inbox.com/api")] * 2)

    def test_request_does_not_retry_non_dns_failure(self) -> None:
        with mock.patch.object(
            dns_fallback.requests,
            "request",
            side_effect=requests.Timeout("request timed out"),
        ) as request:
            with self.assertRaises(requests.Timeout):
                dns_fallback.request_with_dns_fallback("GET", "https://api.scholar-inbox.com/api")

        request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
