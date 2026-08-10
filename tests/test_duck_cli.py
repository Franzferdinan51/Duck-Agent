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
        # Argparse should accept a real workflow + goal; reaching the harness
        # is out of scope here (needs the runtime env), so we just assert the
        # argument contract parses and it doesn't pass argparse.
        result = self.run_cli("work", "a goal", "--workflow", "research")
        self.assertNotIn("invalid choice", result.stderr)


if __name__ == "__main__":
    unittest.main()
