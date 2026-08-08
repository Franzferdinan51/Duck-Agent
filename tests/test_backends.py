#!/usr/bin/env python3
"""
Unit tests for Duck Agent backend selection module.
"""

import os
import sys
import unittest
from unittest.mock import patch

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from duck_agent.backends import (
    BackendType,
    get_backend,
    get_backend_info,
    initialize_backend,
    is_valid_backend,
    print_backend_info,
)


class TestBackendType(unittest.TestCase):
    """Test BackendType enum."""

    def test_backend_values(self):
        """Test that all expected backend values exist."""
        self.assertEqual(BackendType.GROK_BUILD.value, "grok-build")
        self.assertEqual(BackendType.HERMES_COMPATIBLE.value, "hermes-compatible")
        self.assertEqual(BackendType.PRIME_AGENT.value, "prime-agent")

    def test_backend_count(self):
        """Test that exactly 3 backends are defined."""
        self.assertEqual(len(list(BackendType)), 3)


class TestGetBackend(unittest.TestCase):
    """Test get_backend function."""

    def setUp(self):
        """Clean up environment variables."""
        if "DUCK_AGENT_BACKEND" in os.environ:
            del os.environ["DUCK_AGENT_BACKEND"]

    def test_default_backend(self):
        """Test that default backend is grok-build."""
        backend = get_backend()
        self.assertEqual(backend, BackendType.GROK_BUILD)

    def test_grok_build_backend(self):
        """Test setting Grok Build backend."""
        os.environ["DUCK_AGENT_BACKEND"] = "grok-build"
        backend = get_backend()
        self.assertEqual(backend, BackendType.GROK_BUILD)

    def test_hermes_compatible_backend(self):
        """Test setting Hermes-compatible backend."""
        os.environ["DUCK_AGENT_BACKEND"] = "hermes-compatible"
        backend = get_backend()
        self.assertEqual(backend, BackendType.HERMES_COMPATIBLE)

    def test_prime_agent_backend(self):
        """Test setting Prime Agent backend."""
        os.environ["DUCK_AGENT_BACKEND"] = "prime-agent"
        backend = get_backend()
        self.assertEqual(backend, BackendType.PRIME_AGENT)

    def test_invalid_backend_defaults_to_grok_build(self):
        """Test that invalid backend falls back to grok-build."""
        os.environ["DUCK_AGENT_BACKEND"] = "invalid-backend"
        backend = get_backend()
        self.assertEqual(backend, BackendType.GROK_BUILD)


class TestIsValidBackend(unittest.TestCase):
    """Test is_valid_backend function."""

    def test_valid_backends(self):
        """Test valid backend names."""
        self.assertTrue(is_valid_backend("grok-build"))
        self.assertTrue(is_valid_backend("hermes-compatible"))
        self.assertTrue(is_valid_backend("prime-agent"))

    def test_invalid_backends(self):
        """Test invalid backend names."""
        self.assertFalse(is_valid_backend("invalid"))
        self.assertFalse(is_valid_backend(""))
        self.assertFalse(is_valid_backend("hermes"))  # Without -compatible
        self.assertFalse(is_valid_backend("GROK-BUILD"))  # Case sensitive


class TestGetBackendInfo(unittest.TestCase):
    """Test get_backend_info function."""

    def test_returns_all_backends(self):
        """Test that all backends are returned."""
        info = get_backend_info()
        self.assertIn("grok-build", info)
        self.assertIn("hermes-compatible", info)
        self.assertIn("prime-agent", info)

    def test_grok_build_recommended(self):
        """Test that grok-build is marked as recommended."""
        info = get_backend_info()
        self.assertTrue(info["grok-build"].get("recommended"))

    def test_backend_info_structure(self):
        """Test that each backend has required fields."""
        info = get_backend_info()
        for backend_id, backend_info in info.items():
            self.assertIn("name", backend_info)
            self.assertIn("description", backend_info)
            self.assertIsInstance(backend_info["name"], str)
            self.assertIsInstance(backend_info["description"], str)


class TestInitializeBackend(unittest.TestCase):
    """Test initialize_backend function."""

    def setUp(self):
        """Clean up environment variables."""
        if "DUCK_AGENT_BACKEND" in os.environ:
            del os.environ["DUCK_AGENT_BACKEND"]
        if "GROK_API_KEY" in os.environ:
            del os.environ["GROK_API_KEY"]

    def test_initialize_grok_build(self):
        """Test initializing Grok Build backend."""
        result = initialize_backend()
        self.assertIn("Grok Build", result)

    def test_initialize_hermes_compatible(self):
        """Test initializing Hermes-compatible backend."""
        os.environ["DUCK_AGENT_BACKEND"] = "hermes-compatible"
        result = initialize_backend()
        self.assertIn("Hermes", result)

    def test_initialize_prime_agent(self):
        """Test initializing Prime Agent backend."""
        os.environ["DUCK_AGENT_BACKEND"] = "prime-agent"
        result = initialize_backend()
        self.assertIn("Prime", result)

    def test_initialize_with_api_key(self):
        """Test initializing with API key."""
        os.environ["GROK_API_KEY"] = "test-key"
        with patch("builtins.print") as mock_print:
            initialize_backend()
            # Check that API key was mentioned
            mock_print.assert_called()


class TestPrintBackendInfo(unittest.TestCase):
    """Test print_backend_info function."""

    def test_print_runs_without_error(self):
        """Test that print_backend_info executes successfully."""
        with patch("builtins.print") as mock_print:
            print_backend_info()
            # Should have printed multiple lines
            self.assertGreater(mock_print.call_count, 5)

    def test_print_shows_current_backend(self):
        """Test that print shows current backend."""
        os.environ["DUCK_AGENT_BACKEND"] = "hermes-compatible"
        with patch("builtins.print") as mock_print:
            print_backend_info()
            # Join all printed strings
            all_prints = " ".join(str(call) for call in mock_print.call_args_list)
            # Should mention Hermes-compatible or current
            self.assertTrue("Hermes-Compatible" in all_prints or "CURRENT" in all_prints)


if __name__ == "__main__":
    unittest.main()
