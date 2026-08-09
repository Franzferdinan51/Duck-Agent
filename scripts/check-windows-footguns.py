"""
Grep-based checker for Windows cross-platform footguns.

Flags common patterns that break silently on Windows. Run before PRs —
cheap, fast, catches regressions in a codebase that runs on three OSes.

Usage:
    # Scan staged changes (default when run from a git checkout)
    python scripts/check-windows-footguns.py

    # Scan the full tree (full-repo audit)
    python scripts/check-windows-footguns.py --all

    # Scan a specific file or directory
    python scripts/check-windows-footguns.py path/to/file.py path/to/dir/

    # Scan only modified files vs. main
    python scripts/check-windows-footguns.py --diff main

Exit status:
    0 — no Windows footguns found (or all matches suppressed)
    1 — at least one unsuppressed match

Suppress an intentional use (e.g. tests or platform-gated code) with:
    os.kill(pid, 0)  # windows-footgun: ok — only called on POSIX
"""
from __future__ import annotations
import argparse
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parent.parent
SUPPRESS_MARKER = re.compile('#\\s*windows-footgun\\s*:\\s*ok\\b', re.IGNORECASE)
GUARD_HINTS = ('hasattr(os,', 'hasattr(signal,', 'getattr(os,', 'getattr(signal,', 'shutil.which(', 'if platform.system() != "Windows"', "if platform.system() != 'Windows'", 'if sys.platform == "win32"', 'if sys.platform != "win32"', "if sys.platform == 'win32'", "if sys.platform != 'win32'", 'IS_WINDOWS', 'is_windows')
EXCLUDED_DIRS = {'.git', 'node_modules', 'venv', '.venv', '__pycache__', 'build', 'dist', '.tox', '.mypy_cache', '.pytest_cache', 'site-packages', 'website/build', 'optional-skills'}
EXCLUDED_SUFFIXES = {'.pyc', '.pyo', '.so', '.dll', '.exe', '.png', '.jpg', '.gif', '.ico', '.svg', '.mp4', '.mp3', '.wav', '.pdf', '.zip', '.tar', '.gz', '.whl', '.lock', '.min.js', '.min.css'}
EXCLUDED_FILES = {'scripts/check-windows-footguns.py', 'CONTRIBUTING.md'}

@dataclass
class Footgun:
    """A Windows cross-platform footgun pattern."""
    name: str
    pattern: re.Pattern
    message: str
    fix: str
    path_allowlist: tuple[str, ...] = ()
    post_filter: Callable[[re.Match[str], str], bool] | None = None
FOOTGUNS: list[Footgun] = [Footgun(name='open() without encoding= on text mode', pattern=re.compile('(?:^|[\\s\\(,;=])(?<![.\\w])open\\s*\\(\\s*[^,)]+\\s*(?:,\\s*[\'"](?P<mode>[^\'"]*)[\'"])?'), message="open() without an explicit encoding= uses the platform default (UTF-8 on POSIX, cp1252/mbcs on Windows) — files round-tripped between hosts get mojibake. Always pass encoding='utf-8' for text files, or use open(path, 'rb')/'wb' for binary.", fix="open(path, 'r', encoding='utf-8')  # or 'utf-8-sig' if the file may have a BOM", post_filter=lambda m, line: 'b' not in (m.group('mode') or '') and 'encoding=' not in line and ('encoding =' not in line) and (not line.lstrip().startswith('def ')) and (not line.lstrip().startswith('async def ')) and ('**' not in line)), Footgun(name='os.kill(pid, 0)', pattern=re.compile('\\bos\\.kill\\s*\\(\\s*[^,]+,\\s*0\\s*\\)'), message="os.kill(pid, 0) is NOT a no-op on Windows — it sends CTRL_C_EVENT to the target's console process group, hard-killing the target and potentially unrelated siblings. See bpo-14484.", fix='Use psutil.pid_exists(pid) (psutil is a core dependency). Or gateway.status._pid_exists(pid) for the duck-agent wrapper with a stdlib fallback.'), Footgun(name='bare os.setsid', pattern=re.compile('(?<!hasattr\\()\\bos\\.setsid\\b'), message='os.setsid does not exist on Windows and raises AttributeError. Subprocesses that need detachment on Windows use creationflags instead.', fix="if platform.system() != 'Windows':\n    kwargs['preexec_fn'] = os.setsid\nelse:\n    kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP"), Footgun(name='bare os.killpg', pattern=re.compile('\\bos\\.killpg\\b'), message='os.killpg does not exist on Windows.', fix='Use psutil for cross-platform process-tree kill:\n  p = psutil.Process(pid)\n  for c in p.children(recursive=True): c.kill()\n  p.kill()'), Footgun(name='bare os.getuid / os.geteuid / os.getgid', pattern=re.compile('\\bos\\.(?:getuid|geteuid|getgid|getegid)\\b'), message='os.getuid / os.geteuid / os.getgid do not exist on Windows and raise AttributeError at import time if referenced.', fix="Use getpass.getuser() for the username, or gate with hasattr(os, 'getuid')."), Footgun(name='bare os.fork', pattern=re.compile('(?<!hasattr\\()\\bos\\.fork\\s*\\('), message='os.fork does not exist on Windows.', fix="Use subprocess.Popen for daemonization, or guard with hasattr(os, 'fork') and a Windows fallback path."), Footgun(name='bare signal.SIGKILL', pattern=re.compile('\\bsignal\\.SIGKILL\\b'), message='signal.SIGKILL does not exist on Windows and raises AttributeError at import time.', fix="Use getattr(signal, 'SIGKILL', signal.SIGTERM)."), Footgun(name='bare signal.SIGHUP / SIGUSR1 / SIGUSR2 / SIGALRM / SIGCHLD / SIGPIPE / SIGQUIT', pattern=re.compile('\\bsignal\\.(?:SIGHUP|SIGUSR1|SIGUSR2|SIGALRM|SIGCHLD|SIGPIPE|SIGQUIT)\\b'), message="These POSIX signals don't exist on Windows; referencing them raises AttributeError at import time.", fix="Use getattr(signal, 'SIGXXX', None) and check for None before using, or gate the whole block behind a platform check."), Footgun(name='subprocess shebang script invocation', pattern=re.compile('subprocess\\.(?:run|Popen|call|check_output|check_call)\\s*\\(\\s*\\[\\s*[\'\\"]\\./'), message="Running a script via './scriptname' doesn't work on Windows — shebang lines aren't honored. CreateProcessW can't execute bash/python scripts without an explicit interpreter.", fix="Use [sys.executable, 'scriptname.py', ...] explicitly."), Footgun(name='wmic invocation without shutil.which guard', pattern=re.compile('(?:subprocess\\.\\w+\\s*\\(\\s*\\[\\s*[\'"]wmic[\'"]|[\'"]wmic\\.exe[\'"])'), message="wmic was removed in Windows 10 21H1 and later. Always gate with shutil.which('wmic') and fall back to PowerShell (Get-CimInstance Win32_Process).", fix="if shutil.which('wmic'):\n    ... wmic path ...\nelse:\n    subprocess.run(['powershell', '-NoProfile', '-Command',\n                    'Get-CimInstance Win32_Process | ...'])"), Footgun(name='hardcoded ~/Desktop (OneDrive trap)', pattern=re.compile('[\'"](?:~|~/|[A-Z]:[/\\\\]Users[/\\\\][^/\\\\\'"]+[/\\\\])Desktop\\b'), message='When OneDrive Backup is enabled on Windows, the real Desktop is at %USERPROFILE%\\OneDrive\\Desktop, not %USERPROFILE%\\Desktop (which exists as an empty husk).', fix="On Windows, resolve via ctypes + SHGetKnownFolderPath, or read the Shell Folders registry key, or run PowerShell [Environment]::GetFolderPath('Desktop')."), Footgun(name='asyncio add_signal_handler without try/except', pattern=re.compile('\\.add_signal_handler\\s*\\('), message='loop.add_signal_handler raises NotImplementedError on Windows — always wrap in try/except or gate with a platform check.', fix="try:\n    loop.add_signal_handler(sig, handler, sig)\nexcept NotImplementedError:\n    pass  # Windows asyncio doesn't support signal handlers"), Footgun(name='subprocess text=True without explicit encoding=', pattern=re.compile('\\btext\\s*=\\s*True\\b'), message="subprocess text=True without explicit encoding= decodes child output with locale.getpreferredencoding() — cp936 (GBK) on Chinese Windows, cp1252 on Western Windows — which crashes _readerthread with UnicodeDecodeError on non-default-codepage bytes. Always pass encoding='utf-8' (and errors='replace' for Windows-native CLIs that emit non-UTF-8). See issues #47939, #53428, #57238.", fix="subprocess.run(..., text=True, encoding='utf-8', errors='replace')\nBoth params are required: encoding alone still crashes on non-UTF-8 bytes from Windows-native CLIs (tasklist, schtasks).", post_filter=lambda m, line: 'encoding=' not in line and 'encoding =' not in line and (not line.lstrip().startswith('def ')) and (not line.lstrip().startswith('async def ')) and (not _looks_like_string_literal(line, m)) and _is_likely_subprocess_call(line)), Footgun(name='bare Path.read_text()/write_text() without encoding=', pattern=re.compile('\\.(read_text|write_text)\\s*\\('), message='Path.read_text()/write_text() without encoding= uses locale.getpreferredencoding() — cp936/cp1252 on Windows — so UTF-8 content (config JSON, session state, skills) crashes with UnicodeDecodeError or writes mojibake. See issue #37423 and the #71014 / read_text campaign.', fix='path.read_text(encoding="utf-8") / path.write_text(data, encoding="utf-8")', post_filter=lambda m, line: 'encoding=' not in line and 'encoding =' not in line and (not _looks_like_string_literal(line, m)) and line.rstrip().endswith(')'))]

def should_scan_file(path: Path) -> bool:
    """Return True if this file is in scope for the checker."""
    parts = set(path.parts)
    if parts & EXCLUDED_DIRS:
        return False
    for suffix in EXCLUDED_SUFFIXES:
        if str(path).endswith(suffix):
            return False
    rel = path.relative_to(REPO_ROOT).as_posix()
    if rel in EXCLUDED_FILES:
        return False
    if path.suffix in {'.py', '.pyw', '.pyi'}:
        return True
    return False

def iter_files(paths: Iterable[Path]) -> Iterable[Path]:
    for p in paths:
        if p.is_file():
            if should_scan_file(p):
                yield p
        elif p.is_dir():
            for root, dirs, files in os.walk(p):
                dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
                for fname in files:
                    fpath = Path(root) / fname
                    if should_scan_file(fpath):
                        yield fpath

def _strip_code(line: str) -> str:
    """Return just the code portion of a line — strip trailing comments and
    skip lines that are entirely inside a string literal or comment.

    Heuristic only (we don't parse Python); good enough to avoid flagging
    our own `# ``os.kill(pid, 0)`` is NOT a no-op` docstring-style comments.
    """
    stripped = line.lstrip()
    if stripped.startswith('#'):
        return ''
    hash_idx = _find_unquoted_hash(line)
    if hash_idx is not None:
        return line[:hash_idx]
    return line

def _find_unquoted_hash(line: str) -> int | None:
    """Index of the first `#` not inside a single/double/triple-quoted string.

    Simple state machine — good enough for the 99% case of "code, then
    optional trailing comment."
    """
    i = 0
    n = len(line)
    in_s = False
    in_d = False
    while i < n:
        c = line[i]
        if c == '\\' and (in_s or in_d) and (i + 1 < n):
            i += 2
            continue
        if not in_d and c == "'":
            in_s = not in_s
        elif not in_s and c == '"':
            in_d = not in_d
        elif c == '#' and (not in_s) and (not in_d):
            return i
        i += 1
    return None
_SUBPROCESS_METHODS = ('subprocess.run', 'subprocess.Popen', 'subprocess.call', 'subprocess.check_output', 'subprocess.check_call', '_sp.run', '_sp.Popen', '_sp.check_output', '_sp.check_call', '_sp.call', '.run(', '.Popen(', '.check_output(', '.check_call(', '.call(')

def _is_likely_subprocess_call(line: str) -> bool:
    """Heuristic: does this line look like a subprocess invocation?

    The ``text=True`` footgun rule only fires when the matched line also
    contains a subprocess-shaped call site. This avoids false positives on
    unrelated APIs that accept a ``text`` kwarg (e.g. DataFrame.rename,
    custom library calls). Multi-line calls where the ``subprocess.X(``
    prefix is on a previous line won't be flagged — that's an acceptable
    false negative for a line-based scanner.
    """
    return any((token in line for token in _SUBPROCESS_METHODS))

def _looks_like_string_literal(line: str, match: re.Match[str]) -> bool:
    """Heuristic: is the ``text=True`` match inside a string literal?

    Catches the common case of docstrings/comments that mention ``text=True``
    as prose. Walks the line tracking single/double quote state and returns
    True if the match start index falls inside a quoted region.
    """
    start = match.start()
    in_s = False
    in_d = False
    i = 0
    while i < start and i < len(line):
        c = line[i]
        if c == '\\' and (in_s or in_d) and (i + 1 < len(line)):
            i += 2
            continue
        if not in_d and c == "'":
            in_s = not in_s
        elif not in_s and c == '"':
            in_d = not in_d
        i += 1
    return in_s or in_d

def scan_file(path: Path, footguns: list[Footgun]) -> list[tuple[int, str, Footgun]]:
    """Return a list of (line_number, line, footgun) for unsuppressed matches."""
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return []
    matches: list[tuple[int, str, Footgun]] = []
    in_triple: str | None = None
    for i, line in enumerate(text.splitlines(), start=1):
        code_for_scan = line
        if in_triple:
            if in_triple in line:
                after = line.split(in_triple, 1)[1]
                in_triple = None
                code_for_scan = after
            else:
                continue
        for delim in ('"""', "'''"):
            if delim in code_for_scan:
                count = code_for_scan.count(delim)
                if count % 2 == 1:
                    before = code_for_scan.split(delim, 1)[0]
                    code_for_scan = before
                    in_triple = delim
                    break
                else:
                    parts = code_for_scan.split(delim)
                    code_for_scan = ''.join(parts[::2])
                    break
        if SUPPRESS_MARKER.search(line):
            continue
        if any((hint in line for hint in GUARD_HINTS)):
            continue
        code = _strip_code(code_for_scan)
        if not code.strip():
            continue
        for fg in footguns:
            if fg.path_allowlist and any((s in str(path) for s in fg.path_allowlist)):
                continue
            match = fg.pattern.search(code)
            if not match:
                continue
            if fg.post_filter is not None:
                try:
                    if not fg.post_filter(match, line):
                        continue
                except (IndexError, AttributeError):
                    continue
            matches.append((i, line.rstrip(), fg))
    return matches

def get_staged_files() -> list[Path]:
    """Return paths staged in the current git index. Empty on non-git trees."""
    try:
        out = subprocess.check_output(['git', 'diff', '--cached', '--name-only', '--diff-filter=ACMR'], cwd=REPO_ROOT, stderr=subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace')
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [REPO_ROOT / f for f in out.splitlines() if f.strip()]

def get_diff_files(ref: str) -> list[Path]:
    """Return paths modified vs. the given git ref."""
    try:
        out = subprocess.check_output(['git', 'diff', f'{ref}...HEAD', '--name-only', '--diff-filter=ACMR'], cwd=REPO_ROOT, stderr=subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace')
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [REPO_ROOT / f for f in out.splitlines() if f.strip()]

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description='Flag Windows cross-platform footguns in Python code.')
    p.add_argument('paths', nargs='*', type=Path, help='Specific files/dirs to scan (default: staged changes).')
    p.add_argument('--all', action='store_true', help='Scan the full repository (hermes_cli/, gateway/, tools/, cron/, etc.).')
    p.add_argument('--diff', metavar='REF', help='Scan files changed vs. the given git ref (e.g. --diff main).')
    p.add_argument('--list', action='store_true', help='List all known footgun rules and exit.')
    return p.parse_args(argv)

def print_rules() -> None:
    print('Known Windows footguns checked by this script:\n')
    for i, fg in enumerate(FOOTGUNS, start=1):
        print(f'{i:2}. {fg.name}')
        print(f'    {fg.message}')
        print(f'    Fix: {fg.fix}')
        print()

def main(argv: list[str]) -> int:
    stdout_reconfigure = getattr(sys.stdout, 'reconfigure', None)
    if callable(stdout_reconfigure):
        stdout_reconfigure(encoding='utf-8')
    stderr_reconfigure = getattr(sys.stderr, 'reconfigure', None)
    if callable(stderr_reconfigure):
        stderr_reconfigure(encoding='utf-8')
    args = parse_args(argv)
    if args.list:
        print_rules()
        return 0
    if args.all:
        roots = [REPO_ROOT / 'hermes_cli', REPO_ROOT / 'gateway', REPO_ROOT / 'tools', REPO_ROOT / 'cron', REPO_ROOT / 'agent', REPO_ROOT / 'plugins', REPO_ROOT / 'scripts', REPO_ROOT / 'acp_adapter']
        roots = [r for r in roots if r.exists()]
    elif args.diff:
        roots = get_diff_files(args.diff)
    elif args.paths:
        roots = [p.resolve() for p in args.paths]
    else:
        roots = get_staged_files()
        if not roots:
            print('No staged files to scan. Pass --all for a full-repo scan, --diff <ref> for a range diff, or paths explicitly.', file=sys.stderr)
            return 0
    total_matches = 0
    files_scanned = 0
    for path in iter_files(roots):
        files_scanned += 1
        matches = scan_file(path, FOOTGUNS)
        for lineno, line, fg in matches:
            rel = path.relative_to(REPO_ROOT).as_posix()
            print(f'{rel}:{lineno}: [{fg.name}]')
            print(f'    {line.strip()}')
            print(f'    — {fg.message}')
            print(f'    Fix: {fg.fix.splitlines()[0]}')
            print()
            total_matches += 1
    if total_matches:
        print(f'\n✗ {total_matches} Windows footgun(s) found across {files_scanned} file(s) scanned.', file=sys.stderr)
        print('  If an individual match is a false positive or intentionally platform-gated, suppress it with `# windows-footgun: ok` on the same line.\n  Run with --list to see all rules.', file=sys.stderr)
        return 1
    print(f'✓ No Windows footguns found ({files_scanned} file(s) scanned).')
    return 0
if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))