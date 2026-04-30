from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "prepare_env.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("prepare_env", MODULE_PATH)
assert SPEC and SPEC.loader
prepare_env = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = prepare_env
SPEC.loader.exec_module(prepare_env)


def telegram_response(payload: dict[str, object], *, status_code: int = 200) -> mock.Mock:
    response = mock.Mock()
    response.status_code = status_code
    response.json.return_value = payload
    return response


class PrepareEnvTests(unittest.TestCase):
    def test_default_profile_path_uses_private_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(prepare_env.os.environ, {"ECHOES_CONFIG_DIR": tmp}, clear=False):
                self.assertEqual(
                    prepare_env.default_profile_path(),
                    Path(tmp) / "PROFILE.md",
                )

    def test_doctor_reports_missing_profile_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            credentials_path = config_dir / "credentials.env"
            credentials_path.write_text(
                "TELEGRAM_BOT_TOKEN=PRIVATE_FAKE_TELEGRAM_TOKEN\n"
                "TELEGRAM_CHAT_ID=4242\n"
            )
            args = mock.Mock(json=True, timeout=20, skip_auth=True)

            with (
                mock.patch.dict(prepare_env.os.environ, {"ECHOES_CONFIG_DIR": tmp}, clear=False),
                mock.patch.object(prepare_env, "check_runtime_imports", return_value=prepare_env.doctor_check("runtime_dependencies", "ok", "ok")),
                mock.patch.object(prepare_env, "check_ffmpeg", return_value=prepare_env.doctor_check("ffmpeg", "ok", "ok")),
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                exit_code = prepare_env.cmd_doctor(args)

        payload_text = stdout.getvalue()
        payload = json.loads(payload_text)
        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("researcher_profile", {item["name"] for item in payload["checks"]})
        self.assertNotIn("PRIVATE_FAKE_TELEGRAM_TOKEN", payload_text)
        self.assertNotIn("4242", payload_text)

    def test_doctor_passes_with_filled_profile_and_skipped_auth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            (config_dir / "PROFILE.md").write_text("Status: Created from direct answers.\n")
            (config_dir / "credentials.env").write_text(
                "TELEGRAM_BOT_TOKEN=PRIVATE_FAKE_TELEGRAM_TOKEN\n"
                "TELEGRAM_CHAT_ID=4242\n"
            )
            args = mock.Mock(json=True, timeout=20, skip_auth=True)

            with (
                mock.patch.dict(prepare_env.os.environ, {"ECHOES_CONFIG_DIR": tmp}, clear=False),
                mock.patch.object(prepare_env, "check_runtime_imports", return_value=prepare_env.doctor_check("runtime_dependencies", "ok", "ok")),
                mock.patch.object(prepare_env, "check_uv", return_value=prepare_env.doctor_check("uv", "ok", "ok")),
                mock.patch.object(prepare_env, "check_ghostscript", return_value=prepare_env.doctor_check("ghostscript", "warn", "missing", required=False)),
                mock.patch.object(prepare_env, "check_ffmpeg", return_value=prepare_env.doctor_check("ffmpeg", "ok", "ok")),
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                exit_code = prepare_env.cmd_doctor(args)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ok"])

    def test_check_ffmpeg_reports_missing_binary_as_required(self) -> None:
        with mock.patch.object(prepare_env.shutil, "which", return_value=None):
            result = prepare_env.check_ffmpeg()

        self.assertEqual(result["name"], "ffmpeg")
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["required"])

    def test_ensure_playwright_chromium_uses_cross_platform_installer(self) -> None:
        completed = mock.Mock(returncode=0)
        with (
            mock.patch.object(prepare_env, "uv_run_command", return_value=["uv", "run", "python", "-m", "playwright", "install", "chromium"]) as uv_run_command,
            mock.patch.object(prepare_env, "run", return_value=completed) as run,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            prepare_env.ensure_playwright_chromium()

        uv_run_command.assert_called_once_with("python", "-m", "playwright", "install", "chromium")
        run.assert_called_once_with(["uv", "run", "python", "-m", "playwright", "install", "chromium"])

    def test_open_scholar_inbox_uses_macos_open_with_app(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = mock.Mock(app="Google Chrome")
            completed = mock.Mock(returncode=0)
            with (
                mock.patch.dict(prepare_env.os.environ, {"ECHOES_CONFIG_DIR": tmp}, clear=False),
                mock.patch.object(prepare_env.sys, "platform", "darwin"),
                mock.patch.object(prepare_env.shutil, "which", return_value="/usr/bin/open"),
                mock.patch.object(prepare_env.subprocess, "run", return_value=completed) as run,
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                exit_code = prepare_env.cmd_open_scholar_inbox(args)

        self.assertEqual(exit_code, 0)
        run.assert_called_once_with(
            ["/usr/bin/open", "-a", "Google Chrome", prepare_env.DEFAULT_DIGEST_URL],
            cwd=prepare_env.ROOT,
        )

    def test_open_scholar_inbox_uses_linux_xdg_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = mock.Mock(app=None)
            completed = mock.Mock(returncode=0)
            with (
                mock.patch.dict(prepare_env.os.environ, {"ECHOES_CONFIG_DIR": tmp}, clear=False),
                mock.patch.object(prepare_env.sys, "platform", "linux"),
                mock.patch.object(prepare_env.shutil, "which", return_value="/usr/bin/xdg-open"),
                mock.patch.object(prepare_env.subprocess, "run", return_value=completed) as run,
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                exit_code = prepare_env.cmd_open_scholar_inbox(args)

        self.assertEqual(exit_code, 0)
        run.assert_called_once_with(
            ["/usr/bin/xdg-open", prepare_env.DEFAULT_DIGEST_URL],
            cwd=prepare_env.ROOT,
        )

    def test_open_scholar_inbox_prints_manual_fallback_without_opener(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = mock.Mock(app=None)
            with (
                mock.patch.dict(prepare_env.os.environ, {"ECHOES_CONFIG_DIR": tmp}, clear=False),
                mock.patch.object(prepare_env.sys, "platform", "linux"),
                mock.patch.object(prepare_env.shutil, "which", return_value=None),
                mock.patch.object(prepare_env.subprocess, "run") as run,
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                exit_code = prepare_env.cmd_open_scholar_inbox(args)

        self.assertEqual(exit_code, 1)
        self.assertIn(prepare_env.DEFAULT_DIGEST_URL, stdout.getvalue())
        run.assert_not_called()

    def test_save_validated_scholar_session_handles_api_failure_shape(self) -> None:
        args = mock.Mock(value="session=bad", value_stdin=False, digest_url=prepare_env.DEFAULT_DIGEST_URL, timeout=20)
        with mock.patch.object(
            prepare_env,
            "validate_scholar_session",
            return_value={"ok": False, "status_code": 401, "final_url": "https://api.scholar-inbox.com/api?p=0"},
        ):
            with self.assertRaises(SystemExit) as exc:
                prepare_env.cmd_save_validated_scholar_session(args)

        self.assertIn("401", str(exc.exception))

    def test_save_validated_scholar_headers_persists_cookie_and_header_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = mock.Mock(
                headers=json.dumps({"Cookie": "session=fresh", "User-Agent": "Browser UA"}),
                headers_stdin=False,
                digest_url=prepare_env.DEFAULT_DIGEST_URL,
                timeout=20,
            )
            with (
                mock.patch.dict(prepare_env.os.environ, {"ECHOES_CONFIG_DIR": tmp}, clear=False),
                mock.patch.object(prepare_env, "validate_scholar_session", return_value={"ok": True}),
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                exit_code = prepare_env.cmd_save_validated_scholar_headers(args)
                values = prepare_env.parse_env_file(Path(tmp) / "credentials.env")

        self.assertEqual(exit_code, 0)
        self.assertEqual(values["SCHOLAR_INBOX_SESSION"], "session=fresh")
        saved_headers = json.loads(values[prepare_env.SAVED_REQUEST_HEADERS_KEY])
        self.assertEqual(saved_headers["Cookie"], "session=fresh")
        self.assertEqual(saved_headers["User-Agent"], "Browser UA")

    def test_save_telegram_transport_persists_api_ip_without_touching_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            credentials_path = config_dir / "credentials.env"
            credentials_path.write_text("TELEGRAM_BOT_TOKEN=secret\nTELEGRAM_CHAT_ID=42\n")
            args = mock.Mock(
                api_ip="149.154.166.110",
                api_host_header=None,
                proxy_url=None,
                api_base_url=None,
            )

            with (
                mock.patch.dict(prepare_env.os.environ, {"ECHOES_CONFIG_DIR": tmp}, clear=False),
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                exit_code = prepare_env.cmd_save_telegram_transport(args)
                values = prepare_env.parse_env_file(credentials_path)

        self.assertEqual(exit_code, 0)
        self.assertEqual(values["TELEGRAM_BOT_TOKEN"], "secret")
        self.assertEqual(values["TELEGRAM_CHAT_ID"], "42")
        self.assertEqual(values["TELEGRAM_API_IP"], "149.154.166.110")
        self.assertEqual(values["TELEGRAM_API_HOST_HEADER"], "api.telegram.org")

    def test_discover_telegram_chat_id_saves_match_from_private_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = mock.Mock(
                token="123456:abc",
                token_stdin=False,
                code="echoes-test",
                timeout=30.0,
                poll_timeout=5.0,
                json=True,
            )
            with (
                mock.patch.dict(prepare_env.os.environ, {"ECHOES_CONFIG_DIR": tmp}, clear=False),
                mock.patch.object(
                    prepare_env.requests,
                    "get",
                    side_effect=[
                        telegram_response(
                            {
                                "ok": True,
                                "result": [
                                    {
                                        "update_id": 7,
                                        "message": {
                                            "text": "old-message",
                                            "chat": {"type": "private", "id": 1},
                                        },
                                    }
                                ],
                            }
                        ),
                        telegram_response(
                            {
                                "ok": True,
                                "result": [
                                    {
                                        "update_id": 8,
                                        "message": {
                                            "text": "ignore-me",
                                            "chat": {"type": "private", "id": 2},
                                        },
                                    },
                                    {
                                        "update_id": 9,
                                        "message": {
                                            "text": "echoes-test",
                                            "chat": {"type": "private", "id": 4242},
                                        },
                                    },
                                ],
                            }
                        ),
                    ],
                ),
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                exit_code = prepare_env.cmd_discover_telegram_chat_id(args)
                values = prepare_env.parse_env_file(Path(tmp) / "credentials.env")

        self.assertEqual(exit_code, 0)
        self.assertEqual(values["TELEGRAM_BOT_TOKEN"], "123456:abc")
        self.assertEqual(values["TELEGRAM_CHAT_ID"], "4242")
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["ok"])
        self.assertNotIn("4242", stdout.getvalue())

    def test_discover_telegram_chat_id_ignores_stale_matching_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = mock.Mock(
                token="123456:abc",
                token_stdin=False,
                code="echoes-stale",
                timeout=1.0,
                poll_timeout=1.0,
                json=True,
            )
            with (
                mock.patch.dict(prepare_env.os.environ, {"ECHOES_CONFIG_DIR": tmp}, clear=False),
                mock.patch.object(
                    prepare_env.requests,
                    "get",
                    side_effect=[
                        telegram_response(
                            {
                                "ok": True,
                                "result": [
                                    {
                                        "update_id": 5,
                                        "message": {
                                            "text": "echoes-stale",
                                            "chat": {"type": "private", "id": 999},
                                        },
                                    }
                                ],
                            }
                        ),
                        telegram_response({"ok": True, "result": []}),
                    ],
                ),
                mock.patch.object(prepare_env.time, "monotonic", side_effect=[0.0, 0.1, 1.2]),
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                exit_code = prepare_env.cmd_discover_telegram_chat_id(args)

        self.assertEqual(exit_code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["ok"])
        self.assertIn("timed out", payload["message"])

    def test_discover_telegram_chat_id_ignores_non_private_and_non_matching_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = mock.Mock(
                token="123456:abc",
                token_stdin=False,
                code="echoes-private-only",
                timeout=1.0,
                poll_timeout=1.0,
                json=True,
            )
            with (
                mock.patch.dict(prepare_env.os.environ, {"ECHOES_CONFIG_DIR": tmp}, clear=False),
                mock.patch.object(
                    prepare_env.requests,
                    "get",
                    side_effect=[
                        telegram_response({"ok": True, "result": []}),
                        telegram_response(
                            {
                                "ok": True,
                                "result": [
                                    {
                                        "update_id": 10,
                                        "message": {
                                            "text": "echoes-private-only",
                                            "chat": {"type": "group", "id": -100},
                                        },
                                    },
                                    {
                                        "update_id": 11,
                                        "message": {
                                            "text": "wrong-code",
                                            "chat": {"type": "private", "id": 55},
                                        },
                                    },
                                ],
                            }
                        ),
                    ],
                ),
                mock.patch.object(prepare_env.time, "monotonic", side_effect=[0.0, 0.1, 1.2]),
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                exit_code = prepare_env.cmd_discover_telegram_chat_id(args)
                values = prepare_env.parse_env_file(Path(tmp) / "credentials.env")

        self.assertEqual(exit_code, 1)
        self.assertEqual(values, {})
        payload = json.loads(stdout.getvalue())
        self.assertIn("timed out", payload["message"])

    def test_discover_telegram_chat_id_timeout_is_secret_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token = "PRIVATE_FAKE_TELEGRAM_TOKEN"
            args = mock.Mock(
                token=token,
                token_stdin=False,
                code="echoes-timeout",
                timeout=1.0,
                poll_timeout=1.0,
                json=True,
            )
            with (
                mock.patch.dict(prepare_env.os.environ, {"ECHOES_CONFIG_DIR": tmp}, clear=False),
                mock.patch.object(
                    prepare_env.requests,
                    "get",
                    side_effect=[
                        telegram_response({"ok": True, "result": []}),
                        telegram_response({"ok": True, "result": []}),
                    ],
                ),
                mock.patch.object(prepare_env.time, "monotonic", side_effect=[0.0, 0.1, 1.2]),
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                exit_code = prepare_env.cmd_discover_telegram_chat_id(args)

        self.assertEqual(exit_code, 1)
        output = stdout.getvalue()
        self.assertIn("timed out", output)
        self.assertNotIn("PRIVATE_FAKE_TELEGRAM_TOKEN", output)

    def test_discover_telegram_chat_id_reports_invalid_token_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = mock.Mock(
                token="123456:abc",
                token_stdin=False,
                code="echoes-invalid",
                timeout=30.0,
                poll_timeout=5.0,
                json=True,
            )
            with (
                mock.patch.dict(prepare_env.os.environ, {"ECHOES_CONFIG_DIR": tmp}, clear=False),
                mock.patch.object(
                    prepare_env.requests,
                    "get",
                    return_value=telegram_response(
                        {"ok": False, "error_code": 401, "description": "Unauthorized"}
                    ),
                ),
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                exit_code = prepare_env.cmd_discover_telegram_chat_id(args)

        self.assertEqual(exit_code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertIn("invalid bot token", payload["message"])

    def test_discover_telegram_chat_id_reports_not_found_as_token_or_base_url_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = mock.Mock(
                token="123456:abc",
                token_stdin=False,
                code="echoes-not-found",
                timeout=30.0,
                poll_timeout=5.0,
                json=True,
            )
            with (
                mock.patch.dict(prepare_env.os.environ, {"ECHOES_CONFIG_DIR": tmp}, clear=False),
                mock.patch.object(
                    prepare_env.requests,
                    "get",
                    return_value=telegram_response(
                        {"ok": False, "error_code": 404, "description": "Not Found"},
                        status_code=404,
                    ),
                ),
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                exit_code = prepare_env.cmd_discover_telegram_chat_id(args)

        self.assertEqual(exit_code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertIn("bot token is invalid", payload["message"])
        self.assertIn("TELEGRAM_API_BASE_URL", payload["message"])

    def test_fetch_digest_forwards_yesterday_flag(self) -> None:
        args = mock.Mock(
            date=None,
            yesterday=True,
            output=None,
            config_dir=None,
            digest_url=None,
            api_url=None,
            timeout=20.0,
            json=True,
        )

        with mock.patch.object(prepare_env, "fetch_digest_main", return_value=0) as fetch_digest_main:
            exit_code = prepare_env.cmd_fetch_digest(args)

        self.assertEqual(exit_code, 0)
        self.assertIn("--yesterday", fetch_digest_main.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
