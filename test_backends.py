"""
Duck Agent - Backend System Tests

Tests the backend selection and initialization system for Duck Agent.
These tests verify the actual shipped functionality, not a mock.
"""
import os
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from duck_agent.backends import BackendType, get_backend, get_backend_info, initialize_backend, is_valid_backend

class TestBackendType(unittest.TestCase):
    """Test BackendType enum."""

    def test_all_backends_defined(self):
        """All three backends must be defined."""
        backends = [b.value for b in BackendType]
        self.assertEqual(len(backends), 3)
        self.assertIn('grok-build', backends)
        self.assertIn('duck-agent-compatible', backends)
        self.assertIn('prime-agent', backends)

    def test_grok_build_is_default(self):
        """Grok Build should be the default backend."""
        self.assertEqual(BackendType.GROK_BUILD.value, 'grok-build')

class TestGetBackend(unittest.TestCase):
    """Test get_backend function."""

    def setUp(self):
        """Clear environment variable before each test."""
        if 'DUCK_AGENT_BACKEND' in os.environ:
            del os.environ['DUCK_AGENT_BACKEND']

    def test_default_backend_is_grok_build(self):
        """When no env var is set, default to grok-build."""
        result = get_backend()
        self.assertEqual(result, BackendType.GROK_BUILD)

    def test_explicit_grok_build(self):
        """Test setting grok-build explicitly."""
        os.environ['DUCK_AGENT_BACKEND'] = 'grok-build'
        result = get_backend()
        self.assertEqual(result, BackendType.GROK_BUILD)

    def test_explicit_hermes_compatible(self):
        """Test setting duck-agent-compatible."""
        os.environ['DUCK_AGENT_BACKEND'] = 'duck-agent-compatible'
        result = get_backend()
        self.assertEqual(result, BackendType.HERMES_COMPATIBLE)

    def test_explicit_prime_agent(self):
        """Test setting prime-agent."""
        os.environ['DUCK_AGENT_BACKEND'] = 'prime-agent'
        result = get_backend()
        self.assertEqual(result, BackendType.PRIME_AGENT)

    def test_invalid_backend_falls_back_to_default(self):
        """Invalid backend should fall back to grok-build."""
        os.environ['DUCK_AGENT_BACKEND'] = 'invalid-backend'
        result = get_backend()
        self.assertEqual(result, BackendType.GROK_BUILD)

class TestIsValidBackend(unittest.TestCase):
    """Test is_valid_backend function."""

    def test_valid_backends(self):
        """All three backends should be valid."""
        self.assertTrue(is_valid_backend('grok-build'))
        self.assertTrue(is_valid_backend('duck-agent-compatible'))
        self.assertTrue(is_valid_backend('prime-agent'))

    def test_invalid_backends(self):
        """Invalid backends should be rejected."""
        self.assertFalse(is_valid_backend('invalid'))
        self.assertFalse(is_valid_backend(''))
        self.assertFalse(is_valid_backend('duck-agent'))
        self.assertFalse(is_valid_backend('grok'))

class TestGetBackendInfo(unittest.TestCase):
    """Test get_backend_info function."""

    def test_returns_all_backends(self):
        """get_backend_info should return info for all backends."""
        info = get_backend_info()
        self.assertIn('grok-build', info)
        self.assertIn('duck-agent-compatible', info)
        self.assertIn('prime-agent', info)

    def test_grok_build_recommended(self):
        """Grok Build should be marked as recommended."""
        info = get_backend_info()
        self.assertTrue(info['grok-build']['recommended'])

    def test_all_backends_have_name(self):
        """Each backend should have a name."""
        info = get_backend_info()
        for key in ['grok-build', 'duck-agent-compatible', 'prime-agent']:
            self.assertIn('name', info[key])
            self.assertIn('description', info[key])
            self.assertGreater(len(info[key]['name']), 0)

class TestInitializeBackend(unittest.TestCase):
    """Test initialize_backend function."""

    def setUp(self):
        """Clear environment variable before each test."""
        if 'DUCK_AGENT_BACKEND' in os.environ:
            del os.environ['DUCK_AGENT_BACKEND']

    def test_initialize_grok_build(self):
        """Initialize grok-build backend."""
        os.environ['DUCK_AGENT_BACKEND'] = 'grok-build'
        result = initialize_backend()
        self.assertIn('Grok Build', result)

    def test_initialize_hermes_compatible(self):
        """Initialize duck-agent-compatible backend."""
        os.environ['DUCK_AGENT_BACKEND'] = 'duck-agent-compatible'
        result = initialize_backend()
        self.assertIn('Duck Agent-compatible', result)

    def test_initialize_prime_agent(self):
        """Initialize prime-agent backend."""
        os.environ['DUCK_AGENT_BACKEND'] = 'prime-agent'
        result = initialize_backend()
        self.assertIn('Prime Agent', result)

class TestBackendIntegration(unittest.TestCase):
    """Integration tests for the backend system."""

    def test_full_workflow_default(self):
        """Full workflow with default backend."""
        if 'DUCK_AGENT_BACKEND' in os.environ:
            del os.environ['DUCK_AGENT_BACKEND']
        backend = get_backend()
        info = get_backend_info()
        self.assertEqual(backend.value, 'grok-build')
        self.assertIn('Grok Build', info['grok-build']['name'])

    def test_full_workflow_custom(self):
        """Full workflow with custom backend."""
        os.environ['DUCK_AGENT_BACKEND'] = 'prime-agent'
        backend = get_backend()
        self.assertEqual(backend.value, 'prime-agent')
if __name__ == '__main__':
    unittest.main(verbosity=2)