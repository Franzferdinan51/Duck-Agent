"""Windows-safe stdio configuration.

On Windows, Python's ``sys.stdout``/``sys.stderr`` default to the console's
active code page (often ``cp1252``, sometimes ``cp437``, occasionally ``cp932``
on Japanese locales, etc.).  Duck Agent'ss banners, tool output feed, and slash
command listings all contain Unicode: box-drawing characters (``─┌┐└┘├┤``),
mathematical and geometric symbols (``◆ ◇ ◎ ▣ ⚔ ⚖ →``), and user-supplied
text in any language.  Printing those to a cp1252 console raises
``UnicodeEncodeError: 'charmap' codec can't encode character…`` and kills the
whole CLI before the REPL even opens.

The fix is to force UTF-8 on the Python side and also flip the console's
code page to UTF-8 (65001).  Both matter: Python-level only helps when
Python's stdout is a real TTY; code-page flipping lets subprocesses and
child Python ``print()`` calls agree on encoding.

This module is a no-op on every non-Windows platform, and idempotent.
Entry points (``cli.py`` ``main``, ``hermes_cli/main.py`` CLI dispatch,
``gateway/run.py`` startup) call :func:`configure_windows_stdio` exactly
once early in startup.

Patterns cribbed from Claude Code (``src/utils/platform.ts``), OpenCode
(``packages/opencode/src/pty/index.ts`` env injection), and OpenAI Codex
(``codex-rs/core/src/unified_exec/process_manager.rs``).  None of those
actually flip the console code page — they rely on their runtime (Node or
Rust) writing UTF-16 to the Win32 console API and letting the terminal
sort it out.  Python doesn't get that luxury.
"""
from __future__ import annotations
import os
import sys
__all__ = ['configure_windows_stdio', 'is_windows']
_CONFIGURED = False

def is_windows() -> bool:
    """Return True iff running on native Windows (not WSL)."""
    return sys.platform == 'win32'

def _flip_console_code_page_to_utf8() -> None:
    """Set the attached console's input and output code pages to UTF-8.

    Uses ``SetConsoleCP`` / ``SetConsoleOutputCP`` via ``ctypes``.  Failure
    is silent — if there's no attached console (e.g. Duck Agent is running
    behind a redirected stdout, under a service, or inside a PTY-less CI
    runner) these calls simply return 0 and we move on.

    CP_UTF8 is 65001.
    """
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleCP(65001)
        kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass

def _reconfigure_stream(stream, *, encoding: str='utf-8', errors: str='replace') -> None:
    """Reconfigure a text stream to UTF-8 in place.

    Uses ``TextIOWrapper.reconfigure`` (Python 3.7+).  If the stream isn't
    a ``TextIOWrapper`` (e.g. it's been redirected to an ``io.StringIO``
    during tests), we skip rather than blow up.
    """
    try:
        reconfigure = getattr(stream, 'reconfigure', None)
        if reconfigure is None:
            return
        reconfigure(encoding=encoding, errors=errors)
    except Exception:
        pass

def configure_windows_stdio() -> bool:
    """Force UTF-8 stdio on Windows.  No-op elsewhere.

    Idempotent — safe to call multiple times from different entry points.

    Returns ``True`` if anything was actually changed, ``False`` on
    non-Windows or on a repeat call.

    Set ``HERMES_DISABLE_WINDOWS_UTF8=1`` in the environment to opt out
    (for diagnosing encoding-related bugs by forcing the old cp1252 path).

    Also sets a sensible default ``EDITOR`` on Windows if none is already
    set — see :func:`_default_windows_editor`.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return False
    if not is_windows():
        _CONFIGURED = True
        return False
    if os.environ.get('HERMES_DISABLE_WINDOWS_UTF8') in {'1', 'true', 'True', 'yes'}:
        _CONFIGURED = True
        return False
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    os.environ.setdefault('PYTHONUTF8', '1')
    _default_editor = _default_windows_editor()
    if _default_editor and (not os.environ.get('EDITOR')) and (not os.environ.get('VISUAL')):
        os.environ['EDITOR'] = _default_editor
    _augment_path_with_known_tools()
    _flip_console_code_page_to_utf8()
    _reconfigure_stream(sys.stdout)
    _reconfigure_stream(sys.stderr)
    _reconfigure_stream(sys.stdin)
    _CONFIGURED = True
    return True

def _default_windows_editor() -> str:
    """Return a Windows-appropriate default for ``$EDITOR``.

    Priority order, first match wins:

    1. ``notepad`` — ships with every Windows install, no deps, works as a
       blocking editor (``subprocess.call(["notepad", file])`` blocks until
       the user closes the window).  This is the "always-works" default.

    The prompt_toolkit buffer's ``open_in_editor`` and Duck Agent'ss
    ``duck-agent config edit`` both honour ``$EDITOR``.  Users who prefer a
    different editor can override:

    - VSCode: ``$env:EDITOR = "code --wait"``  (``--wait`` is critical;
      without it the editor returns immediately and any input is lost)
    - Notepad++: ``$env:EDITOR = "'C:\\Program Files\\Notepad++\\notepad++.exe' -multiInst -nosession"``
    - Neovim: ``$env:EDITOR = "nvim"``  (if installed)

    Set this before launching Duck Agent (User env var in Windows Settings, or
    export in a PowerShell profile) and Duck Agent picks it up automatically.
    """
    import shutil
    if shutil.which('notepad'):
        return 'notepad'
    return ''

def _augment_path_with_known_tools() -> None:
    """Prepend well-known Duck Agent-managed tool directories to os.environ['PATH'].

    Fixes the "User PATH was just updated but my process can't see it" gap on
    Windows.  When install.ps1 runs, it adds entries like
    ``%LOCALAPPDATA%\\duck-agent\\git\\bin`` to the User PATH via
    ``SetEnvironmentVariable(..., "User")``.  That write propagates to newly
    *spawned* processes only — already-running shells (including the one the
    user invokes ``duck-agent`` from right after install) retain their old PATH.

    Any subprocess Duck Agent spawns — bash, ``rg``, ``grep``, ``npm`` — inherits
    that stale PATH and reports commands as missing even though they're on
    disk.  Symptom: ``search_files`` reports "rg/find not available" when
    the user clearly just installed ripgrep.

    Patch-up strategy: add the known Duck Agent-managed tool directories to our
    PATH at startup so subprocess calls resolve correctly.  No-op on POSIX
    and when the directories don't exist.  The User PATH broadcast still
    happens in the background for future shells; this just smooths over
    the first-launch gap.
    """
    if not is_windows():
        return
    local_appdata = os.environ.get('LOCALAPPDATA', '')
    if not local_appdata:
        return
    candidate_dirs = [os.path.join(local_appdata, 'duck-agent', 'git', 'cmd'), os.path.join(local_appdata, 'duck-agent', 'git', 'bin'), os.path.join(local_appdata, 'duck-agent', 'git', 'usr', 'bin'), os.path.join(local_appdata, 'duck-agent', 'duck-agent', 'venv', 'Scripts'), os.path.join(local_appdata, 'Microsoft', 'WinGet', 'Links')]
    existing = os.environ.get('PATH', '')
    existing_lower = {p.lower() for p in existing.split(os.pathsep) if p}
    prepend = []
    for d in candidate_dirs:
        if os.path.isdir(d) and d.lower() not in existing_lower:
            prepend.append(d)
    if prepend:
        os.environ['PATH'] = os.pathsep.join([*prepend, existing])