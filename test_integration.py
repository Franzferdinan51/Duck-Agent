"""
Duck Agent - End-to-End Integration Tests

These tests verify the complete Duck Agent backend system works as expected,
from launcher script through backend initialization.
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path

DUCK_AGENT_DIR = Path(__file__).parent.resolve()
LAUNCHER = DUCK_AGENT_DIR / "duck-agent"
SCRATCH_DIR = Path("/var/folders/jq/chmpngcn7p16wwtscq81tl_m0000gn/T/grok-goal-f39b8233090a/implementer")


class TestEndToEndLauncher(unittest.TestCase):
    """End-to-end tests for the launcher script."""

    def test_help_exit_code_zero(self):
        """--help should exit with code 0."""
        result = subprocess.run(
            [str(LAUNCHER), "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("Duck Agent", result.stdout)

    def test_version_exit_code_zero(self):
        """--version should exit with code 0."""
        result = subprocess.run(
            [str(LAUNCHER), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("Duck Agent", result.stdout)

    def test_backends_list_all_three(self):
        """--backends should list all three backends."""
        result = subprocess.run(
            [str(LAUNCHER), "--backends"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("grok-build", result.stdout)
        self.assertIn("hermes-compatible", result.stdout)
        self.assertIn("prime-agent", result.stdout)

    def test_status_shows_current_backend(self):
        """--status should show the current backend."""
        env = os.environ.copy()
        env["DUCK_AGENT_BACKEND"] = "grok-build"
        result = subprocess.run(
            [str(LAUNCHER), "--status"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("grok-build", result.stdout)

    def test_status_with_hermes_backend(self):
        """--status should show the hermes-compatible backend when set."""
        env = os.environ.copy()
        env["DUCK_AGENT_BACKEND"] = "hermes-compatible"
        result = subprocess.run(
            [str(LAUNCHER), "--status"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("hermes-compatible", result.stdout)

    def test_status_with_prime_agent(self):
        """--status should show the prime-agent backend when set."""
        env = os.environ.copy()
        env["DUCK_AGENT_BACKEND"] = "prime-agent"
        result = subprocess.run(
            [str(LAUNCHER), "--status"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("prime-agent", result.stdout)

    def test_capture_output_to_file(self):
        """Capture launcher output to scratch directory for evidence."""
        SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        output_file = SCRATCH_DIR / "launcher_test_output.txt"

        result = subprocess.run(
            [str(LAUNCHER), "--backends"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        output_file.write_text(result.stdout)
        self.assertTrue(output_file.exists())
        self.assertGreater(output_file.stat().st_size, 0)


class TestNoHermesConflicts(unittest.TestCase):
    """Verify Duck Agent does not conflict with Hermes Agent."""

    def test_app_id_is_duck_agent(self):
        """The app ID should be com.duckagent.desktop, not com.nousresearch.hermes."""
        package_json = DUCK_AGENT_DIR / "apps/desktop/package.json"
        content = package_json.read_text()
        self.assertIn('"com.duckagent.desktop"', content)
        self.assertNotIn('"com.nousresearch.hermes"', content)

    def test_product_name_is_duck_agent(self):
        """The product name should be Duck Agent."""
        package_json = DUCK_AGENT_DIR / "apps/desktop/package.json"
        content = package_json.read_text()
        self.assertIn('"Duck Agent"', content)
        self.assertNotIn('"Hermes"', content)

    def test_protocol_scheme_is_duck_agent(self):
        """The protocol scheme should be duck-agent, not hermes."""
        package_json = DUCK_AGENT_DIR / "apps/desktop/package.json"
        content = package_json.read_text()
        self.assertIn('"duck-agent"', content)
        self.assertNotIn('"hermes"', content)

    def test_executable_name_is_duck(self):
        """The executable name should be DuckAgent, not Hermes."""
        package_json = DUCK_AGENT_DIR / "apps/desktop/package.json"
        content = package_json.read_text()
        self.assertIn('"DuckAgent"', content)
        self.assertNotIn('"Hermes"', content)


class TestMultiBackendIntegration(unittest.TestCase):
    """Test that all three backends can be initialized."""

    def test_grok_build_full_init(self):
        """Test full initialization of Grok Build backend."""
        env = os.environ.copy()
        env["DUCK_AGENT_BACKEND"] = "grok-build"
        result = subprocess.run(
            [str(LAUNCHER), "--status"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("Grok Build", result.stdout)

    def test_hermes_compatible_full_init(self):
        """Test full initialization of Hermes-compatible backend."""
        env = os.environ.copy()
        env["DUCK_AGENT_BACKEND"] = "hermes-compatible"
        result = subprocess.run(
            [str(LAUNCHER), "--status"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("Hermes", result.stdout)

    def test_prime_agent_full_init(self):
        """Test full initialization of Prime Agent backend."""
        env = os.environ.copy()
        env["DUCK_AGENT_BACKEND"] = "prime-agent"
        result = subprocess.run(
            [str(LAUNCHER), "--status"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("Prime Agent", result.stdout)


class TestBrandingAssets(unittest.TestCase):
    """Test that Duck Agent branding assets are in place."""

    def test_duck_logo_exists(self):
        """The duck logo should exist in the public folder."""
        logo = DUCK_AGENT_DIR / "apps/desktop/public/duck-logo.png"
        self.assertTrue(logo.exists())
        self.assertGreater(logo.stat().st_size, 0)

    def test_brand_mark_uses_duck_agent(self):
        """The brand mark component should reference the duck logo."""
        brand_mark = DUCK_AGENT_DIR / "apps/desktop/src/components/brand-mark.tsx"
        content = brand_mark.read_text()
        self.assertIn("duck-logo", content)
        self.assertIn("Duck Agent", content)

    def test_index_html_title_is_duck_agent(self):
        """The index.html title should be Duck Agent."""
        index_html = DUCK_AGENT_DIR / "apps/desktop/index.html"
        content = index_html.read_text()
        self.assertIn("Duck Agent", content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
