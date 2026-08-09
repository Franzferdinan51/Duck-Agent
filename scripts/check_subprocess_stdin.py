"""Check that subprocess calls in TUI-context code specify stdin=.

When Duck Agent runs in TUI mode, the gateway child process communicates with
the Node.js parent over a JSON-RPC protocol on stdin. Subprocess calls that
inherit this fd can cause the gateway to exit with stdin EOF during tool
execution (issue #14036, PR #39257).

This script checks that all subprocess.run() and subprocess.Popen() calls
in TUI-context files (agent/, tools/, plugins/, tui_gateway/) explicitly
set stdin= to prevent fd inheritance.

Exit codes:
  0 — all calls are safe
  1 — violations found
  2 — script error

Usage:
  python scripts/check_subprocess_stdin.py [--fix]

With --fix, prints the commands to add stdin=subprocess.DEVNULL to each
violation (does not modify files).
"""
from __future__ import annotations
import os
import re
import sys
from pathlib import Path
TUI_CONTEXT_DIRS = ['agent/', 'tools/', 'plugins/', 'tui_gateway/']
_SUBPROCESS_PATTERNS = ['subprocess\\.(run|Popen|call|check_output|check_call)\\s*\\([\\"\'a-zA-Z_\\[\\(]', 'os\\.system\\s*\\([\\"\'a-zA-Z_\\[\\(]', 'asyncio\\.create_subprocess_(exec|shell)\\s*\\([\\"\'a-zA-Z_\\[\\(]']
KNOWN_SAFE = {'agent/shell_hooks.py', 'plugins/security-guidance/patterns.py'}
EXEMPT_MARKER = 'noqa: subprocess-stdin'
SKIP_DIRS = {'tests/', 'scripts/', 'skills/', 'optional-skills/', 'hermes_cli/', 'gateway/', 'cron/'}

def find_subprocess_calls(content: str, filepath: str) -> list[dict]:
    """Find all subprocess/os/asyncio calls missing stdin= in content."""
    violations = []
    lines = content.split('\n')
    patterns = [re.compile(p) for p in _SUBPROCESS_PATTERNS]
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith('#'):
            continue
        if '``subprocess' in line:
            continue
        if not any((p.search(line) for p in patterns)):
            continue
        call_start = i
        paren_depth = 0
        found_open = False
        call_lines = []
        for j in range(i, min(i + 30, len(lines))):
            call_lines.append(lines[j])
            for ch in lines[j]:
                if ch == '(':
                    paren_depth += 1
                    found_open = True
                elif ch == ')':
                    paren_depth -= 1
                    if found_open and paren_depth == 0:
                        call_text = '\n'.join(call_lines)
                        if 'stdin=' in call_text:
                            break
                        if 'input=' in call_text:
                            break
                        window_start = max(0, i - 4)
                        preceding = '\n'.join(lines[window_start:i])
                        if EXEMPT_MARKER in call_text or EXEMPT_MARKER in preceding:
                            break
                        violations.append({'file': filepath, 'line': i + 1, 'snippet': line.strip()[:120]})
                        break
            else:
                continue
            break
    return violations

def main() -> int:
    fix_mode = '--fix' in sys.argv
    repo_root = Path(__file__).resolve().parent.parent
    os.chdir(repo_root)
    sys.path.insert(0, str(repo_root))
    from hermes_constants import get_hermes_home
    all_violations = []
    for tui_dir in TUI_CONTEXT_DIRS:
        dirpath = repo_root / tui_dir
        if not dirpath.exists():
            continue
        for py_file in dirpath.rglob('*.py'):
            rel = str(py_file.relative_to(repo_root))
            if rel in KNOWN_SAFE:
                continue
            parts = py_file.parts
            if any((skip.rstrip('/') in parts for skip in SKIP_DIRS)):
                continue
            content = py_file.read_text(encoding='utf-8')
            violations = find_subprocess_calls(content, rel)
            all_violations.extend(violations)
    plugin_roots: list[Path] = [get_hermes_home() / 'plugins']
    if os.environ.get('HERMES_ENABLE_PROJECT_PLUGINS'):
        plugin_roots.append(Path.cwd() / '.duck-agent' / 'plugins')
    seen_roots: set[Path] = set()
    for plugin_root in plugin_roots:
        resolved = plugin_root.resolve()
        if resolved in seen_roots or not resolved.is_dir():
            continue
        seen_roots.add(resolved)
        for py_file in resolved.rglob('*.py'):
            rel = str(py_file)
            if py_file.name in ('conftest.py',) or '/tests/' in rel:
                continue
            try:
                content = py_file.read_text(encoding='utf-8')
            except Exception:
                continue
            violations = find_subprocess_calls(content, rel)
            all_violations.extend(violations)
    if all_violations:
        print(f'❌ {len(all_violations)} subprocess calls missing stdin=:')
        for v in all_violations:
            print(f"  {v['file']}:{v['line']}: {v['snippet']}")
        if fix_mode:
            print('\nAdd stdin=subprocess.DEVNULL to each call above.')
        return 1
    else:
        print('✅ All TUI-context subprocess calls have explicit stdin=')
        return 0
if __name__ == '__main__':
    sys.exit(main())