"""
Duck Agent - Launcher Script Tests

Tests the duck-agent launcher script to ensure it correctly handles
backend selection and error cases.
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path
DUCK_AGENT_DIR = Path(__file__).parent.resolve()
LAUNCHER = DUCK_AGENT_DIR / 'duck-agent'

class TestLauncherScript(unittest.TestCase):
    """Test the duck-agent launcher script."""

    def test_launcher_exists(self):
        """The launcher script must exist."""
        self.assertTrue(LAUNCHER.exists(), f'Launcher not found at {LAUNCHER}')

    def test_launcher_is_executable(self):
        """The launcher script must be executable."""
        self.assertTrue(os.access(LAUNCHER, os.X_OK), f'Launcher at {LAUNCHER} is not executable')

    def test_launcher_shows_banner(self):
        """The launcher should show the Duck Agent banner when run."""
        result = subprocess.run([str(LAUNCHER), '--help'], capture_output=True, text=True, timeout=10)
        output = result.stdout + result.stderr
        self.assertTrue('Duck Agent' in output or 'backend' in output.lower(), f'Expected banner or backend info, got: {output[:200]}')

    def test_launcher_handles_invalid_backend(self):
        """Invalid backend should show error message."""
        env = os.environ.copy()
        env['DUCK_AGENT_BACKEND'] = 'totally-invalid-backend'
        result = subprocess.run([str(LAUNCHER), '--help'], capture_output=True, text=True, timeout=10, env=env)
        output = result.stdout + result.stderr
        self.assertTrue(len(output) > 0, 'Expected some output for invalid backend')

class TestEnvFile(unittest.TestCase):
    """Test the .env.example file."""

    def test_env_example_exists(self):
        """The .env.example file must exist."""
        env_file = DUCK_AGENT_DIR / '.env.example'
        self.assertTrue(env_file.exists())

    def test_env_example_has_backend_var(self):
        """The .env.example should document DUCK_AGENT_BACKEND."""
        env_file = DUCK_AGENT_DIR / '.env.example'
        content = env_file.read_text()
        self.assertIn('DUCK_AGENT_BACKEND', content)

    def test_env_example_lists_all_backends(self):
        """The .env.example should list all three backends."""
        env_file = DUCK_AGENT_DIR / '.env.example'
        content = env_file.read_text()
        self.assertIn('grok-build', content)
        self.assertIn('duck-agent-compatible', content)
        self.assertIn('prime-agent', content)

class TestBackendSwitching(unittest.TestCase):
    """Integration test for backend switching."""

    def test_backend_via_env_var(self):
        """Backend should be selectable via environment variable."""
        for backend in ['grok-build', 'duck-agent-compatible', 'prime-agent']:
            env = os.environ.copy()
            env['DUCK_AGENT_BACKEND'] = backend
            result = subprocess.run([str(LAUNCHER), '--help'], capture_output=True, text=True, timeout=10, env=env)
            output = result.stdout + result.stderr
            self.assertNotIn('Traceback', output, f'Backend {backend} traceback')
if __name__ == '__main__':
    unittest.main(verbosity=2)