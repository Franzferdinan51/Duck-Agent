#!/usr/bin/env python3
"""
Duck Agent Test Runner

Runs all tests for the Duck Agent project.
"""

import os
import sys
import unittest

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

# Discover and run all tests
def run_all_tests():
    """Discover and run all tests in the tests directory."""
    loader = unittest.TestLoader()
    start_dir = os.path.join(PROJECT_ROOT, "tests")
    suite = loader.discover(start_dir, pattern="test_*.py")

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(run_all_tests())
