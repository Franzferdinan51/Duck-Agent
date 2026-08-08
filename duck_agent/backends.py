"""
Duck Agent - Backend Selection Module

Handles backend selection and initialization for Duck Agent.
"""

import os
from enum import Enum
from typing import Optional


class BackendType(Enum):
    """Available Duck Agent backends."""
    GROK_BUILD = "grok-build"
    HERMES_COMPATIBLE = "hermes-compatible"
    PRIME_AGENT = "prime-agent"


def get_backend() -> BackendType:
    """Get the configured backend type from environment."""
    backend = os.environ.get("DUCK_AGENT_BACKEND", "grok-build")
    
    # Validate backend
    valid_backends = [b.value for b in BackendType]
    if backend not in valid_backends:
        print(f"Warning: Unknown backend '{backend}', defaulting to grok-build")
        return BackendType.GROK_BUILD
    
    return BackendType(backend)


def get_backend_info() -> dict:
    """Get information about all available backends."""
    return {
        "grok-build": {
            "name": "Grok Build",
            "description": "Primary harness with full Grok Build capabilities",
            "recommended": True,
        },
        "hermes-compatible": {
            "name": "Hermes-Compatible",
            "description": "Hermes Agent compatibility mode",
            "recommended": False,
        },
        "prime-agent": {
            "name": "Prime Agent",
            "description": "Prime Intellect RLM-based agent",
            "recommended": False,
        },
    }


def print_backend_info():
    """Print information about available backends."""
    backends = get_backend_info()
    current = get_backend().value
    
    print("Duck Agent - Backend Selection")
    print("=" * 40)
    print()
    
    for key, info in backends.items():
        marker = " [CURRENT]" if key == current else ""
        recommended = " (Recommended)" if info.get("recommended") else ""
        print(f"  {info['name']}{recommended}{marker}")
        print(f"    {info['description']}")
        print()


def initialize_backend() -> str:
    """Initialize the configured backend and return status."""
    backend = get_backend()
    
    print(f"Initializing {backend.value} backend...")
    
    # Backend-specific initialization would happen here
    if backend == BackendType.GROK_BUILD:
        return initialize_grok_build()
    elif backend == BackendType.HERMES_COMPATIBLE:
        return initialize_hermes_compatible()
    elif backend == BackendType.PRIME_AGENT:
        return initialize_prime_agent()
    
    return "Unknown backend"


def initialize_grok_build() -> str:
    """Initialize Grok Build harness."""
    print("  - Connecting to Grok Build API...")
    api_key = os.environ.get("GROK_API_KEY")
    if api_key:
        print("  - API key configured")
    else:
        print("  - Warning: GROK_API_KEY not set")
    return "Grok Build initialized"


def initialize_hermes_compatible() -> str:
    """Initialize Hermes-compatible mode."""
    print("  - Loading Hermes protocol layer...")
    print("  - Connecting to Hermes backend...")
    return "Hermes-compatible mode initialized"


def initialize_prime_agent() -> str:
    """Initialize Prime Agent backend."""
    print("  - Loading Prime Agent RLM...")
    print("  - Initializing IPython environment...")
    return "Prime Agent initialized"


if __name__ == "__main__":
    print_backend_info()
    initialize_backend()
