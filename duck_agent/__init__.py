"""
Duck Agent - AI Agent powered by Grok Build

A self-improving AI agent desktop application combining
Hermes Agent architecture with Grok Build capabilities.
"""

__version__ = "0.1.0"
__author__ = "Duck Agent Team"

from .backends import (
    BackendType,
    get_backend,
    get_backend_info,
    initialize_backend,
)

__all__ = [
    "BackendType",
    "get_backend",
    "get_backend_info", 
    "initialize_backend",
]
