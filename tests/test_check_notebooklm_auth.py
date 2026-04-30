from __future__ import annotations

import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_notebooklm_auth.py"
SPEC = importlib.util.spec_from_file_location("check_notebooklm_auth", MODULE_PATH)
assert SPEC and SPEC.loader
check_notebooklm_auth = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = check_notebooklm_auth
SPEC.loader.exec_module(check_notebooklm_auth)


class CheckNotebookLMAuthTests(unittest.TestCase):
    def test_list_check_retries_transient_502(self) -> None:
        list_calls = 0

        def fake_run_json(command: list[str]):  # noqa: ANN202
            nonlocal list_calls
            if "auth" in command:
                return 0, {"status": "ok"}
            list_calls += 1
            if list_calls == 1:
                return 1, {"message": "Server error 502 calling LIST_NOTEBOOKS: Bad Gateway"}
            return 0, {"notebooks": [{"id": "notebook"}]}

        with (
            mock.patch.object(check_notebooklm_auth, "notebooklm_binary", return_value="notebooklm"),
            mock.patch.object(check_notebooklm_auth, "run_json", side_effect=fake_run_json),
            mock.patch.object(check_notebooklm_auth.time, "sleep"),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            exit_code = check_notebooklm_auth.main(["--json", "--retry-delay", "0"])

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["list_attempts"], 2)
        self.assertEqual(payload["notebook_count"], 1)


if __name__ == "__main__":
    unittest.main()
