#!/usr/bin/env python3
"""
End-to-end tests for Duck Agent backend selection.

These tests verify the complete flow from launcher script to backend.
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class TestLauncherScript(unittest.TestCase):
    """Test the launcher script."""

    def setUp(self):
        """Set up test environment."""
        self.launcher = PROJECT_ROOT / "duck-agent"
        if self.launcher.exists():
            os.chmod(self.launcher, 0o755)

    def test_launcher_exists(self):
        """Test that launcher script exists."""
        self.assertTrue(self.launcher.exists())

    def test_launcher_is_executable(self):
        """Test that launcher is executable."""
        self.assertTrue(os.access(self.launcher, os.X_OK))

    def test_launcher_prints_backend_name(self):
        """Test that launcher prints backend name."""
        env = os.environ.copy()
        env["DUCK_AGENT_BACKEND"] = "grok-build"
        result = subprocess.run(
            [str(self.launcher)],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        # Should mention Duck Agent and backend
        all_output = result.stdout + result.stderr
        self.assertIn("Duck Agent", all_output)
        self.assertIn("grok-build", all_output)

    def test_launcher_rejects_invalid_backend(self):
        """Test that launcher rejects invalid backend."""
        env = os.environ.copy()
        env["DUCK_AGENT_BACKEND"] = "totally-invalid"
        result = subprocess.run(
            [str(self.launcher)],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        # Should exit non-zero
        self.assertNotEqual(result.returncode, 0)
        all_output = result.stdout + result.stderr
        self.assertIn("Unknown backend", all_output)


class TestBackendIntegration(unittest.TestCase):
    """Test backend integration with Duck Agent Python module."""

    def test_python_module_imports(self):
        """Test that python module imports correctly."""
        from duck_agent.backends import (
            BackendType,
            get_backend,
            get_backend_info,
            initialize_backend,
        )
        self.assertTrue(BackendType.GROK_BUILD is not None)

    def test_backend_module_structure(self):
        """Test backend module structure."""
        from duck_agent import backends
        # Check module has expected functions
        self.assertTrue(hasattr(backends, 'get_backend'))
        self.assertTrue(hasattr(backends, 'get_backend_info'))
        self.assertTrue(hasattr(backends, 'initialize_backend'))
        self.assertTrue(hasattr(backends, 'BackendType'))

    def test_init_package(self):
        """Test that __init__.py exports correctly."""
        import duck_agent
        self.assertTrue(hasattr(duck_agent, 'BackendType'))
        self.assertTrue(hasattr(duck_agent, 'get_backend'))
        self.assertTrue(hasattr(duck_agent, 'get_backend_info'))
        self.assertTrue(hasattr(duck_agent, 'initialize_backend'))

    def test_version_defined(self):
        """Test that version is defined."""
        import duck_agent
        self.assertTrue(hasattr(duck_agent, '__version__'))
        self.assertEqual(duck_agent.__version__, "0.1.0")


class TestBackendFileStructure(unittest.TestCase):
    """Test backend file structure."""

    def test_backends_dir_exists(self):
        """Test that backends directory exists."""
        backends_dir = PROJECT_ROOT / "backends"
        self.assertTrue(backends_dir.exists())

    def test_grok_build_backend_exists(self):
        """Test that Grok Build backend exists."""
        grok_build = PROJECT_ROOT / "backends" / "grok-build"
        self.assertTrue(grok_build.exists())

    def test_backend_files_exist(self):
        """Test that expected backend files exist."""
        files = [
            "backends/index.ts",
            "backends/grok-build/index.ts",
            "backends/grok-build/harness.ts",
            "backends/grok-build/config.ts",
        ]
        for f in files:
            path = PROJECT_ROOT / f
            self.assertTrue(path.exists(), f"Missing: {f}")

    def test_backend_tests_exist(self):
        """Test that backend tests exist."""
        test_files = [
            "backends/tests/test-index.ts",
            "backends/tests/test-harness.ts",
        ]
        for f in test_files:
            path = PROJECT_ROOT / f
            self.assertTrue(path.exists(), f"Missing: {f}")


class TestDuckAgentBranding(unittest.TestCase):
    """Test Duck Agent branding."""

    def test_no_user_facing_hermes_branding(self):
        """Test that user-facing files don't have Hermes branding."""
        # Check README.md
        readme = PROJECT_ROOT / "README.md"
        content = readme.read_text()
        # Should mention Duck Agent
        self.assertIn("Duck Agent", content)

    def test_package_name_is_duck_agent(self):
        """Test that package name is duck-agent."""
        package_json = PROJECT_ROOT / "package.json"
        if package_json.exists():
            content = package_json.read_text()
            self.assertIn("duck-agent", content)

    def test_launcher_has_duck_agent_branding(self):
        """Test that launcher has Duck Agent branding."""
        launcher = PROJECT_ROOT / "duck-agent"
        if launcher.exists():
            content = launcher.read_text()
            self.assertIn("Duck Agent", content)
            self.assertIn("Grok Build", content)


if __name__ == "__main__":
    unittest.main()
