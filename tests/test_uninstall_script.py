from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UNINSTALL = ROOT / "uninstall.sh"


def run_uninstaller(
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        [str(UNINSTALL), *args],
        cwd=ROOT,
        env=merged_env,
        text=True,
        capture_output=True,
    )


class UninstallScriptTests(unittest.TestCase):
    def test_help_lists_reset_flags(self) -> None:
        result = run_uninstaller("--help")

        self.assertEqual(result.returncode, 0)
        self.assertIn("--remove-checkout", result.stdout)
        self.assertIn("--keep-checkout", result.stdout)
        self.assertIn("--notebooklm-home PATH", result.stdout)

    def test_dry_run_uses_requested_config_and_notebooklm_paths(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            result = run_uninstaller(
                "--dry-run",
                "--no-prompt",
                "--keep-checkout",
                "--config-dir",
                "/tmp/custom-echoes-config",
                "--notebooklm-home",
                "/tmp/custom-notebooklm",
                env={"HOME": home},
            )

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("/tmp/custom-echoes-config", output)
        self.assertIn("/tmp/custom-notebooklm", output)
        self.assertIn("Keeping the echoes checkout", output)

    def test_managed_cron_block_removal_is_idempotent(self) -> None:
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            source {UNINSTALL}
            existing="$(cat <<'EOF'
MAILTO=user@example.com
# BEGIN ECHOES DAILY
# Managed by echoes install.sh. Re-run install.sh to update safely.
15 7 * * * cd '/srv/echoes' && ECHOES_ALLOW_UNSANDBOXED=1 ECHOES_CONFIG_DIR='/tmp/cfg' PATH='/usr/bin:/bin' '/srv/echoes/scripts/run_daily_codex.sh'
# END ECHOES DAILY
EOF
)"
            cleaned_once="$(printf '%s\\n' "$existing" | remove_managed_cron_block)"
            cleaned_twice="$(printf '%s\\n' "$cleaned_once" | remove_managed_cron_block)"
            test "$cleaned_once" = "$cleaned_twice"
            printf '%s\\n' "$cleaned_once"
            """
        )
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MAILTO=user@example.com", result.stdout)
        self.assertNotIn("# BEGIN ECHOES DAILY", result.stdout)

    def test_noninteractive_default_keeps_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            result = run_uninstaller(
                "--dry-run",
                "--no-prompt",
                env={"HOME": home},
            )

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("Keeping the echoes checkout", output)
        self.assertNotIn("Scheduled checkout removal", output)

    def test_remove_checkout_flag_schedules_checkout_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            result = run_uninstaller(
                "--dry-run",
                "--no-prompt",
                "--remove-checkout",
                env={"HOME": home},
            )

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("nohup bash -c", output)

    def test_missing_codex_degrades_gracefully(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            result = run_uninstaller(
                "--dry-run",
                "--no-prompt",
                "--keep-checkout",
                env={"HOME": home, "PATH": "/usr/bin:/bin"},
            )

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("skipping chrome-devtools mcp cleanup", output.lower())


if __name__ == "__main__":
    unittest.main()
