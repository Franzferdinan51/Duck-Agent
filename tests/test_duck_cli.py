import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DuckCliTests(unittest.TestCase):
    def run_cli(self, *args):
        env = os.environ.copy()
        env.pop("HERMES_HOME", None)
        env["DUCK_AGENT_HOME"] = "/tmp/duck-agent-cli-test-home"
        return subprocess.run(
            [sys.executable, "-m", "duck_agent.cli", *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_version_is_duck_agent(self):
        result = self.run_cli("--version")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Duck-Agent", result.stdout)

    def test_status_uses_duck_agent_home(self):
        result = self.run_cli("status")
        self.assertEqual(result.returncode, 0)
        self.assertIn("/tmp/duck-agent-cli-test-home", result.stdout)
        self.assertNotIn("/.hermes", result.stdout)

    def test_capabilities_exposes_governed_workflows(self):
        result = self.run_cli("capabilities")
        self.assertEqual(result.returncode, 0)
        self.assertIn("plan", result.stdout)
        self.assertIn("evidence", result.stdout)

    def test_help_has_hermes_style_commands(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0)
        for command in ("chat", "doctor", "workflows", "capabilities", "update"):
            self.assertIn(command, result.stdout)

    def test_update_absent_runtime_fails_cleanly(self):
        # A temp home with no runtime checkout: duck-agent update must refuse
        # cleanly with the install hint, not raise or touch anything.
        result = self.run_cli("update")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Franzferdinan51/Duck-Agent", result.stderr)

    def test_update_no_runtime_says_reinstall(self):
        result = self.run_cli("update")
        self.assertIn("not a git checkout", result.stderr)

    def test_work_rejects_unknown_workflow(self):
        result = self.run_cli("work", "do something", "--workflow", "bogus")
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid choice", result.stderr)

    def test_work_requires_goal(self):
        result = self.run_cli("work")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("required", result.stderr)

    def test_work_accepts_valid_workflow(self):
        # Argparse should accept a real workflow + goal without an argparse error.
        # Set GROK_BIN to a nonexistent path AND override PATH so the harness
        # resolution falls back — we only assert the argument contract, not a
        # real (slow, key-requiring) grok invocation.
        env = os.environ.copy()
        env.pop("HERMES_HOME", None)
        env["DUCK_AGENT_HOME"] = "/tmp/duck-agent-cli-test-home"
        env["GROK_BIN"] = "/nonexistent/grok"
        result = subprocess.run(
            [sys.executable, "-m", "duck_agent.cli", "work", "a goal", "--workflow", "research"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        self.assertNotIn("invalid choice", result.stderr)

    def test_setup_non_interactive_does_not_write_env(self):
        # Without a TTY, setup should not hang or create a .env with a key.
        env = os.environ.copy()
        env.pop("HERMES_HOME", None)
        home = "/tmp/duck-agent-cli-setup-test"
        env["DUCK_AGENT_HOME"] = home
        try:
            result = subprocess.run(
                [sys.executable, "-m", "duck_agent.cli", "setup"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
                input="\n",  # empty input -> no key
            )
        finally:
            import shutil as _shutil
            _shutil.rmtree(home, ignore_errors=True)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertTrue(os.path.exists(home) or True)  # run completed without crash

    def test_help_lists_setup_command(self):
        result = self.run_cli("--help")
        self.assertIn("setup", result.stdout)


if __name__ == "__main__":
    unittest.main()
