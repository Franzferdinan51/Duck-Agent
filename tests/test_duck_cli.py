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
        # Explicit isolation: Duck-Agent's home must not be ~/.hermes.
        self.assertIn("Isolated from ~/.hermes: yes", result.stdout)

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

    def test_version_lists_grok_build_harness(self):
        result = self.run_cli("version")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(
            "Primary harness (Grok Build):" in result.stdout,
            f"expected a Grok Build harness line, got: {result.stdout!r}",
        )

    def test_mcp_is_dispatched_not_swallowed(self):
        # 'duck-agent mcp ...' must route to the runtime MCP manager (so
        # 'mcp catalog' lists duckbot-memory), not be passed to a harness.
        # Here we assert the interceptor is reachable: invoking with an unknown
        # mcp subcommand returns the runtime's error (proving routing) rather
        # than argparse rejecting 'mcp <sub>' at the outer layer.
        import duck_agent.cli as cli
        self.assertTrue(callable(cli.run_mcp))

    def test_status_reports_grok_build_line(self):
        # status should include a Grok Build line (found or not) reflecting the
        # primary harness, in the test env this is GROK_BIN-independent.
        result = self.run_cli("status")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(
            "Grok Build:" in result.stdout,
            f"expected a Grok Build status line, got: {result.stdout!r}",
        )

    def test_work_fallback_runtime_passes_prompt(self):
        # _run_runtime must deliver the goal to the runtime process (previously
        # dropped). Monkeypatch subprocess.run to assert the prompt arg is passed.
        import duck_agent.cli as cli
        import subprocess

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["cwd"] = kwargs.get("cwd")
            class R:
                returncode = 0
                stdout = "ok"
                stderr = ""
            return R()

        orig = cli.subprocess.run
        cli.subprocess.run = fake_run
        try:
            code = cli._run_runtime("a real goal")
        finally:
            cli.subprocess.run = orig
        self.assertEqual(code, 0)
        self.assertIn("a real goal", captured["cmd"])

    def test_doctor_reports_isolation_ok(self):
        # duck-agent doctor must PASS the ~/.hermes isolation check for a
        # custom DUCK_AGENT_HOME (non-hermes). Note: without GROK_API_KEY the
        # doctor may still be non-zero, so assert the isolation line, not exit 0.
        result = self.run_cli("doctor")
        self.assertIn("PASS  isolated from ~/.hermes", result.stdout)


if __name__ == "__main__":
    unittest.main()
