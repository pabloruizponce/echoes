from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"


def run_installer(
    *args: str,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        [str(INSTALL), *args],
        cwd=ROOT,
        env=merged_env,
        input=input_text,
        text=True,
        capture_output=True,
    )


class InstallScriptTests(unittest.TestCase):
    def test_help_lists_interactive_flags(self) -> None:
        result = run_installer("--help")

        self.assertEqual(result.returncode, 0)
        self.assertIn("--dry-run", result.stdout)
        self.assertIn("--cron-time HH:MM", result.stdout)
        self.assertIn("--skip-auth", result.stdout)

    def test_dry_run_no_prompt_safe_path_does_not_print_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            result = run_installer(
                "--dry-run",
                "--no-prompt",
                "--skip-auth",
                "--skip-profile",
                "--skip-cron",
                "--skip-system",
                "--skip-codex-install",
                "--skip-mcp",
                env={
                    "HOME": home,
                    "TELEGRAM_BOT_TOKEN": "PRIVATE_FAKE_TELEGRAM_TOKEN",
                    "TELEGRAM_CHAT_ID": "4242",
                },
            )

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("[dry-run] skipped", output)
        self.assertNotIn("PRIVATE_FAKE_TELEGRAM_TOKEN", output)
        self.assertNotIn("4242", output)

    def test_unsupported_os_fails_clearly(self) -> None:
        result = run_installer(
            "--dry-run",
            "--no-prompt",
            "--skip-auth",
            "--skip-profile",
            "--skip-cron",
            "--skip-system",
            "--skip-codex-install",
            "--skip-mcp",
            env={"ECHOES_OS_OVERRIDE": "Plan9"},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsupported OS 'Plan9'", result.stderr)

    def test_cron_block_merge_is_idempotent(self) -> None:
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            source {INSTALL}
            block="$(render_cron_block "/srv/echoes workspace" "/home/user/checkout/.echoes" "/home/user/.notebooklm" "07:15" "/usr/local/bin:/usr/bin:/bin" "/home/user/.nvm/versions/node/v22/bin/codex" "/home/user/.local/bin/uv")"
            existing="$(printf 'MAILTO=user@example.com\\n\\n%s\\n' "$block")"
            merged_once="$(merge_cron_text "$existing" "$block")"
            merged_twice="$(merge_cron_text "$merged_once" "$block")"
            test "$merged_once" = "$merged_twice"
            printf '%s\\n' "$merged_once"
            """
        )
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("# BEGIN ECHOES DAILY"), 1)
        self.assertIn("MAILTO=user@example.com", result.stdout)
        self.assertIn("ECHOES_ALLOW_UNSANDBOXED=1", result.stdout)
        self.assertIn("NOTEBOOKLM_HOME=", result.stdout)
        self.assertIn("CODEX_BIN='/home/user/.nvm/versions/node/v22/bin/codex'", result.stdout)
        self.assertIn("UV_BIN='/home/user/.local/bin/uv'", result.stdout)
        self.assertIn("cron-wrapper.log", result.stdout)

    def test_prepend_path_entries_keeps_detected_tool_dirs_once(self) -> None:
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            source {INSTALL}
            path="$(prepend_path_entries "/usr/bin:/bin" "/home/user/.nvm/versions/node/v22/bin" "/home/user/.local/bin" "/usr/bin")"
            test "$path" = "/home/user/.nvm/versions/node/v22/bin:/home/user/.local/bin:/usr/bin:/bin"
            printf '%s\\n' "$path"
            """
        )
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "/home/user/.nvm/versions/node/v22/bin:/home/user/.local/bin:/usr/bin:/bin",
        )

    def test_missing_tool_message_is_actionable(self) -> None:
        result = run_installer(
            "--dry-run",
            "--no-prompt",
            "--skip-auth",
            "--skip-profile",
            "--skip-cron",
            "--skip-system",
            "--skip-codex-install",
            "--skip-mcp",
            env={"PATH": "/usr/bin:/bin", "ECHOES_OS_OVERRIDE": "Linux"},
        )

        output = result.stdout + result.stderr
        if result.returncode == 0:
            self.assertIn("Codex CLI is available", output)
        else:
            self.assertIn("Codex CLI is missing", output)

    def test_telegram_setup_dry_run_uses_chat_discovery_helper(self) -> None:
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            source {INSTALL}
            DRY_RUN=1
            SKIP_AUTH=0
            NO_PROMPT=0
            REPO_DIR="/srv/echoes"
            CONFIG_DIR="/tmp/echoes-config"
            prompt_allowed() {{ return 0; }}
            ask_yes_no() {{ return 0; }}
            read_secret() {{ printf 'PRIVATE_FAKE_TELEGRAM_TOKEN'; }}
            save_telegram_credentials
            """
        )
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("discover-telegram-chat-id", output)
        self.assertNotIn("PRIVATE_FAKE_TELEGRAM_TOKEN", output)

    def test_profile_import_branch_uses_import_command(self) -> None:
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            source {INSTALL}
            DRY_RUN=1
            SKIP_PROFILE=0
            NO_PROMPT=0
            REPO_DIR="/srv/echoes"
            CONFIG_DIR="/tmp/echoes-config"
            prompt_allowed() {{ return 0; }}
            ask_yes_no() {{ return 0; }}
            choose_option() {{ printf '1'; }}
            read_line() {{
              if [[ "$1" == "Path to the markdown profile to import:" ]]; then
                printf '/tmp/existing-profile.md'
              else
                printf ''
              fi
            }}
            configure_researcher_profile
            """
        )
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("researcher_profile.py import-markdown", output)
        self.assertNotIn("collect-evidence", output)

    def test_choose_option_only_returns_selection_on_stdout(self) -> None:
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            source {INSTALL}
            prompt_allowed() {{ return 0; }}
            choice="$(printf '1\\n' | choose_option "How should we set up the active researcher profile?" "2" "Import existing markdown profile" "Create or update it from evidence and Codex synthesis")"
            test "$choice" = "1"
            printf '%s\\n' "$choice"
            """
        )
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1")
        self.assertIn("How should we set up the active researcher profile?", result.stderr)


if __name__ == "__main__":
    unittest.main()
