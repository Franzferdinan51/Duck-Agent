"""Duck-Agent's Duck Agent-style, local-first command-line interface."""
from __future__ import annotations
import argparse
import os
import subprocess
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
    work = sub.add_parser('work', help='Run a governed OMH-style workflow (plan|research|code|operate) through the primary harness')
    work.add_argument('goal', help='The goal to achieve')
    work.add_argument('--workflow', '-w', default='code', choices=list(WORKFLOWS), help='Workflow to run (default: code)')
    sub.add_parser('setup', help='Configure the primary harness (Grok Build API key) and local providers')
    sub.add_parser('version', help='Show Duck-Agent and primary-harness (Grok Build) versions')
    sub.add_parser('mcp', help='Manage MCP servers (catalog, list, install, remove) — includes duckbot-memory')
    return parser

def run_version() -> int:
    """Show Duck-Agent and the primary harness (Grok Build) version."""
    print(f'Duck-Agent {__version__}')
    grok = _grok_version()
    if grok:
        print(f'Primary harness (Grok Build): {grok}')
    else:
        print('Primary harness (Grok Build): not found')
    return 0


def print_status() -> int:
    home = duck_home()
    backend = get_backend()
    info = get_backend_info()[backend.value]
    print('Duck-Agent status')
    print(f'Version: {__version__}')
    print(f"Backend: {backend.value} ({info['name']})")
    grok = _grok_version()
    if grok:
        print(f'Grok Build: {grok}')
    else:
        print('Grok Build: not found')
    print(f'State home: {home}')
    print(f'Isolated from ~/.hermes: {("yes" if home != Path.home() / ".hermes" else "no")}')
    print(f"State initialized: {('yes' if home.exists() else 'no')}")
    return 0


def _grok_version() -> str | None:
    """Return the installed Grok Build binary's version, or None."""
    import shutil
    import subprocess

    exe = os.environ.get('GROK_BIN') or shutil.which('grok')
    if not exe:
        return None
    try:
        r = subprocess.run([exe, '--version'], capture_output=True, text=True, timeout=15)
        version = (r.stdout or r.stderr or '').strip()
        return version.splitlines()[0] if version else None
    except Exception:  # noqa: BLE001
        return None

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
    import shutil

    home = duck_home()
    grok_exe = os.environ.get('GROK_BIN') or shutil.which('grok')
    grok_key = bool(os.environ.get('GROK_API_KEY') or _env_key_from_file(home))
    grok_runs = bool(grok_exe and _grok_version())
    checks = [
        ('isolated from ~/.hermes', home != Path.home() / '.hermes'),
        ('Python', sys.version_info >= (3, 9)),
        ('repository state', Path.cwd().exists()),
        ('Grok Build primary harness', bool(grok_exe)),
        ('Grok Build runs (grok --version)', grok_runs),
        ('GROK_API_KEY configured', grok_key),
    ]
    failed = False
    for name, passed in checks:
        print(f"{('PASS' if passed else 'FAIL')}  {name}")
        failed |= not passed
    if not grok_exe:
        print('  hint: install Grok Build (https://github.com/xai-org/grok-build) to enable the primary harness.', file=sys.stderr)
    elif not grok_key:
        print('  hint: run `duck-agent setup` to configure GROK_API_KEY.', file=sys.stderr)
    if failed:
        print('Duck-Agent doctor found issues.')
        return 1
    print(f'PASS  state directory target: {home}')
    return 0


def _env_key_from_file(home: Path) -> str | None:
    """Read GROK_API_KEY from ~/.duck-agent/.env if present (secrets file)."""
    env_file = home / '.env'
    if not env_file.exists():
        return None
    try:
        for line in env_file.read_text().splitlines():
            if line.startswith('GROK_API_KEY=') and len(line) > len('GROK_API_KEY='):
                return line[len('GROK_API_KEY='):]
    except Exception:  # noqa: BLE001
        return None
    return None

def run_chat() -> int:
    """Start the interactive agent surface through the primary harness.

    Grok Build (the installed ``grok`` binary) is Duck-Agent's primary harness
    per AGENTS.md. When present, ``chat`` launches the Grok Build TUI so agent
    work runs through the primary harness. Falls back to the Hermes-derived
    runtime's chat only when grok is unavailable.
    """
    import subprocess

    harness = _resolve_harness()

    if harness['kind'] == 'grok':
        print(f'Duck-Agent · chat via {harness["name"]}')
        # Launch the grok TUI interactively (no --single); hand over stdin.
        try:
            return subprocess.call(['grok'], cwd=os.getcwd())
        except FileNotFoundError as exc:
            print(f'Grok Build is on PATH but could not start: {exc}', file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            return 130

    # Fallback: Hermes-derived runtime chat (no grok binary installed).
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

def run_work(goal: str, workflow: str = 'code') -> int:
    """Run a governed workflow step with OMH-style evidence status.

    This folds oh-my-hermes' core operating model into the Duck-Agent CLI:
    a request becomes an explicit capability, executed through the primary
    harness, with status reported as stage+evidence rather than a naked
    \"done\" claim. Work is NEVER reported as verified unless the underlying
    run actually completed.
    """
    import time

    if workflow not in WORKFLOWS:
        print(f'Unknown workflow: {workflow!r}. Choosely one of: {", ".join(WORKFLOWS)}', file=sys.stderr)
        return 2

    print(f'Duck-Agent · {workflow.capitalize()} · {goal}')
    print(f'  Plan · not run   (workflow: {workflow}, goal framed, nothing executed yet)')

    # Frame the goal as a governed workflow prompt. Status is honest: we report
    # what the harness did, never upgraded to verified unless it completed.
    framed = (
        f'[Duck-Agent {workflow} workflow]\n'
        f'Goal: {goal}\n'
        'Treat this as a governed task: explain the plan, do the work, '
        'then report what actually ran. Do not claim verification you did not perform.'
    )

    execution = _resolve_harness()
    started = time.time()
    print(f'  {workflow.capitalize()} · running   (harness: {execution["name"]})')
    try:
        if execution['kind'] == 'grok':
            code = _run_grok_single(framed, cwd=os.getcwd())
        else:
            code = _run_runtime(framed)
    except Exception as exc:  # noqa: BLE001
        print(f'  {workflow.capitalize()} · failed     ({exc})', file=sys.stderr)
        return 1
    elapsed = round(time.time() - started, 1)

    if code == 0:
        print(f'  {workflow.capitalize()} · reported done  (took {elapsed}s; not independently verified)')
    else:
        print(f'  {workflow.capitalize()} · failed     (harness exited {code} after {elapsed}s)', file=sys.stderr)
        return code
    return 0


def _resolve_harness():
    """Pick the primary harness: Grok Build (the grok binary) first, else the
    Hermes-derived runtime. Grok Build is the product's primary harness."""
    import shutil

    grok_exe = shutil.which('grok') or os.environ.get('GROK_BIN')
    if grok_exe:
        return {'kind': 'grok', 'name': f'Grok Build ({grok_exe})', 'exe': grok_exe}
    return {'kind': 'runtime', 'name': 'Duck-Agent runtime'}


def _run_grok_single(prompt: str, cwd: str | None = None) -> int:
    """Run a single headless Grok Build turn (grok --single), printing its output."""
    import subprocess

    exe = os.environ.get('GROK_BIN') or 'grok'
    result = subprocess.run(
        [exe, '--single', prompt],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr)
    return result.returncode


def _run_runtime(prompt: str) -> int:
    """Fallback: run a single goal through the Hermes-derived runtime.

    Invokes the runtime as a subprocess so the goal is actually delivered
    (the runtime's main() reads sys.argv/stdin rather than accepting a
    positional argument), mirroring _run_grok_single. Returns the exit code.
    """
    import shutil
    import subprocess

    os.environ.setdefault('DUCK_AGENT_HOME', str(duck_home()))

    # Prefer the environment's python that has hermes_cli importable, then the
    # runtime venv next to this repo, then plain python3 (may lack hermes_cli).
    python_exe = None
    runtime_venv = Path(__file__).resolve().parent.parent / 'venv' / 'bin' / 'python'
    candidates = [runtime_venv if runtime_venv.exists() else None]
    configured = os.environ.get('DUCK_AGENT_HOME')
    if configured:
        alt = Path(configured) / 'runtime' / 'venv' / 'bin' / 'python'
        if alt.exists():
            candidates.insert(0, alt)
    for cand in candidates:
        if cand:
            python_exe = str(cand)
            break
    python_exe = python_exe or shutil.which('python3') or 'python3'

    r = subprocess.run(
        [python_exe, '-m', 'hermes_cli.main', prompt],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if r.stdout:
        print(r.stdout.rstrip())
    if r.stderr:
        print(r.stderr.rstrip(), file=sys.stderr)
    return r.returncode


def run_setup() -> int:
    """Interactively configure Duck-Agent's primary harness.

    Detects the installed Grok Build binary, checks for GROK_API_KEY, and lets
    the user paste it (stored only in ~/.duck-agent/.env — never printed,
    never committed). Also notes local-provider setup. This is the guided path
    to making ``duck-agent work`` actually complete turns.
    """
    import getpass

    home = duck_home()
    env_file = home / '.env'
    grok_key = os.environ.get('GROK_API_KEY', '')
    harness = _resolve_harness()

    print(f'Duck-Agent setup · state home: {home}')
    print(f'Primary harness: {harness["name"]}')
    print(f'GROK_API_KEY currently set: {("yes" if grok_key else "no")}')

    if harness['kind'] != 'grok':
        print('  (Grok Build binary not on PATH; setup will still write GROK_API_KEY)')

    if grok_key:
        print('GROK_API_KEY is already configured. Nothing to do.')
        return 0

    home.mkdir(parents=True, exist_ok=True)
    print('Paste your xAI Grok Build API key (https://console.x.ai) — input is hidden:')
    try:
        user_key = getpass.getpass('GROK_API_KEY: ').strip()
    except Exception:  # e.g. no TTY; fall back to a stable placeholder path
        print('(no interactive prompt available; leaving GROK_API_KEY unset)', file=sys.stderr)
        return 1

    if not user_key:
        print('No key entered; leaving GROK_API_KEY unset.', file=sys.stderr)
        return 1

    # Write/merge into the .env (secrets file, not tracked by git).
    existing = ''
    if env_file.exists():
        existing = env_file.read_text()
    lines = [ln for ln in existing.splitlines() if not ln.startswith('GROK_API_KEY=')]
    lines.append(f'GROK_API_KEY={user_key}')
    env_file.write_text('\n'.join(lines) + '\n')
    try:
        os.chmod(env_file, 0o600)
    except Exception:
        pass
    print(f'GROK_API_KEY saved to {env_file} (mode 600, not committed).')
    print("You can now run: duck-agent work '<your goal>'")
    return 0


def run_mcp(argv: list[str]) -> int:
    """Delegate `mcp <...>` to the runtime's MCP manager.

    The runtime (hermes_cli) owns the MCP catalog/install surface, including
    duckbot-memory. We hand `mcp catalog|list|install|remove|...` through so
    our CLI is a thin, consistent front for it and the subcommand isn't
    swallowed by the grok-backend wrapper in the launcher.
    """
    import shutil
    import subprocess

    os.environ.setdefault('DUCK_AGENT_HOME', str(duck_home()))

    python_exe = None
    runtime_venv = Path(__file__).resolve().parent.parent / 'venv' / 'bin' / 'python'
    if runtime_venv.exists():
        python_exe = str(runtime_venv)
    else:
        configured = os.environ.get('DUCK_AGENT_HOME')
        if configured:
            alt = Path(configured) / 'runtime' / 'venv' / 'bin' / 'python'
            if alt.exists():
                python_exe = str(alt)
    # Fall back to the default isolated runtime home (~/.duck-agent/runtime).
    if not python_exe:
        default = Path.home() / '.duck-agent' / 'runtime' / 'venv' / 'bin' / 'python'
        if default.exists():
            python_exe = str(default)
    python_exe = python_exe or shutil.which('python3') or 'python3'

    r = subprocess.run([python_exe, '-m', 'hermes_cli.main', 'mcp', *argv], capture_output=True, text=True)
    if r.stdout:
        print(r.stdout.rstrip())
    if r.stderr:
        print(r.stderr.rstrip(), file=sys.stderr)
    return r.returncode


def main(argv: list[str] | None=None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == 'mcp':
        return run_mcp(argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.home:
        os.environ['DUCK_AGENT_HOME'] = args.home
    command = args.command or 'chat'
    if command == 'work':
        return run_work(getattr(args, 'goal', ''), workflow=getattr(args, 'workflow', 'code'))
    handlers = {'status': print_status, 'backends': print_backends, 'workflows': print_workflows, 'capabilities': print_capabilities, 'doctor': print_doctor, 'chat': run_chat, 'update': run_update, 'work': run_work, 'setup': run_setup, 'version': run_version, 'mcp': lambda: run_mcp([])}
    return handlers[command]()
if __name__ == '__main__':
    raise SystemExit(main())