"""Installed Duck-Agent CLI entry point.

The repository has a legacy multi-package editable build; add the source root
explicitly so the console command works when launched from any directory.
"""
from __future__ import annotations
import sys
from pathlib import Path
_source_root = Path(__file__).resolve().parent
if str(_source_root) not in sys.path:
    sys.path.insert(0, str(_source_root))
from duck_agent.cli import main
if __name__ == '__main__':
    raise SystemExit(main())