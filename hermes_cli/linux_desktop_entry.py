"""Install and remove the Linux desktop entry (``duck-agent.desktop``).

``duck-agent desktop`` builds and launches the Electron app. On Linux, a
freshly-built app has no launcher presence: no menu item, no icon. This
module writes the XDG desktop entry that gives it one.
``duck-agent uninstall --gui`` removes the entry again.

Two values must be absolute for the entry to work:

  - ``Exec`` — the launcher runs without shell ``PATH`` customizations, so
    a bare ``duck-agent desktop`` fails when duck-agent lives in ``~/.local/bin``
    or a venv. Resolve the real binary and write its full path.
  - ``Icon`` — an unqualified icon name needs an indexed icon theme. The
    spec allows an absolute path instead, so point at the app icon in the
    checkout. Do not copy the icon: ``Exec`` already depends on that tree.

Cache refresh is best-effort and tool-gated: ``update-desktop-database``
for the freedesktop menu cache, and ``kbuildsycoca6``/``kbuildsycoca5``
for Plasma. Run each tool only when it exists. A missing tool is not an
error.

Import-light and side-effect-free at import time: the uninstaller and the
Electron main process both use this without loading the full CLI.
"""
from __future__ import annotations
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional
DESKTOP_ENTRY_NAME = 'duck-agent.desktop'

def is_supported() -> bool:
    """XDG desktop entries exist only on Linux and BSD."""
    return sys.platform.startswith(('linux', 'freebsd', 'openbsd', 'netbsd'))

def _xdg_data_home() -> Path:
    raw = os.environ.get('XDG_DATA_HOME')
    if raw and raw.strip():
        return Path(raw).expanduser()
    return Path.home() / '.local' / 'share'

def desktop_entry_path() -> Path:
    """Where the ``duck-agent.desktop`` entry lives."""
    return _xdg_data_home() / 'applications' / DESKTOP_ENTRY_NAME

def icon_path(project_root: Path) -> Path:
    """The app icon shipped in the desktop workspace."""
    return project_root / 'apps' / 'desktop' / 'assets' / 'icon.png'

def resolve_exec_command() -> str:
    """Build the absolute ``Exec=`` command line for ``duck-agent desktop``.

    Prefer the real ``duck-agent`` executable (argv[0] or PATH). When Duck Agent
    runs as a module with no launcher installed, use the current
    interpreter, also absolute.
    """
    from hermes_cli.relaunch import resolve_hermes_bin
    bin_path = resolve_hermes_bin()
    if bin_path:
        argv = [str(Path(bin_path).resolve()), 'desktop']
    else:
        argv = [str(Path(sys.executable).resolve()), '-m', 'hermes_cli.main', 'desktop']
    return ' '.join((_quote_exec_arg(a) for a in argv))

def _quote_exec_arg(arg: str) -> str:
    """Quote one ``Exec`` argument per the desktop entry spec.

    Reserved characters require double quotes. Inside the quotes, escape
    a backslash and a double quote with a backslash.
    """
    if not any((c in arg for c in ' \t\n"\'\\><~|&;$*?#()`')):
        return arg
    escaped = arg.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'

def render_desktop_entry(exec_command: str, icon: str) -> str:
    return f'[Desktop Entry]\nType=Application\nName=Duck Agent\nGenericName=Duck Agent Desktop\nComment=Launch Duck Agent Desktop\nExec={exec_command}\nIcon={icon}\nTerminal=false\nCategories=Utility;\nStartupNotify=true\nStartupWMClass=Duck Agent\n'

def refresh_desktop_databases(applications_dir: Path) -> 'list[str]':
    """Reindex the menu caches. Run each tool only when it exists.

    Return the names of the tools that ran (for logging and tests).
    """
    ran: list[str] = []
    update_db = shutil.which('update-desktop-database')
    if update_db:
        if _run_quiet([update_db, str(applications_dir)]):
            ran.append('update-desktop-database')
    for tool in ('kbuildsycoca6', 'kbuildsycoca5'):
        resolved = shutil.which(tool)
        if not resolved:
            continue
        if _run_quiet([resolved, '--noincremental']):
            ran.append(tool)
        break
    return ran

def _run_quiet(cmd: 'list[str]') -> bool:
    try:
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0

def install_desktop_entry(project_root: Path) -> Optional[Path]:
    """Write (or refresh) the Duck Agent desktop entry. Return its path.

    Return ``None`` on non-Linux platforms or when the write fails. This
    is a convenience, never a reason to fail a launch.
    """
    if not is_supported():
        return None
    entry_path = desktop_entry_path()
    icon = icon_path(project_root)
    icon_value = str(icon) if icon.is_file() else 'duck-agent'
    contents = render_desktop_entry(resolve_exec_command(), icon_value)
    try:
        entry_path.parent.mkdir(parents=True, exist_ok=True)
        if entry_path.is_file() and entry_path.read_text(encoding='utf-8') == contents:
            return entry_path
        entry_path.write_text(contents, encoding='utf-8')
        entry_path.chmod(493)
    except OSError:
        return None
    refresh_desktop_databases(entry_path.parent)
    return entry_path