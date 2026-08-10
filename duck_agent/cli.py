"""Duck-Agent's Duck Agent-style, local-first command-line interface."""
from __future__ import annotations
import argparse
import os
import sys
from pathlib import Path
from . import __version__
from .backends import get_backend, get_backend_info
WORKFLOWS = {'plan': 'Turn a goal into an explicit, reviewable plan before execution.', 'research': 'Gather sources and preserve links/evidence for claims.', 'code': 'Run implementation through an executor with progress and verification.', 'operate': 'Inspect status, diagnose the environment, and perform bounded operations.'}
CAPABILITIES = {'planning': 'plan', 'research': 'research', 'coding': 'code', 'operations': 'operate', 'evidence': 'Every reported result distinguishes planned, running, reported, and verified.'}

def duck_home() -> Path:
    """Return Duck-Agent's isolated state directory.

    Defaults to ~/.duck-agent on macOS/Linux and %LOCALAPPDATA%\\duck-agent on
    Windows, mirroring the desktop runtime. Never inherits Hermes's home.
    """
    configured = os.environ.get('DUCK_AGENT_HOME')
    if configured:
        return Path(configured).expanduser()
    if os.name == 'nt':
        local = os.environ.get('LOCALAPPDATA')
        if local:
            return Path(local) / 'duck-agent'
    return Path.home() / '.duck-agent'

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='duck-agent', description='Duck-Agent — a local-first agent with Duck Agent-style CLI workflows.')
    parser.add_argument('--version', action='version', version=f'Duck-Agent {__version__}')
    parser.add_argument('--home', help='Duck-Agent state directory (default: ~/.duck-agent)')
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('chat', help='Start the interactive agent surface')
    sub.add_parser('status', help='Show backend and isolated state status')
    sub.add_parser('doctor', help='Check the local Duck-Agent installation')
    sub.add_parser('backends', help='List available model backends')
    sub.add_parser('workflows', help='List governed workflows')
    sub.add_parser('capabilities', help='List available capabilities')
    sub.add_parser('update', help='Update Duck-Agent from its own repository (Franzferdinan51/Duck-Agent)')
    return parser

def print_status() -> int:
    home = duck_home()
    backend = get_backend()
    info = get_backend_info()[backend.value]
    print('Duck-Agent status')
    print(f'Version: {__version__}')
    print(f"Backend: {backend.value} ({info['name']})")
    print(f'State home: {home}')
    print(f'Duck Agent home touched: no (Duck-Agent uses {home})')
    print(f"State initialized: {('yes' if home.exists() else 'no')}")
    return 0

def print_backends() -> int:
    for key, info in get_backend_info().items():
        marker = ' [recommended]' if info.get('recommended') else ''
        print(f"{key}{marker}: {info['description']}")
    return 0

def print_workflows() -> int:
    for name, description in WORKFLOWS.items():
        print(f'{name}: {description}')
    return 0

def print_capabilities() -> int:
    for name, value in CAPABILITIES.items():
        print(f'{name}: {value}')
    return 0

def print_doctor() -> int:
    home = duck_home()
    checks = [('isolated home', home != Path.home() / '.duck-agent'), ('Python', sys.version_info >= (3, 9)), ('repository state', Path.cwd().exists())]
    failed = False
    for name, passed in checks:
        print(f"{('PASS' if passed else 'FAIL')}  {name}")
        failed |= not passed
    if failed:
        print('Duck-Agent doctor found issues.')
        return 1
    print(f'PASS  state directory target: {home}')
    return 0

def run_chat() -> int:
    """Delegate to the Duck Agent-compatible CLI without inheriting Duck Agent storage."""
    os.environ.setdefault('DUCK_AGENT_HOME', str(duck_home()))
    try:
        from hermes_cli.main import main as hermes_main
    except ImportError as exc:
        print(f'Duck-Agent chat backend is unavailable: {exc}', file=sys.stderr)
        return 1
    hermes_main()
    return 0

DUCK_AGENT_REPO = 'https://github.com/Franzferdinan51/Duck-Agent.git'

def run_update(argv: list[str] | None = None) -> int:
    """Update Duck-Agent's own runtime repository.

    Works like ``hermes update`` — fetch + pull the active checkout — but it
    pulls **Duck-Agent's own** repository (Franzferdinan51/Duck-Agent), never
    the upstream Hermes repo. The runtime is expected to be a git checkout of
    this fork; if its origin points somewhere else we surface that rather than
    silently mutating a foreign tree.
    """
    import subprocess

    home = duck_home()
    runtime = home / 'runtime'

    if not (runtime / '.git').exists():
        print(f'Duck-Agent runtime is not a git checkout at {runtime}.', file=sys.stderr)
        print('Reinstall Duck-Agent from https://github.com/Franzferdinan51/Duck-Agent', file=sys.stderr)
        return 1

    origin = subprocess.run(['git', 'config', '--get', 'remote.origin.url'], cwd=runtime, capture_output=True, text=True)
    origin_url = (origin.stdout or '').strip()

    if origin_url and 'Duck-Agent' not in origin_url:
        print(f'Duck-Agent runtime origin is {origin_url!r}, which is not the Duck-Agent fork.', file=sys.stderr)
        print('Refusing to update a foreign checkout. Set origin to https://github.com/Franzferdinan51/Duck-Agent.git', file=sys.stderr)
        return 1

    print(f'Updating Duck-Agent runtime at {runtime}')
    for cmd in (['git', 'fetch', 'origin'], ['git', 'pull', '--ff-only', 'origin', 'main']):
        print('  $ ' + ' '.join(cmd))
        r = subprocess.run(cmd, cwd=runtime, capture_output=True, text=True, timeout=600)
        print(r.stdout, end='')
        if r.stderr:
            print(r.stderr, end='', file=sys.stderr)
        if r.returncode != 0:
            print(f'Duck-Agent update failed at `{" ".join(cmd)}`.', file=sys.stderr)
            return r.returncode
    print('Duck-Agent is up to date.')
    return 0

def main(argv: list[str] | None=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.home:
        os.environ['DUCK_AGENT_HOME'] = args.home
    command = args.command or 'chat'
    handlers = {'status': print_status, 'backends': print_backends, 'workflows': print_workflows, 'capabilities': print_capabilities, 'doctor': print_doctor, 'chat': run_chat, 'update': run_update}
    return handlers[command]()
if __name__ == '__main__':
    raise SystemExit(main())