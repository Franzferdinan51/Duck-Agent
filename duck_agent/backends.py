"""
Duck Agent - Backend Selection Module

Handles backend selection and initialization for Duck Agent.
"""
import os
import sys
from enum import Enum
from typing import Optional

class BackendType(Enum):
    """Available Duck Agent backends."""
    GROK_BUILD = 'grok-build'
    HERMES_COMPATIBLE = 'duck-agent-compatible'
    PRIME_AGENT = 'prime-agent'

def get_backend() -> BackendType:
    """Get the configured backend type from environment."""
    backend = os.environ.get('DUCK_AGENT_BACKEND', 'grok-build')
    if not is_valid_backend(backend):
        print(f"Warning: Unknown backend '{backend}', defaulting to grok-build")
        return BackendType.GROK_BUILD
    return BackendType(backend)

def is_valid_backend(backend: str) -> bool:
    """Check if a backend string is a valid backend type."""
    return backend in [b.value for b in BackendType]

def get_backend_info() -> dict:
    """Get information about all available backends."""
    return {'grok-build': {'name': 'Grok Build', 'description': 'Primary harness with full Grok Build capabilities', 'recommended': True}, 'duck-agent-compatible': {'name': 'Duck Agent-Compatible', 'description': 'Duck Agent compatibility mode', 'recommended': False}, 'prime-agent': {'name': 'Prime Agent', 'description': 'Prime Intellect RLM-based agent', 'recommended': False}}

def print_backend_info():
    """Print information about available backends."""
    backends = get_backend_info()
    current = get_backend().value
    print('Duck Agent - Backend Selection')
    print('=' * 40)
    print()
    for key, info in backends.items():
        marker = ' [CURRENT]' if key == current else ''
        recommended = ' (Recommended)' if info.get('recommended') else ''
        print(f"  {info['name']}{recommended}{marker}")
        print(f"    {info['description']}")
        print()

def initialize_backend() -> str:
    """Initialize the configured backend and return status."""
    backend = get_backend()
    print(f'Initializing {backend.value} backend...')
    if backend == BackendType.GROK_BUILD:
        return initialize_grok_build()
    elif backend == BackendType.HERMES_COMPATIBLE:
        return initialize_hermes_compatible()
    elif backend == BackendType.PRIME_AGENT:
        return initialize_prime_agent()
    return 'Unknown backend'

def initialize_grok_build() -> str:
    """Initialize Grok Build harness."""
    print('  - Connecting to Grok Build API...')
    api_key = os.environ.get('GROK_API_KEY')
    if api_key:
        print('  - API key configured')
    else:
        print('  - Warning: GROK_API_KEY not set')
    return 'Grok Build initialized'

def initialize_hermes_compatible() -> str:
    """Initialize Duck Agent-compatible mode."""
    print('  - Loading Duck Agent protocol layer...')
    print('  - Connecting to Duck Agent backend...')
    return 'Duck Agent-compatible mode initialized'

def initialize_prime_agent() -> str:
    """Initialize Prime Agent backend."""
    print('  - Loading Prime Agent RLM...')
    print('  - Initializing IPython environment...')
    return 'Prime Agent initialized'

def handle_cli():
    """Handle command-line interface for the backend module."""
    if len(sys.argv) < 2:
        print_backend_info()
        initialize_backend()
        return
    command = sys.argv[1]
    if command == 'info':
        print_backend_info()
    elif command == 'start':
        if '--backend' in sys.argv:
            idx = sys.argv.index('--backend')
            if idx + 1 < len(sys.argv):
                os.environ['DUCK_AGENT_BACKEND'] = sys.argv[idx + 1]
        result = initialize_backend()
        print(f'\n[OK] {result}')
    elif command == 'status':
        backend = get_backend()
        info = get_backend_info()
        print(f'Current backend: {backend.value}')
        print(f"Name: {info[backend.value]['name']}")
        print(f"Description: {info[backend.value]['description']}")
    else:
        print(f'Unknown command: {command}')
        print('Available commands: info, start, status')
        sys.exit(1)
if __name__ == '__main__':
    handle_cli()