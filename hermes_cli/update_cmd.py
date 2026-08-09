"""Duck Agent update pipeline — extracted from ``hermes_cli/main.py``.

Mechanical move (main.py decomposition): ``_cmd_update_impl``, ``_cmd_update_check``
and every module-level helper used only by the update path, plus the update-only
constants they read. Function bodies are lifted verbatim; the only mechanical
change is that references to helpers/constants that STAY in ``hermes_cli.main``
(and to moved-but-test-patched siblings) are routed through ``_m()`` — a lazy
``hermes_cli.main`` reference — so existing call sites and test monkeypatches
that target ``hermes_cli.main.<name>`` (``PROJECT_ROOT``, ``_is_windows``,
``_run_pre_update_backup``, ...) keep working unchanged. ``main.py`` re-imports
every public-ish name from here (``# noqa: F401``) so the argparse wiring and
the test-patch surface still resolve on ``hermes_cli.main``.

Three self-contained closures nested inside ``_cmd_update_impl``
(``_print_items``, ``_wait_for_service_active``, ``_service_restart_sec``) were
hoisted to module level; they capture no enclosing state (verified via
``symtable``). ``_restart_one_systemd_gateway_unit``, ``_resolve_manage_cmd``
and ``_on_unit_timeout`` DO capture enclosing locals and stay nested,
byte-identical.

Imports are one-way: ``hermes_cli.main`` imports this module, never the reverse
at import time (``_m()`` resolves lazily at call time, when main.py is fully
loaded, so there is no import cycle).
"""
import hashlib
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import time as _time
from datetime import datetime
from pathlib import Path
from typing import Optional
from hermes_cli.config import get_hermes_home
from hermes_constants import venv_python_path
logger = logging.getLogger(__name__)

def _m():
    """Lazy ``hermes_cli.main`` reference.

    Lets callers keep patching ``hermes_cli.main.<helper>`` (the historical
    test surface) and have those patches reach this code path, and defers the
    import so ``hermes_cli.main`` -> ``hermes_cli.update_cmd`` stays one-way
    at import time.
    """
    from hermes_cli import main
    return main
_UPDATE_RUNTIME_RELOAD_MODULES = ('hermes_constants', 'tools.environments.local', 'tools.lazy_deps')

def _reload_updated_runtime_modules() -> None:
    """Reload update-sensitive modules after the checkout changes in-place.

    ``duck-agent update`` keeps running in the pre-pull Python process. After a
    large update, modules already present in ``sys.modules`` can still expose
    old symbols even though their source files on disk are new. Refresh the
    small module set used by lazy-backend refresh before that step imports
    newly-updated code paths.
    """
    try:
        import importlib
        importlib.invalidate_caches()
        for module_name in _UPDATE_RUNTIME_RELOAD_MODULES:
            module = _m().sys.modules.get(module_name)
            if module is None:
                continue
            try:
                importlib.reload(module)
            except Exception as exc:
                logger.debug('Could not reload updated module %s: %s', module_name, exc)
    except Exception as exc:
        logger.debug('Could not refresh update runtime modules: %s', exc)
_UPDATE_CRITICAL_FILES = ('hermes_cli/main.py', 'hermes_cli/config.py', 'hermes_cli/__init__.py', 'hermes_cli/web_server.py', 'cli.py', 'run_agent.py', 'model_tools.py', 'toolsets.py', 'hermes_constants.py')

def _capture_head_sha(git_cmd, cwd) -> str | None:
    """Return the current HEAD SHA, or None if it can't be resolved."""
    try:
        result = subprocess.run(git_cmd + ['rev-parse', 'HEAD'], cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace', check=True)
        return result.stdout.strip() or None
    except (subprocess.CalledProcessError, OSError):
        return None

def _validate_critical_files_syntax(root) -> tuple[bool, str | None, str | None]:
    """Compile each file in ``_UPDATE_CRITICAL_FILES`` to catch SyntaxErrors.

    These are the files imported on every ``duck-agent`` startup; if any of them
    has a syntax error (orphan merge-conflict markers, bad ref to a name
    that no longer exists, etc.) the CLI can't bootstrap at all. We validate
    them after a successful ``git pull`` so we can auto-roll-back instead of
    leaving the user with a bricked install.

    The compiled ``.pyc`` is written to a temp directory rather than the
    source tree's ``__pycache__/`` so we don't race with concurrent test
    workers that walk the same dir, and so we don't leave a stale pyc
    behind in production if the next interpreter run picks a different
    Python version. The pyc is discarded on function return either way —
    we only care about the compile-or-not signal.

    Returns ``(ok, failing_path, error_message)``. ``ok=True`` means every
    file parsed cleanly.
    """
    import py_compile
    import tempfile
    root = Path(root)
    with tempfile.TemporaryDirectory(prefix='duck-agent-syntax-check-') as tmpdir:
        for relpath in _UPDATE_CRITICAL_FILES:
            path = root / relpath
            if not path.exists():
                continue
            cfile = Path(tmpdir) / (relpath.replace('/', '__') + 'c')
            try:
                py_compile.compile(str(path), cfile=str(cfile), doraise=True)
            except py_compile.PyCompileError as exc:
                return (False, str(path), str(exc))
            except OSError as exc:
                return (False, str(path), f'could not read: {exc}')
    return (True, None, None)
_UPDATE_CRITICAL_MODULES = ('hermes_cli.main', 'run_agent', 'model_tools', 'toolsets')

def _validate_critical_modules_import(root) -> tuple[bool, str | None, str | None]:
    """Import each module in ``_UPDATE_CRITICAL_MODULES`` in a subprocess.

    ``_validate_critical_files_syntax`` only *parses* files, so it cannot see
    cross-module breakage: a partially-updated tree where ``agent/`` is new but
    ``tools/`` is old parses perfectly and still dies at startup with
    ``ImportError: cannot import name 'TODO_INJECTION_HEADER' from
    'tools.todo_tool'``. Every file is valid Python; the *combination* is not.

    That skew is reachable on the Windows ZIP-update path, whose copy loop
    walks top-level entries in ``os.listdir`` order and replaces each one
    independently — ``agent/`` lands long before ``tools/``, so a failure or
    interruption between them leaves exactly that mismatch on disk.

    Runs in a subprocess because importing these modules into the running
    updater would pollute ``sys.modules`` and execute import-time side effects
    against the half-updated tree. Costs ~0.4s.

    Uses the project venv's interpreter when there is one (matching
    ``_venv_core_imports_healthy``): ``duck-agent update`` can be driven by a
    different Python than the install's own, and probing the wrong
    interpreter would test a tree the user never runs.

    Returns ``(ok, failing_module, error_message)``.
    """
    from hermes_constants import FIRST_PARTY_MODULE_ROOTS
    probe = "import importlib, sys\nfor name in %r:\n    try:\n        importlib.import_module(name)\n    except ModuleNotFoundError as exc:\n        missing = (getattr(exc, 'name', '') or '').split('.')[0]\n        if missing in %r or missing.startswith('hermes_'):\n            sys.stdout.write(name + '\\n' + str(exc))\n            raise SystemExit(3)\n    except ImportError as exc:\n        sys.stdout.write(name + '\\n' + str(exc))\n        raise SystemExit(3)\n    except Exception:\n        pass\nraise SystemExit(0)\n" % (_UPDATE_CRITICAL_MODULES, tuple(sorted(FIRST_PARTY_MODULE_ROOTS)))
    try:
        interpreter = sys.executable
        try:
            venv_python = venv_python_path(Path(root) / 'venv', windows=_m()._is_windows())
            if venv_python.exists():
                interpreter = str(venv_python)
        except Exception:
            pass
        result = subprocess.run([interpreter, '-c', probe], cwd=str(root), capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
    except (OSError, subprocess.SubprocessError):
        return (True, None, None)
    if result.returncode == 3:
        parts = (result.stdout or '').split('\n', 1)
        module = parts[0].strip() or 'unknown'
        detail = parts[1].strip() if len(parts) > 1 else ''
        return (False, module, detail)
    return (True, None, None)

def _gateway_prompt(prompt_text: str, default: str='', timeout: float=300.0) -> str:
    """File-based IPC prompt for gateway mode.

    Writes a prompt marker file so the gateway can forward the question to the
    user, then polls for a response file.  Falls back to *default* on timeout.

    Used by ``duck-agent update --gateway`` so interactive prompts (stash restore,
    config migration) are forwarded to the messenger instead of being silently
    skipped.
    """
    import json as _json
    import uuid as _uuid
    from hermes_constants import get_hermes_home
    home = get_hermes_home()
    prompt_path = home / '.update_prompt.json'
    response_path = home / '.update_response'
    response_path.unlink(missing_ok=True)
    payload = {'prompt': prompt_text, 'default': default, 'id': str(_uuid.uuid4())}
    tmp = prompt_path.with_suffix('.tmp')
    tmp.write_text(_json.dumps(payload), encoding='utf-8')
    tmp.replace(prompt_path)
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if response_path.exists():
            try:
                answer = response_path.read_text(encoding='utf-8').strip()
                response_path.unlink(missing_ok=True)
                prompt_path.unlink(missing_ok=True)
                return answer if answer else default
            except (OSError, ValueError):
                pass
        _time.sleep(0.5)
    prompt_path.unlink(missing_ok=True)
    response_path.unlink(missing_ok=True)
    print(f'  (no response after {int(timeout)}s, using default: {default!r})')
    return default

def _npm_bin_exists(bin_dir: Path, name: str) -> bool:
    """True when an npm bin shim for *name* exists (POSIX or Windows)."""
    return any(((bin_dir / candidate).exists() for candidate in (name, f'{name}.cmd', f'{name}.ps1', f'{name}.exe')))

def _web_build_toolchain_ready(*roots: Path) -> bool:
    """True when ``tsc`` and ``vite`` shims are reachable from any of *roots*.

    Callers must pass every root the build would search; checking only one
    reports a healthy tree as broken.
    """
    bin_dirs = [bin_dir for bin_dir in (root / 'node_modules' / '.bin' for root in roots) if bin_dir.is_dir()]
    return bool(bin_dirs) and all((any((_npm_bin_exists(bin_dir, tool) for bin_dir in bin_dirs)) for tool in ('tsc', 'vite')))

def _web_toolchain_roots(web_dir: Path) -> tuple[Path, ...]:
    """Roots whose ``node_modules/.bin`` can satisfy the web build.

    ``npm run build`` prepends ``node_modules/.bin`` for the package and each
    of its ancestors, so shims hoisted to the workspace root and shims nested
    under a package that owns its lockfile (#42973) are equally valid.
    """
    return (web_dir, web_dir.parent)

def _print_curator_first_run_notice() -> None:
    """Print a short heads-up about the skill curator after `duck-agent update`.

    Only fires when the curator is enabled AND has no recorded run yet, which
    is exactly the window where the gateway ticker used to fire Curator
    against a fresh skill library immediately after an update. We defer the
    first real pass by one ``interval_hours``; this notice tells the user how
    to preview or disable before then. Silent on steady state.
    """
    try:
        from agent import curator
    except Exception:
        return
    try:
        if not curator.is_enabled():
            return
        state = curator.load_state()
    except Exception:
        return
    if state.get('last_run_at'):
        return
    try:
        hours = curator.get_interval_hours()
    except Exception:
        hours = 24 * 7
    days = max(1, hours // 24)
    print()
    print('ℹ Skill curator')
    print(f'  Background skill maintenance is enabled. First pass is deferred ~{days}d after installation; only agent-created skills are in scope and nothing is ever auto-deleted (archive is recoverable).')
    print('  Preview now:  duck-agent curator run --dry-run')
    print('  Pause it:     duck-agent curator pause')
    print('  Docs:         https://duck-agent.nousresearch.com/docs/user-guide/features/curator')

def _print_fts_optimize_available_notice() -> None:
    """Advertise the opt-in v23 search-index optimization after `duck-agent update`.

    Only fires when the current profile's state.db is still on the legacy
    (pre-v23) inline FTS layout. Leads with the reclaimable-space figure and
    points at the exact command. Honors ``sessions.fts_optimize_notice``:
    ``advise`` (default) prints an advisory notice, ``require`` prints a
    firmer required-upgrade notice, ``off`` suppresses it. Silent for
    fresh/already-optimized installs.
    """
    mode = 'advise'
    try:
        from hermes_cli.config import load_config
        mode = str(((load_config() or {}).get('sessions') or {}).get('fts_optimize_notice', 'advise')).strip().lower()
    except Exception:
        mode = 'advise'
    if mode == 'off':
        return
    try:
        from hermes_constants import get_hermes_home
        from hermes_state import SessionDB
    except Exception:
        return
    db_path = get_hermes_home() / 'state.db'
    if not db_path.exists():
        return
    try:
        size_gb = db_path.stat().st_size / 1024 ** 3
    except OSError:
        return
    if size_gb < 0.5:
        return
    db = None
    interrupted = False
    try:
        db = SessionDB(db_path=db_path, read_only=True)
        row = db._conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'messages_fts'").fetchone()
        interrupted = bool(db._conn.execute("SELECT 1 FROM state_meta WHERE key = 'fts_rebuild_high_water' LIMIT 1").fetchone() or db._conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name LIKE 'fts\\_v22\\_trash\\_%' ESCAPE '\\' LIMIT 1").fetchone() or db._conn.execute("SELECT 1 FROM state_meta WHERE key IN ('fts_cjk_rebuild_high_water', 'fts_cjk_stale') LIMIT 1").fetchone())
    except Exception:
        return
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
    sql = (row[0] if row else '') or ''
    if not sql or ('tool_name' in sql and (not interrupted)):
        return
    if interrupted:
        print()
        print('◆ Session database optimization incomplete')
        print('  A previous `duck-agent sessions optimize-storage` run was interrupted. Search still works; re-run the command to resume and finish reclaiming disk:')
        print('    duck-agent sessions optimize-storage')
        return
    est_reclaim = size_gb * 0.6
    print()
    if mode == 'require':
        print('◆ Session database upgrade required')
        print(f'  Your search index uses the OLD storage layout and should be upgraded. The new layout typically frees ~60% of state.db (≈{est_reclaim:.1f} GB of your current {size_gb:.1f} GB) and is required for continued optimal operation.')
    else:
        print('◆ Reclaim ~60% of your session database disk')
        print(f'  Your search index uses the old storage layout. Upgrading it typically frees ~60% of state.db — about {est_reclaim:.1f} GB of your current {size_gb:.1f} GB.')
    print('  Run when convenient:  duck-agent sessions optimize-storage')
    print('  It runs in the foreground with a progress bar, is safe to interrupt/re-run, and never changes your conversations.')

def _print_curator_recent_run_notice() -> None:
    """Print the most recent curator run summary, exactly once.

    The curator runs in the background (gateway tick + CLI session start),
    so users learn about skill consolidations only by stumbling into a
    rename. ``duck-agent update`` is a high-attention surface — surface the
    most recent run's rename map here, once.

    Show-once: state stamps ``last_run_summary_shown_at`` after printing.
    Subsequent ``duck-agent update`` invocations skip the block until a newer
    curator run lands. Silent when the curator has never run, when the
    most recent summary has already been shown, or when the summary has
    no rename information to display (no archives).
    """
    try:
        from agent import curator
    except Exception:
        return
    try:
        state = curator.load_state()
    except Exception:
        return
    last_run_at = state.get('last_run_at')
    if not last_run_at:
        return
    if state.get('last_run_summary_shown_at') == last_run_at:
        return
    summary = state.get('last_run_summary') or ''
    if not summary:
        return
    if '\n' not in summary:
        try:
            state['last_run_summary_shown_at'] = last_run_at
            curator.save_state(state)
        except Exception:
            pass
        return
    when = _format_time_ago(last_run_at)
    print()
    print(f'ℹ Skill curator — last run {when}')
    for line in summary.splitlines():
        print(f'  {line}')
    print('  (This message shows once per curator run. View anytime: duck-agent curator status)')
    try:
        state['last_run_summary_shown_at'] = last_run_at
        curator.save_state(state)
    except Exception:
        pass

def _format_time_ago(iso_ts: str) -> str:
    """Render an ISO timestamp as `Xh ago` / `Xd ago` / `Xm ago`. Best effort."""
    try:
        from datetime import datetime, timezone
        ts = datetime.fromisoformat(iso_ts.replace('Z', '+00:00'))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - ts
        secs = int(delta.total_seconds())
        if secs < 60:
            return 'just now'
        if secs < 3600:
            return f'{secs // 60}m ago'
        if secs < 86400:
            return f'{secs // 3600}h ago'
        return f'{secs // 86400}d ago'
    except Exception:
        return 'recently'

def _finish_dashboard_update_cleanup(node_failures: list[str]) -> None:
    """Refresh managed dashboards or stop stale manual ones after an update."""
    if node_failures:
        print()
        print('  ℹ Leaving running dashboard process(es) untouched because the')
        print('    Node.js dependency refresh did not complete.')
        return
    stop_result = _m()._kill_stale_dashboard_processes(restart_managed=True)
    if not stop_result.get('unrecovered'):
        return
    print()
    print('⚠ A web dashboard/serve process was stopped during update and could not be auto-restarted.')
    print('  Re-launch it when you want the web UI back:')
    print('    duck-agent dashboard --port <port>')

def _atomic_replace_dir(src: str, dst: str) -> None:
    """Replace directory *dst* with *src* without leaving *dst* half-deleted.

    The naive ``rmtree(dst); copytree(src, dst)`` has a destructive window: if
    the copy fails partway (common on the Windows ZIP-update path, which only
    runs because file I/O is already flaky on that machine), the old directory
    is already gone and nothing replaced it — the install is left with a
    deleted tree (issue #49145, where ``ui-tui/`` vanished and broke the TUI).

    Now a thin single-entry alias over the two-phase helpers below, which
    generalise the same stage-then-swap discipline across every entry the ZIP
    update touches (#76104). Retained because it is part of the mechanical
    ``hermes_cli.main`` re-export surface and guards the #49145 regression.
    """
    _commit_staged_replacements([(_stage_replacement(src, dst), dst)])

def _stage_replacement(src: str, dst: str) -> str:
    """Copy *src* to a sibling staging path for *dst*; return the staging path.

    Phase 1 of the two-phase replace. Handles both directories and plain
    files. Touches nothing live, so a failure here leaves the whole install
    untouched.
    """
    staging = f'{dst}.duck-agent-update-staging'
    backup = f'{dst}.duck-agent-update-old'
    if not os.path.exists(dst) and os.path.exists(backup):
        os.rename(backup, dst)
    for leftover in (staging, backup):
        if os.path.isdir(leftover):
            shutil.rmtree(leftover, ignore_errors=True)
        elif os.path.exists(leftover):
            os.remove(leftover)
    if os.path.isdir(src):
        shutil.copytree(src, staging)
    else:
        shutil.copy2(src, staging)
    return staging

def _discard_staged(staged) -> None:
    """Remove staging paths for entries that were never committed.

    Without this a phase-1 failure (typically disk exhaustion) orphans one
    staging copy per entry already processed — up to a full second copy of
    the tree. The user then follows the "re-run `duck-agent update`" advice with
    *less* free space than before and the retry fails harder than the
    original attempt.
    """
    for staging, _dst in staged:
        try:
            if os.path.isdir(staging):
                shutil.rmtree(staging, ignore_errors=True)
            elif os.path.exists(staging):
                os.remove(staging)
        except OSError as exc:
            logger.warning('could not remove staging path %s: %s', staging, exc)

def _commit_staged_replacements(staged) -> None:
    """Phase 2: swap every staged entry into place, rolling back all on failure.

    ``_atomic_replace_dir`` makes each *individual* directory swap safe, but
    the ZIP update replaces ~90 top-level entries in a loop, and nothing made
    the loop atomic *as a whole*. A failure partway left some entries at the
    new version and the rest at the old one — every file valid Python, the
    combination unbootable (issue #76104; the ``ImportError`` in #76091 and
    the field report in #63717 are both this).

    This covers plain files as well as directories: the repo root holds 20
    first-party modules (``run_agent.py``, ``cli.py``, ``hermes_constants.py``
    …), so a files-only failure reproduces exactly the bug class we are
    closing. Every swap is an ``os.rename`` onto a path that was just moved
    aside — a same-filesystem rename is atomic on POSIX and NTFS alike, so a
    file swap can never leave a half-written module the way ``copy2`` onto a
    live path can.

    Splitting stage-all-then-swap-all shrinks the failure window from "the
    duration of a full tree copy" to "the duration of N renames", and makes
    the remaining window recoverable: if a swap fails we restore every entry
    already swapped, so the tree lands wholly new or wholly old.
    """
    swapped: list[tuple[str, str]] = []
    try:
        for staging, dst in staged:
            backup = f'{dst}.duck-agent-update-old'
            if os.path.exists(dst):
                os.rename(dst, backup)
                swapped.append((dst, backup))
            else:
                swapped.append((dst, ''))
            os.rename(staging, dst)
    except OSError:
        for dst, backup in reversed(swapped):
            try:
                if os.path.isdir(dst):
                    shutil.rmtree(dst, ignore_errors=True)
                elif os.path.exists(dst):
                    os.remove(dst)
                if backup and os.path.exists(backup):
                    os.rename(backup, dst)
            except OSError as exc:
                logger.warning('rollback failed for %s: %s', dst, exc)
        raise
    for _dst, backup in swapped:
        if backup and os.path.isdir(backup):
            shutil.rmtree(backup, ignore_errors=True)
        elif backup and os.path.exists(backup):
            try:
                os.remove(backup)
            except OSError:
                pass

def _print_update_completion(message: str) -> None:
    """Print an update outcome plus, when the dashboard launched this run
    with an action id, a terminal receipt line the Desktop can match after
    the dashboard restarts (see #47359 / #58764)."""
    print(message)
    action_id = os.environ.get('HERMES_ACTION_ID', '')
    if len(action_id) == 32 and all((char in '0123456789abcdef' for char in action_id)):
        print(f'=== duck-agent-update completed {action_id} ===')

def _update_via_zip(args):
    """Update Duck Agent by downloading a ZIP archive.

    Used on Windows when git file I/O is broken (antivirus, NTFS filter
    drivers causing 'Invalid argument' errors on file creation).
    """
    import tempfile
    import zipfile
    from urllib.request import urlretrieve
    branch = _m()._resolve_update_branch(args)
    if branch != 'main':
        print(f'✗ --branch={branch} is not supported on the Windows ZIP-fallback update path.')
        print(f'  This path runs when git file I/O is broken on the system. Either resolve the git-side breakage (typically an antivirus or NTFS filter holding files open) and rerun `duck-agent update --branch {branch}`, or update against main with `duck-agent update`.')
        _m().sys.exit(1)
    zip_url = f'https://github.com/NousResearch/duck-agent/archive/refs/heads/{branch}.zip'
    print('→ Downloading latest version...')
    tmp_dir = tempfile.mkdtemp(prefix='duck-agent-update-')
    try:
        zip_path = os.path.join(tmp_dir, f'duck-agent-{branch}.zip')
        urlretrieve(zip_url, zip_path)
        print('→ Extracting...')
        import stat as _stat
        with zipfile.ZipFile(zip_path, 'r') as zf:
            tmp_dir_real = os.path.realpath(tmp_dir)
            for member in zf.infolist():
                member_path = os.path.realpath(os.path.join(tmp_dir, member.filename))
                if not member_path.startswith(tmp_dir_real + os.sep) and member_path != tmp_dir_real:
                    raise ValueError(f'Zip-slip detected: {member.filename} escapes extraction directory')
                mode = member.external_attr >> 16 & 61440
                if _stat.S_ISLNK(mode):
                    raise ValueError(f'ZIP contains unsupported symlink member: {member.filename}')
            zf.extractall(tmp_dir)
        extracted = os.path.join(tmp_dir, f'duck-agent-{branch}')
        if not os.path.isdir(extracted):
            for d in os.listdir(tmp_dir):
                candidate = os.path.join(tmp_dir, d)
                if os.path.isdir(candidate) and d != '__MACOSX':
                    extracted = candidate
                    break
        preserve = {'venv', 'node_modules', '.git', '.env'}
        entries = [i for i in os.listdir(extracted) if i not in preserve]
        need = sum((os.path.getsize(os.path.join(dirpath, f)) for entry in entries for dirpath, _dirs, files in os.walk(os.path.join(extracted, entry)) for f in files)) + sum((os.path.getsize(os.path.join(extracted, e)) for e in entries if os.path.isfile(os.path.join(extracted, e))))
        required = int(need * 1.2)
        free = shutil.disk_usage(str(_m().PROJECT_ROOT)).free
        if free < required:
            raise RuntimeError(f'not enough free disk space to stage the update safely (need ~{required // (1024 * 1024)} MB, have {free // (1024 * 1024)} MB)')
        staged: list[tuple[str, str]] = []
        try:
            for item in entries:
                src = os.path.join(extracted, item)
                dst = os.path.join(str(_m().PROJECT_ROOT), item)
                staged.append((_stage_replacement(src, dst), dst))
        except Exception:
            _discard_staged(staged)
            raise
        try:
            _commit_staged_replacements(staged)
        except Exception:
            _discard_staged(staged)
            raise
        update_count = len(staged)
        print(f'✓ Updated {update_count} items from ZIP')
    except Exception as e:
        print(f'✗ ZIP update failed: {e}')
        print('  Your existing install was left in place.')
        print("  Re-run `duck-agent update` to retry; if the agent won't start, reinstall from https://duck-agent.nousresearch.com")
        _m().sys.exit(1)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    removed = _m()._clear_bytecode_cache(_m().PROJECT_ROOT)
    if removed:
        print(f"  ✓ Cleared {removed} stale __pycache__ director{('y' if removed == 1 else 'ies')}")
    _m()._record_bytecode_fingerprint()
    print('→ Updating Python dependencies...')
    from hermes_cli.managed_uv import ensure_uv, update_managed_uv
    update_managed_uv()
    uv_bin = ensure_uv()
    pip_cmd = [_m().sys.executable, '-m', 'pip']
    if not uv_bin:
        uv_bin = _ensure_uv_for_termux(pip_cmd)
    if uv_bin:
        uv_env = {**os.environ, 'VIRTUAL_ENV': str(_m().PROJECT_ROOT / 'venv')}
        if _m()._is_termux_env(uv_env):
            uv_env.pop('PYTHONPATH', None)
            uv_env.pop('PYTHONHOME', None)
        _m()._install_python_dependencies_with_optional_fallback([uv_bin, 'pip'], env=uv_env)
    else:
        try:
            subprocess.run(pip_cmd + ['--version'], cwd=_m().PROJECT_ROOT, check=True, capture_output=True)
        except subprocess.CalledProcessError:
            subprocess.run([_m().sys.executable, '-m', 'ensurepip', '--upgrade', '--default-pip'], cwd=_m().PROJECT_ROOT, check=True)
        _m()._install_python_dependencies_with_optional_fallback(pip_cmd)
    _m()._refresh_active_memory_provider_dependencies()
    import_ok, failing_module, import_error = _validate_critical_modules_import(_m().PROJECT_ROOT)
    if not import_ok:
        print()
        print('✗ Update left the install in an unimportable state:')
        print(f'  {failing_module}: {import_error}')
        print()
        print('  This usually means the copy was interrupted partway through.')
        print('  Re-run `duck-agent update` to complete it.')
        _m().sys.exit(1)
    node_failures = _update_node_dependencies()
    _m()._build_web_ui(_m().PROJECT_ROOT / 'web')
    try:
        from tools.skills_sync import sync_skills
        print('→ Syncing bundled skills...')
        result = sync_skills(quiet=True)
        if result['copied']:
            print(f"  + {len(result['copied'])} new: {', '.join(result['copied'])}")
        if result.get('updated'):
            print(f"  ↑ {len(result['updated'])} updated: {', '.join(result['updated'])}")
        if result.get('user_modified'):
            print(f"  ~ {len(result['user_modified'])} user-modified (kept)")
            print('    → see them: duck-agent skills list-modified  (diff/reset to resume updates)')
        if result.get('cleaned'):
            print(f"  − {len(result['cleaned'])} removed from manifest")
        if result.get('relocated'):
            print(f"  → {len(result['relocated'])} moved to new upstream paths: {', '.join(result['relocated'])}")
        if not result['copied'] and (not result.get('updated')):
            print('  ✓ Skills are up to date')
    except Exception:
        pass
    try:
        from hermes_cli.model_catalog import seed_cache_from_checkout
        if seed_cache_from_checkout(_m().PROJECT_ROOT):
            print('  ✓ Model catalog cache refreshed from checkout')
    except Exception as e:
        logger.debug('Model catalog seed during zip update failed: %s', e)
    try:
        from hermes_cli.backup import _quick_snapshot_root, verify_sqlite_integrity
        _state_path = get_hermes_home() / 'state.db'
        if _state_path.exists():
            _state_ok = verify_sqlite_integrity(_state_path, check_header=True, run_pragma=True)
            if not _state_ok.get('valid'):
                print()
                print('⚠ state.db is corrupted after update: ' + _state_ok.get('message', 'unknown error'))
                _snap_root = _quick_snapshot_root(get_hermes_home())
                if _snap_root.exists():
                    _snap_dirs = sorted((d for d in _snap_root.iterdir() if d.is_dir()), reverse=True)
                    for _snap_dir in _snap_dirs:
                        _snap_state = _snap_dir / 'state.db'
                        if _snap_state.exists():
                            _snap_ok = verify_sqlite_integrity(_snap_state, check_header=True, run_pragma=True)
                            if _snap_ok.get('valid'):
                                try:
                                    import shutil as _shutil
                                    _shutil.copy2(_snap_state, _state_path)
                                    _restored_ok = verify_sqlite_integrity(_state_path, check_header=True, run_pragma=True)
                                    if _restored_ok.get('valid'):
                                        print(f'  ✓ Auto-restored from snapshot {_snap_dir.name}')
                                    else:
                                        print('  ✗ Auto-restore FAILED — restored copy also failed integrity')
                                    break
                                except OSError as _exc:
                                    print(f'  ✗ Auto-restore file copy failed: {_exc}')
                                    break
    except Exception as exc:
        logger.debug('Post-update state.db integrity check (zip path) failed: %s', exc)
    print()
    if node_failures:
        print(f"⚠ Update partially complete — Node.js dependencies for {', '.join(node_failures)} did not refresh.")
        print('  Code and Python deps are updated, but the dashboard/TUI may')
        print('  be in a mixed state until the Node deps are rebuilt.')
    else:
        _print_update_completion('✓ Update complete!')
    try:
        _print_curator_first_run_notice()
    except Exception as e:
        logger.debug('Curator first-run notice failed: %s', e)
    try:
        _print_curator_recent_run_notice()
    except Exception as e:
        logger.debug('Curator recent-run notice failed: %s', e)
    _finish_dashboard_update_cleanup(node_failures)

def _stash_local_changes_if_needed(git_cmd: list[str], cwd: Path) -> Optional[str]:
    status = subprocess.run(git_cmd + ['status', '--porcelain'], cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace', check=True)
    if not status.stdout.strip():
        return None
    unmerged = subprocess.run(git_cmd + ['ls-files', '--unmerged'], cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if unmerged.stdout.strip():
        print('→ Clearing unmerged index entries from a previous conflict...')
        subprocess.run(git_cmd + ['reset'], cwd=cwd, capture_output=True)
    from datetime import datetime, timezone
    stash_name = datetime.now(timezone.utc).strftime('duck-agent-update-autostash-%Y%m%d-%H%M%S')
    print('→ Local changes detected — stashing before update...')
    prev_stash = subprocess.run(git_cmd + ['rev-parse', '--verify', 'refs/stash'], cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace').stdout.strip()
    push = subprocess.run(git_cmd + ['stash', 'push', '--include-untracked', '-m', stash_name], cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if push.stdout.strip():
        print(push.stdout.strip())
    stash_probe = subprocess.run(git_cmd + ['rev-parse', '--verify', 'refs/stash'], cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    stash_ref = stash_probe.stdout.strip()
    stash_created = stash_probe.returncode == 0 and bool(stash_ref) and (stash_ref != prev_stash)
    if push.returncode != 0:
        if stash_created:
            if push.stderr.strip():
                print(push.stderr.strip())
            print('  ⚠ Some untracked files could not be removed from the working tree (permission denied).')
            print('    They were still saved to the stash and were left in place — the update will continue.')
            subprocess.run(git_cmd + ['reset', '--hard', 'HEAD'], cwd=cwd, capture_output=True)
        else:
            print('✗ Could not stash local changes — update aborted.')
            if push.stderr.strip():
                print(f'  {push.stderr.strip().splitlines()[0]}')
            print('  Commit, stash, or clean up your local changes manually, then re-run `duck-agent update`.')
            raise subprocess.CalledProcessError(push.returncode, push.args, output=push.stdout, stderr=push.stderr)
    return stash_ref

def _resolve_stash_selector(git_cmd: list[str], cwd: Path, stash_ref: str) -> Optional[str]:
    stash_list = subprocess.run(git_cmd + ['stash', 'list', '--format=%gd %H'], cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace', check=True)
    for line in stash_list.stdout.splitlines():
        selector, _, commit = line.partition(' ')
        if commit.strip() == stash_ref:
            return selector.strip()
    return None

def _print_stash_cleanup_guidance(stash_ref: str, stash_selector: Optional[str]=None) -> None:
    print("  Check `git status` first so you don't accidentally reapply the same change twice.")
    print("  Find the saved entry with: git stash list --format='%gd %H %s'")
    if stash_selector:
        print(f'  Remove it with: git stash drop {stash_selector}')
    else:
        print(f'  Look for commit {stash_ref}, then drop its selector with: git stash drop stash@{{N}}')

def _stash_apply_failed_only_on_existing_untracked(stderr: str) -> bool:
    """True when a ``git stash apply`` failure is ONLY about untracked files
    that already exist in the working tree.

    This is the tail end of the permission-denied autostash class: ``git stash
    push --include-untracked`` swept undeletable files (e.g. a root-owned
    ``packaging/`` directory) into the stash but could not remove them from
    disk.  On restore, git applies all tracked changes, then refuses to
    overwrite those still-present files (``already exists, no checkout`` /
    ``could not restore untracked files from stash``) and exits non-zero even
    though nothing was lost.  Any other error line (e.g. ``would be
    overwritten by merge`` / ``Aborting``) means the tracked apply itself
    failed and this returns False.
    """
    lines = [ln.strip() for ln in (stderr or '').splitlines() if ln.strip()]
    if not lines:
        return False
    saw_untracked_error = False
    for ln in lines:
        if 'already exists, no checkout' in ln:
            saw_untracked_error = True
        elif 'could not restore untracked files from stash' in ln:
            saw_untracked_error = True
        elif ln.startswith(('warning:', 'hint:')):
            continue
        else:
            return False
    return saw_untracked_error

def _restore_stashed_changes(git_cmd: list[str], cwd: Path, stash_ref: str, prompt_user: bool=False, input_fn=None) -> bool:
    if prompt_user:
        print()
        print('⚠ Local changes were stashed before updating.')
        print('  Restoring them may reapply local customizations onto the updated codebase.')
        print('  Review the result afterward if Duck Agent behaves unexpectedly.')
        print('Restore local changes now? [Y/n]')
        if input_fn is not None:
            response = input_fn('Restore local changes now? [Y/n]', 'y')
        else:
            response = input().strip().lower()
        if response not in {'', 'y', 'yes'}:
            print('Skipped restoring local changes.')
            print('Your changes are still preserved in git stash.')
            print(f'Restore manually with: git stash apply {stash_ref}')
            return False
    print('→ Restoring local changes...')
    restore = subprocess.run(git_cmd + ['stash', 'apply', stash_ref], cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    unmerged = subprocess.run(git_cmd + ['diff', '--name-only', '--diff-filter=U'], cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    has_conflicts = bool(unmerged.stdout.strip())
    if restore.returncode != 0 and (not has_conflicts) and _stash_apply_failed_only_on_existing_untracked(restore.stderr):
        print('  ⚠ Some stashed untracked files already exist in the working tree and were kept as-is.')
    elif restore.returncode != 0 or has_conflicts:
        print('✗ Update pulled new code, but restoring local changes hit conflicts.')
        if restore.stdout.strip():
            print(restore.stdout.strip())
        if restore.stderr.strip():
            print(restore.stderr.strip())
        conflicted_files = unmerged.stdout.strip()
        if conflicted_files:
            print('\nConflicted files:')
            for f in conflicted_files.splitlines():
                print(f'  • {f}')
        print('\nYour stashed changes are preserved — nothing is lost.')
        print(f'  Stash ref: {stash_ref}')
        subprocess.run(git_cmd + ['reset', '--hard', 'HEAD'], cwd=cwd, capture_output=True)
        print('Working tree reset to clean state.')
        print(f'Restore your changes later with: git stash apply {stash_ref}')
        return False
    stash_selector = _resolve_stash_selector(git_cmd, cwd, stash_ref)
    if stash_selector is None:
        print("⚠ Local changes were restored, but Duck Agent couldn't find the stash entry to drop.")
        print('  The stash was left in place. You can remove it manually after checking the result.')
        _print_stash_cleanup_guidance(stash_ref)
    else:
        drop = subprocess.run(git_cmd + ['stash', 'drop', stash_selector], cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace')
        if drop.returncode != 0:
            print("⚠ Local changes were restored, but Duck Agent couldn't drop the saved stash entry.")
            if drop.stdout.strip():
                print(drop.stdout.strip())
            if drop.stderr.strip():
                print(drop.stderr.strip())
            print('  The stash was left in place. You can remove it manually after checking the result.')
            _print_stash_cleanup_guidance(stash_ref, stash_selector)
    print('⚠ Local changes were restored on top of the updated codebase.')
    print('  Review `git diff` / `git status` if Duck Agent behaves unexpectedly.')
    return True

def _discard_stashed_changes(git_cmd: list[str], cwd: Path, stash_ref: str) -> bool:
    """Throw away a stash created before an update, without applying it.

    Used only on a NON-interactive update when the user has set
    ``updates.non_interactive_local_changes: discard`` — i.e. they've opted out
    of keeping local source edits on this machine. Drops the stash entry
    instead of re-applying it, so the working tree stays clean at the freshly
    pulled HEAD. Unlike ``git reset --hard`` + ``git clean -fd``, this only
    affects what was stashed (tracked changes + the untracked files we
    explicitly captured) — ignored paths like node_modules/venv/build outputs
    are never touched, since they were never stashed.

    Returns True if the stash was dropped, False on a git failure (in which
    case the stash is left in place for safety).
    """
    stash_selector = _resolve_stash_selector(git_cmd, cwd, stash_ref)
    if stash_selector is None:
        print("⚠ Configured to discard local changes on non-interactive update, but Duck Agent couldn't find the stash entry to drop.")
        _print_stash_cleanup_guidance(stash_ref)
        return False
    drop = subprocess.run(git_cmd + ['stash', 'drop', stash_selector], cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if drop.returncode != 0:
        print("⚠ Configured to discard local changes, but Duck Agent couldn't drop the saved stash entry.")
        if drop.stderr.strip():
            print(f'  {drop.stderr.strip().splitlines()[0]}')
        _print_stash_cleanup_guidance(stash_ref, stash_selector)
        return False
    print('→ Discarded local source changes (updates.non_interactive_local_changes=discard).')
    return True
OFFICIAL_REPO_URLS = {'https://github.com/NousResearch/duck-agent.git', 'git@github.com:NousResearch/duck-agent.git', 'https://github.com/NousResearch/duck-agent', 'git@github.com:NousResearch/duck-agent'}
OFFICIAL_REPO_URL = 'https://github.com/NousResearch/duck-agent.git'
SKIP_UPSTREAM_PROMPT_FILE = '.skip_upstream_prompt'

def _get_origin_url(git_cmd: list[str], cwd: Path) -> Optional[str]:
    """Get the URL of the origin remote, or None if not set."""
    try:
        result = subprocess.run(git_cmd + ['remote', 'get-url', 'origin'], cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace')
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None

def _is_fork(origin_url: Optional[str]) -> bool:
    """Check if the origin remote points to a fork (not the official repo)."""
    if not origin_url:
        return False
    normalized = origin_url.rstrip('/')
    if normalized.endswith('.git'):
        normalized = normalized[:-4]
    for official in OFFICIAL_REPO_URLS:
        official_normalized = official.rstrip('/')
        if official_normalized.endswith('.git'):
            official_normalized = official_normalized[:-4]
        if normalized == official_normalized:
            return False
    return True

def _has_upstream_remote(git_cmd: list[str], cwd: Path) -> bool:
    """Check if an 'upstream' remote already exists."""
    try:
        result = subprocess.run(git_cmd + ['remote', 'get-url', 'upstream'], cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace')
        return result.returncode == 0
    except Exception:
        return False

def _add_upstream_remote(git_cmd: list[str], cwd: Path) -> bool:
    """Add the official repo as the 'upstream' remote. Returns True on success."""
    try:
        result = subprocess.run(git_cmd + ['remote', 'add', 'upstream', OFFICIAL_REPO_URL], cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace')
        return result.returncode == 0
    except Exception:
        return False

def _count_commits_between(git_cmd: list[str], cwd: Path, base: str, head: str) -> int:
    """Count commits on `head` that are not on `base`. Returns -1 on error."""
    try:
        result = subprocess.run(git_cmd + ['rev-list', '--count', f'{base}..{head}'], cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace')
        if result.returncode == 0:
            return int(result.stdout.strip())
    except Exception:
        pass
    return -1

def _should_skip_upstream_prompt() -> bool:
    """Check if user previously declined to add upstream."""
    from hermes_constants import get_hermes_home
    return (get_hermes_home() / SKIP_UPSTREAM_PROMPT_FILE).exists()

def _mark_skip_upstream_prompt():
    """Create marker file to skip future upstream prompts."""
    try:
        from hermes_constants import get_hermes_home
        (get_hermes_home() / SKIP_UPSTREAM_PROMPT_FILE).touch()
    except Exception:
        pass

def _sync_fork_with_upstream(git_cmd: list[str], cwd: Path) -> bool:
    """Attempt to push updated main to origin (sync fork).

    Returns True if push succeeded, False otherwise.
    """
    try:
        result = subprocess.run(git_cmd + ['push', 'origin', 'main', '--force-with-lease'], cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace')
        return result.returncode == 0
    except Exception:
        return False

def _sync_with_upstream_if_needed(git_cmd: list[str], cwd: Path) -> None:
    """Check if fork is behind upstream and sync if safe.

    This implements the fork upstream sync logic:
    - If upstream remote doesn't exist, ask user if they want to add it
    - Compare origin/main with upstream/main
    - If origin/main is strictly behind upstream/main, pull from upstream
    - Try to sync fork back to origin if possible
    """
    has_upstream = _has_upstream_remote(git_cmd, cwd)
    if not has_upstream:
        if _should_skip_upstream_prompt():
            return
        print()
        print('ℹ Your fork is not tracking the official Duck Agent repository.')
        print('  This means you may miss updates from NousResearch/duck-agent.')
        print()
        try:
            response = input("Add official repo as 'upstream' remote? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            response = 'n'
        if response in {'', 'y', 'yes'}:
            print('→ Adding upstream remote...')
            if _add_upstream_remote(git_cmd, cwd):
                print('  ✓ Added upstream: https://github.com/NousResearch/duck-agent.git')
                has_upstream = True
            else:
                print('  ✗ Failed to add upstream remote. Skipping upstream sync.')
                return
        else:
            print("  Skipped. Run 'git remote add upstream https://github.com/NousResearch/duck-agent.git' to add later.")
            _mark_skip_upstream_prompt()
            return
    print()
    print('→ Fetching upstream...')
    try:
        subprocess.run(git_cmd + ['fetch', 'upstream', 'main', '--quiet'], cwd=cwd, capture_output=True, check=True)
    except subprocess.CalledProcessError:
        print('  ✗ Failed to fetch upstream. Skipping upstream sync.')
        return
    origin_ahead = _count_commits_between(git_cmd, cwd, 'upstream/main', 'origin/main')
    upstream_ahead = _count_commits_between(git_cmd, cwd, 'origin/main', 'upstream/main')
    if origin_ahead < 0 or upstream_ahead < 0:
        print('  ✗ Could not compare branches. Skipping upstream sync.')
        return
    if origin_ahead > 0:
        print()
        print(f'ℹ Your fork has {origin_ahead} commit(s) not on upstream.')
        print('  Skipping upstream sync to preserve your changes.')
        print('  If you want to merge upstream changes, run:')
        print('    git pull upstream main')
        return
    if upstream_ahead == 0:
        print('  ✓ Fork is up to date with upstream')
        return
    print()
    print(f'→ Fork is {upstream_ahead} commit(s) behind upstream')
    print('→ Pulling from upstream...')
    try:
        subprocess.run(git_cmd + ['pull', '--ff-only', 'upstream', 'main'], cwd=cwd, check=True)
    except subprocess.CalledProcessError:
        print('  ✗ Failed to pull from upstream. You may need to resolve conflicts manually.')
        return
    print('  ✓ Updated from upstream')
    print('→ Syncing fork...')
    if _sync_fork_with_upstream(git_cmd, cwd):
        print('  ✓ Fork synced with upstream')
    else:
        print("  ℹ Got updates from upstream but couldn't push to fork (no write access?)")
        print('    Your local repo is updated, but your fork on GitHub may be behind.')

def _invalidate_update_cache():
    """Delete the update-check cache for ALL profiles so no banner
    reports a stale "commits behind" count after a successful update.

    The git repo is shared across profiles — when one profile runs
    ``duck-agent update``, every profile is now current.
    """
    homes = []
    from hermes_constants import get_default_hermes_root
    default_home = get_default_hermes_root()
    homes.append(default_home)
    profiles_root = default_home / 'profiles'
    if profiles_root.is_dir():
        for entry in profiles_root.iterdir():
            if entry.is_dir():
                homes.append(entry)
    for home in homes:
        try:
            cache_file = home / '.update_check'
            if cache_file.exists():
                cache_file.unlink()
        except Exception:
            pass

def _write_marker_file(path: Path, *, label: str) -> None:
    """Drop an update-recovery breadcrumb. Never raises."""
    if _m()._pytest_owns_live_checkout(path.parent):
        logger.debug('Skipping %s marker under pytest (live checkout)', label)
        return
    try:
        path.write_text(f'started={_time.time()}\npid={os.getpid()}\n', encoding='utf-8')
    except OSError as exc:
        logger.debug('Could not write %s marker: %s', label, exc)

def _write_update_incomplete_marker() -> None:
    """Drop the interrupted core-install breadcrumb. Never raises."""
    _write_marker_file(_m()._update_marker_path(), label='update-incomplete')

def _write_lazy_refresh_incomplete_marker() -> None:
    """Drop the interrupted lazy-refresh breadcrumb. Never raises."""
    _write_marker_file(_m()._lazy_refresh_marker_path(), label='lazy-refresh-incomplete')

def _format_concurrent_instances_message(matches: list[tuple[int, str]], scripts_dir: Path) -> str:
    """Build a human-readable explanation + remediation hint for the user."""
    shim = scripts_dir / 'duck-agent.exe'
    lines = ['✗ Another duck-agent.exe is running:']
    for pid, name in matches:
        lines.append(f'    PID {pid}  {name}')
    lines.append('')
    lines.append(f'  Updating now would fail to overwrite {shim} because')
    lines.append('  Windows blocks REPLACE on a running executable.')
    lines.append('')
    lines.append('  Close Duck Agent Desktop, exit any open `duck-agent` REPLs, and')
    lines.append('  stop the gateway (`duck-agent gateway stop`) before retrying.')
    lines.append('')
    if matches:
        pid_args = ' '.join((f'/PID {pid}' for pid, _ in matches))
        lines.append("  If you've already closed everything and these PIDs are")
        lines.append('  stale, terminate them directly, then retry the update:')
        lines.append(f'      taskkill {pid_args} /F')
        lines.append('')
    lines.append("  Override with `duck-agent update --force` if you've already")
    lines.append('  confirmed those processes will not write to the venv.')
    return '\n'.join(lines)

def _upgrade_pip_before_lazy_refresh(install_cmd_prefix: list[str], *, env: dict[str, str] | None=None) -> None:
    """Upgrade pip before lazy-backend refreshes.

    Older pip (e.g. 24.0 on Python 3.11) can fail setuptools-backed source
    builds during lazy installs and leave a partially-written venv (#57828).
    Never raises.
    """
    try:
        _m()._run_package_only_install(install_cmd_prefix + ['install', '--upgrade', 'pip'], env=env)
    except subprocess.CalledProcessError as exc:
        logger.debug('pip upgrade before lazy refresh failed: %s', exc)

def _refresh_active_lazy_features(install_cmd_prefix: list[str] | None=None, *, env: dict[str, str] | None=None) -> bool:
    """Refresh lazy-installed backends after a code update.

    When pyproject.toml's ``[all]`` extra was slimmed down (May 2026), most
    optional backends moved to ``tools/lazy_deps.py`` and only install on
    first use. ``duck-agent update`` runs ``uv pip install -e .[all]`` which
    leaves those packages untouched — so if we bump a pin in
    :data:`LAZY_DEPS` (CVE response, transitive bug fix), users who already
    activated the backend keep the stale version forever.

    This function asks lazy_deps which features the user has previously
    activated and reinstalls them under the current pins. Features the
    user never enabled stay quiet — no churn for cold backends.

    Returns True when the venv is safe to use (refresh succeeded, or no
    active lazy backends, or post-failure import repair succeeded). Returns
    False when a failed lazy install left broken core imports that automatic
    repair could not fix (#57828).

    Never raises. A failure here must not block the rest of the update.
    """
    try:
        from tools import lazy_deps
    except Exception as exc:
        logger.debug('Lazy refresh skipped (import failed): %s', exc)
        return True
    try:
        active = lazy_deps.active_features()
    except Exception as exc:
        logger.debug('Lazy refresh skipped (active_features failed): %s', exc)
        return True
    if not active:
        return True
    print()
    print(f'→ Refreshing {len(active)} active lazy backend(s)...')
    unexpected_failure = False
    try:
        results = lazy_deps.refresh_active_features(prompt=False)
    except Exception as exc:
        print(f'  ⚠ Lazy refresh failed unexpectedly: {exc}')
        results = {}
        unexpected_failure = True
    refreshed = [f for f, s in results.items() if s == 'refreshed']
    current = [f for f, s in results.items() if s == 'current']
    failed = [(f, s) for f, s in results.items() if s.startswith('failed:')]
    skipped = [(f, s) for f, s in results.items() if s.startswith('skipped:')]
    if refreshed:
        print(f"  ↑ {len(refreshed)} refreshed: {', '.join(refreshed)}")
    if current:
        print(f'  ✓ {len(current)} already current')
    if skipped:
        names = ', '.join((f for f, _ in skipped))
        reason = skipped[0][1].split(': ', 1)[-1]
        print(f'  · {len(skipped)} skipped ({reason}): {names}')
    if not failed and (not unexpected_failure):
        return True
    for feature, status in failed:
        reason = status.split(': ', 1)[-1]
        if len(reason) > 200:
            reason = reason[:200] + '...'
        print(f'  ⚠ {feature} failed to refresh: {reason}')
    if install_cmd_prefix is None:
        print('  ⚠ Lazy refresh failed; rerun `duck-agent update` once resolved.')
        return False
    status = _m()._repair_venv_via_import_probes(install_cmd_prefix, env=env)
    if status == 'repaired':
        print('  Lazy backend(s) keep their previous version until refresh succeeds.')
        return True
    if status == 'healthy':
        print('  Lazy backend(s) keep their previous version; probed packages look intact.')
        print('  Rerun `duck-agent update` once the upstream issue is resolved.')
        return True
    if status == 'indeterminate':
        print('  ⚠ Leaving `.lazy-refresh-incomplete` until import probes can confirm health.')
    return False

def _refresh_active_memory_provider_dependencies() -> None:
    """Refresh pip dependencies for the configured external memory provider.

    Memory-provider bridge packages are declared in each provider's
    ``plugin.yaml`` (plus mode-dependent extras like Hindsight's
    ``hindsight-all``), NOT in Duck Agent' editable-install extras or
    ``LAZY_DEPS`` alone — so the core dependency reinstall above can strip
    or downgrade them (#53272 mem0ai, #70636 hindsight-embed). Re-run the
    provider's declared install for the ACTIVE provider only, after the
    core install and lazy refresh, so the last write to any shared package
    is the one the active provider needs.

    Never raises. A failure here must not block the rest of the update.
    """
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
    except Exception as exc:
        logger.debug('Memory provider refresh skipped (config load failed): %s', exc)
        return
    provider = ''
    if isinstance(cfg, dict):
        memory_cfg = cfg.get('memory')
        if isinstance(memory_cfg, dict):
            if memory_cfg.get('enabled') is False:
                return
            provider = str(memory_cfg.get('provider') or '').strip()
    if not provider or provider in {'default', 'builtin', 'none'}:
        return
    try:
        from hermes_cli.memory_setup import _install_dependencies
    except Exception as exc:
        logger.debug('Memory provider refresh skipped (import failed): %s', exc)
        return
    print()
    print(f'→ Refreshing active memory provider dependencies ({provider})...')
    try:
        _install_dependencies(provider, force=True)
    except Exception as exc:
        print(f'  ⚠ {provider} dependencies failed to refresh: {exc}')

def _is_android_python() -> bool:
    return _m().sys.platform == 'android'

def _install_psutil_android_compat(install_cmd_prefix: list[str], *, env: dict[str, str] | None=None) -> None:
    """Install psutil on Android by patching upstream platform detection.

    psutil's setup currently gates Linux sources behind
    ``sys.platform.startswith('linux')``. On Termux Python reports
    ``sys.platform == 'android'``, so setup aborts with
    "platform android is not supported" despite compiling fine when using the
    Linux source path.

    We patch only the extracted build tree used for this install attempt;
    nothing is persisted in the repository.

    Stopgap: remove this once https://github.com/giampaolo/psutil/pull/2762
    merges and ships in a release. The standalone installer script uses the
    same shared helper and should be removed together.
    """
    import tempfile
    import urllib.request
    from hermes_cli.psutil_android import PSUTIL_URL, prepare_patched_psutil_sdist
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / 'psutil.tar.gz'
        urllib.request.urlretrieve(PSUTIL_URL, archive)
        src_root = prepare_patched_psutil_sdist(archive, tmp_path)
        _m()._run_install_with_heartbeat(install_cmd_prefix + ['install', '--no-build-isolation', str(src_root)], env=env)

def _ensure_uv_for_termux(pip_cmd: list[str]) -> str | None:
    """Best-effort uv bootstrap on Termux for faster update installs.

    The normal path (``ensure_uv()`` in managed_uv) installs the managed
    standalone uv into ``$DUCK_AGENT_HOME/bin/uv``, but on Termux the official
    installer may not work (glibc vs bionic).  Prefer a uv already on PATH
    (e.g. ``pkg install uv``); only if there is none do we fall back to a
    wheel-only ``pip install uv`` so we never source-build the Rust crate.
    """
    from hermes_cli.managed_uv import resolve_uv
    existing = resolve_uv()
    if existing:
        return existing
    if not _m()._is_termux_env():
        return None
    system_uv = shutil.which('uv')
    if system_uv:
        return system_uv
    try:
        print('  → Termux detected: trying to install uv for faster dependency updates...')
        result = subprocess.run(pip_cmd + ['install', 'uv', '--only-binary', ':all:'], cwd=_m().PROJECT_ROOT, check=False)
        if result.returncode != 0:
            return None
    except Exception:
        pass
    return resolve_uv() or shutil.which('uv')

def _npm_manifest_paths() -> tuple[Path, ...]:
    """Manifests whose changes must defeat the update-skip.

    The lockfile alone is NOT a sufficient key: on a local checkout a dev
    can edit package.json (root or a workspace) without running npm — the
    lockfile is then unchanged but `duck-agent update` is exactly the step
    expected to sync node_modules (via the `npm install` fallback in
    _run_npm_install_deterministic).

    The workspace list is pulled from the root package.json's `workspaces`
    globs (npm's own source of truth) rather than hardcoded, so adding a
    workspace can never silently escape the skip key. The root install
    (step 1, --workspaces=false) still hoists shared deps for EVERY
    workspace — desktop included — so all of them belong in the key, not
    just the ones step 2 installs. Falls back to hashing just root
    manifests if package.json is unreadable (never skips more than main
    would have installed).
    """
    root_pkg = _m().PROJECT_ROOT / 'package.json'
    paths = [_m().PROJECT_ROOT / 'package-lock.json', root_pkg]
    try:
        workspaces = json.loads(root_pkg.read_text(encoding='utf-8')).get('workspaces', [])
        if isinstance(workspaces, dict):
            workspaces = workspaces.get('packages', [])
        for pattern in workspaces:
            for match in sorted(_m().PROJECT_ROOT.glob(str(pattern))):
                manifest = match / 'package.json'
                if manifest.is_file():
                    paths.append(manifest)
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return tuple(paths)

def _npm_manifests_digest() -> str | None:
    """Combined sha256 over the lockfile + all workspace package.json files.

    Returns None when the lockfile is missing (never skip then).
    """
    if not (_m().PROJECT_ROOT / 'package-lock.json').exists():
        return None
    h = hashlib.sha256()
    for p in _npm_manifest_paths():
        h.update(str(p.relative_to(_m().PROJECT_ROOT)).encode())
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b'<missing>')
    return h.hexdigest()

def _npm_lockfile_changed(hermes_root: Path) -> bool:
    current = _npm_manifests_digest()
    if current is None:
        return True
    if not (_m().PROJECT_ROOT / 'node_modules').is_dir():
        return True
    web_dir = _m().PROJECT_ROOT / 'web'
    if (web_dir / 'package.json').is_file() and (not _web_build_toolchain_ready(*_web_toolchain_roots(web_dir))):
        return True
    try:
        cache_key = hashlib.sha256(str(_m().PROJECT_ROOT).encode()).hexdigest()[:12]
        cache_file = hermes_root / f'.npm_lock_hash_{cache_key}'
        if not cache_file.exists():
            return True
        return cache_file.read_text(encoding='utf-8').strip() != current
    except OSError:
        return True

def _record_npm_lockfile_hash(hermes_root: Path) -> None:
    digest = _npm_manifests_digest()
    if digest is None:
        return
    try:
        cache_key = hashlib.sha256(str(_m().PROJECT_ROOT).encode()).hexdigest()[:12]
        cache_file = hermes_root / f'.npm_lock_hash_{cache_key}'
        cache_file.write_text(digest, encoding='utf-8')
    except OSError:
        logger.debug('Could not write npm lockfile hash cache')

def _update_node_dependencies() -> list[str]:
    """Refresh Node deps in the repo root and update workspaces.

    Returns the list of labels whose npm install failed (empty on success),
    so the caller can treat a Node refresh failure as a partial update rather
    than silently reporting ``Update complete!`` (#30271).
    """
    if not (_m().PROJECT_ROOT / 'package.json').exists():
        return []
    npm = _m()._resolve_node_runtime_npm()
    if not npm:
        from hermes_constants import is_wsl
        path_npm = shutil.which('npm')
        if is_wsl() and path_npm and _m()._is_windows_npm_path(path_npm):
            print('→ Updating Node.js dependencies...')
            print('  ⚠ Skipped: only a Windows npm is reachable from this WSL shell.')
            print("    Install Node.js inside the WSL distro (nvm, or your distro's")
            print('    package manager), then re-run `duck-agent update`.')
            failed = ['repo root']
            if any(((_m().PROJECT_ROOT / workspace / 'package.json').exists() for workspace in ('ui-tui', 'web'))):
                failed.append('ui-tui, web workspaces')
            return failed
        return []
    from hermes_constants import get_default_hermes_root
    shared_hermes_root = get_default_hermes_root()
    if not _m()._npm_lockfile_changed(shared_hermes_root):
        logger.info('npm lockfile unchanged, skipping npm install')
        return []
    print('→ Updating Node.js dependencies...')

    def _partial_update_failure(*labels: str) -> list[str]:
        print()
        print('  ⚠ Node.js dependency refresh did not complete cleanly; the')
        print('    installation may be in a mixed state (updated code, stale Node')
        print('    deps). Fix npm and re-run `duck-agent update`.')
        return list(labels)
    extra_args = ['--no-fund', '--no-audit', '--prefer-offline', '--progress=false']
    from hermes_constants import with_hermes_node_path
    nixos_env = with_hermes_node_path(_m()._nixos_build_env())
    root_args = [*extra_args, '--workspaces=false']
    root_result = _m()._run_npm_install_deterministic(npm, _m().PROJECT_ROOT, extra_args=tuple(root_args), capture_output=False, env=nixos_env)
    if root_result.returncode != 0:
        print('  ⚠ npm install failed in repo root')
        stderr = (root_result.stderr or '').strip() if root_result.stderr else ''
        if stderr:
            print(f'    {stderr.splitlines()[-1]}')
        return _partial_update_failure('repo root')
    ws_args = [*extra_args, '--workspace', 'ui-tui', '--workspace', 'web']
    ws_result = _m()._run_npm_install_deterministic(npm, _m().PROJECT_ROOT, extra_args=tuple(ws_args), capture_output=False, env=nixos_env)
    if ws_result.returncode == 0:
        _record_npm_lockfile_hash(shared_hermes_root)
        print('  ✓ repo root + ui-tui, web workspaces (desktop skipped)')
        return []
    print('  ⚠ npm workspace install failed')
    stderr = (ws_result.stderr or '').strip() if ws_result.stderr else ''
    if stderr:
        print(f'    {stderr.splitlines()[-1]}')
    return _partial_update_failure('ui-tui, web workspaces')

def _log_only_write(text: str) -> None:
    """Write ``text`` to ``~/.duck-agent/logs/update.log`` only, never the terminal.

    During ``duck-agent update`` ``sys.stdout`` is an ``_UpdateOutputStream`` that
    mirrors to both the terminal and ``update.log``. Loud, low-signal
    subprocess output (npm installs, the Electron/vite build, the cua-driver
    installer's "Next steps" wall) should be captured and tucked into the log
    so failures stay debuggable, without flooding the user's terminal. This
    reaches past the mirroring stream straight to the underlying log handle.
    """
    if not text:
        return
    stream = _m().sys.stdout
    log_file = getattr(stream, '_log', None)
    if log_file is None:
        return
    try:
        log_file.write(text if text.endswith('\n') else text + '\n')
        log_file.flush()
    except Exception:
        pass

def _run_logged_subprocess(cmd, *, cwd=None, env=None):
    """Run ``cmd`` capturing combined output into update.log (not the terminal).

    Returns the ``CompletedProcess`` (with ``stdout`` populated) so the caller
    can decide whether to surface the captured output on failure.
    """
    result = subprocess.run(cmd, cwd=cwd, env=env, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
    _log_only_write(result.stdout or '')
    return result

def _cmd_update_check(branch: str='main', *, branch_explicit: bool=False):
    """Implement ``duck-agent update --check``: fetch and report without installing.

    ``branch`` selects which branch the check compares against. Default is
    "main"; callers can pass another branch to ask "are there new commits
    on origin/<branch>?" without performing the update.

    ``branch_explicit`` is True iff the caller passed --branch on the CLI.
    Installs that can't honor non-default branches (e.g. Docker) surface a
    one-line notice instead of silently dropping the flag.
    """
    from hermes_cli.config import detect_install_method, recommended_update_command_for_method
    method = detect_install_method(_m().PROJECT_ROOT)
    if method == 'docker':
        from hermes_cli.config import format_docker_update_message
        print(format_docker_update_message())
        sys.exit(1)
    if method in {'nix', 'nixos'}:
        print(recommended_update_command_for_method(method))
        sys.exit(1)
    git_dir = _m().PROJECT_ROOT / '.git'
    if not git_dir.exists():
        print('✗ Not a git repository — cannot check for updates.')
        sys.exit(1)
    git_cmd = ['git']
    if sys.platform == 'win32':
        git_cmd = ['git', '-c', 'windows.appendAtomically=false']
    is_shallow = subprocess.run(git_cmd + ['rev-parse', '--is-shallow-repository'], cwd=_m().PROJECT_ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace').stdout.strip() == 'true'
    depth_args = ['--depth', '1'] if is_shallow else []
    if branch == 'main':
        has_upstream_remote = subprocess.run(git_cmd + ['remote', 'get-url', 'upstream'], cwd=_m().PROJECT_ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace').returncode == 0
        fetch_result = None
        if has_upstream_remote:
            print('→ Fetching from upstream...')
            fetch_result = subprocess.run(git_cmd + ['fetch'] + depth_args + ['upstream', branch], cwd=_m().PROJECT_ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace')
        if fetch_result is not None and fetch_result.returncode == 0:
            upstream_exists = True
            compare_branch = f'upstream/{branch}'
        else:
            print('→ Fetching from origin...')
            fetch_result = subprocess.run(git_cmd + ['fetch'] + depth_args + ['origin', branch], cwd=_m().PROJECT_ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace')
            upstream_exists = False
            compare_branch = f'origin/{branch}'
    else:
        print('→ Fetching from origin...')
        fetch_result = subprocess.run(git_cmd + ['fetch'] + depth_args + ['origin', branch], cwd=_m().PROJECT_ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace')
        upstream_exists = False
        compare_branch = f'origin/{branch}'
    if fetch_result.returncode != 0:
        stderr = fetch_result.stderr.strip()
        if 'Could not resolve host' in stderr or 'unable to access' in stderr:
            print('✗ Network error — cannot reach the remote repository.')
        elif 'Authentication failed' in stderr or 'could not read Username' in stderr:
            print('✗ Authentication failed — check your git credentials or SSH key.')
        else:
            print('✗ Failed to fetch.')
            if stderr:
                print(f'  {stderr.splitlines()[0]}')
        sys.exit(1)
    verify_result = subprocess.run(git_cmd + ['rev-parse', '--verify', '--quiet', compare_branch], cwd=_m().PROJECT_ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if verify_result.returncode != 0:
        print(f"✗ Branch '{branch}' not found on {compare_branch.split('/', 1)[0]}.")
        sys.exit(1)
    if is_shallow:
        head_sha = subprocess.run(git_cmd + ['rev-parse', 'HEAD'], cwd=_m().PROJECT_ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace').stdout.strip()
        target_sha = subprocess.run(git_cmd + ['rev-parse', compare_branch], cwd=_m().PROJECT_ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace').stdout.strip()
        if head_sha and target_sha and (head_sha == target_sha):
            print('✓ Already up to date.')
        else:
            print(f'⚕ Update available (behind {compare_branch}).')
            from hermes_cli.config import recommended_update_command
            print(f"  Run '{recommended_update_command()}' to install.")
        return
    rev_result = subprocess.run(git_cmd + ['rev-list', f'HEAD..{compare_branch}', '--count'], cwd=_m().PROJECT_ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace', check=True)
    behind = int(rev_result.stdout.strip())
    if behind == 0:
        print('✓ Already up to date.')
    else:
        commits_word = 'commit' if behind == 1 else 'commits'
        print(f'⚕ Update available: {behind} {commits_word} behind {compare_branch}.')
        from hermes_cli.config import recommended_update_command
        print(f"  Run '{recommended_update_command()}' to install.")

def _ensure_fhs_path_guard() -> None:
    """Ensure /usr/local/bin is on PATH for RHEL-family root non-login shells.

    Mirrors the post-symlink probe added to ``scripts/install.sh`` so that
    existing FHS-layout root installs on RHEL/CentOS/Rocky/Alma 8+ get
    repaired on ``duck-agent update`` without requiring a reinstall.  The
    installer's assumption that ``/usr/local/bin`` is on PATH for every
    standard shell breaks on those distros in non-login interactive shells
    (su, sudo -s, tmux panes, some web terminals): /etc/bashrc doesn't
    add /usr/local/bin and /root/.bash_profile doesn't either.  Symptom:
    ``duck-agent`` prints ``command not found`` even though the symlink lives
    at /usr/local/bin/duck-agent.

    Silent no-op on: non-Linux, non-root, non-FHS installs, and any system
    where ``bash -i -c 'command -v duck-agent'`` already resolves.  Idempotent.
    """
    if _m().sys.platform != 'linux':
        return
    try:
        if os.geteuid() != 0:
            return
    except AttributeError:
        return
    fhs_link = Path('/usr/local/bin/duck-agent')
    if not fhs_link.is_symlink() and (not fhs_link.exists()):
        return
    home = os.environ.get('HOME') or '/root'
    try:
        probe = subprocess.run(['env', '-i', f'HOME={home}', f"TERM={os.environ.get('TERM', 'dumb')}", 'bash', '-i', '-c', 'command -v duck-agent'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return
    if probe.returncode == 0:
        return
    path_line = 'export PATH="/usr/local/bin:$PATH"'
    path_comment = '# Duck Agent — ensure /usr/local/bin is on PATH (RHEL non-login shells)'
    wrote_any = False
    for candidate in ('.bashrc', '.bash_profile'):
        cfg = Path(home) / candidate
        if not cfg.is_file():
            continue
        try:
            existing = cfg.read_text(errors='replace', encoding='utf-8')
        except OSError:
            continue
        already_guarded = any(('/usr/local/bin' in line and 'PATH' in line and (not line.lstrip().startswith('#')) for line in existing.splitlines()))
        if already_guarded:
            continue
        try:
            with cfg.open('a', encoding='utf-8') as f:
                f.write('\n' + path_comment + '\n' + path_line + '\n')
        except OSError as e:
            print(f'  ⚠ Could not update {cfg}: {e}')
            continue
        print(f'  ✓ Added /usr/local/bin to PATH in {cfg}')
        wrote_any = True
    if wrote_any:
        print("    (reload your shell or run 'source ~/.bashrc' to pick it up)")

def _ensure_acp_launcher() -> None:
    """Self-heal: install a ``duck-agent-acp`` launcher next to the ``duck-agent`` one.

    Mirrors the launcher block in ``scripts/install.sh`` so existing installs
    gain the ACP command on ``duck-agent update`` without a reinstall.  ACP hosts
    (Zed, JetBrains, Buzz Desktop) spawn the agent by resolving the
    ``duck-agent-acp`` command name against the login-shell PATH; the console
    script of that name lives inside the install's venv, which is not on that
    PATH, so those hosts report Duck Agent as not installed even when it is.

    The shim simply delegates to the sibling ``duck-agent`` launcher with the
    ``acp`` subcommand, which makes it correct for every install layout
    (venv wrapper, FHS symlink, pipx/pip console script) without having to
    reconstruct interpreter/entrypoint paths.

    No-op on Windows (install.ps1 puts ``venv\\Scripts`` on the user PATH, so
    ``duck-agent-acp.exe`` already resolves) and wherever a ``duck-agent-acp`` is
    already present next to the ``duck-agent`` command.  Unwritable directories
    (e.g. ``/usr/local/bin`` as non-root) are skipped silently.  Idempotent.
    """
    if _m().sys.platform == 'win32':
        return
    for bin_dir in (Path.home() / '.local' / 'bin', Path('/usr/local/bin')):
        hermes_cmd = bin_dir / 'duck-agent'
        acp_cmd = bin_dir / 'duck-agent-acp'
        try:
            if not (hermes_cmd.is_file() or hermes_cmd.is_symlink()):
                continue
            if acp_cmd.exists() or acp_cmd.is_symlink():
                continue
            shim = f'#!/usr/bin/env bash\n# Duck Agent — ACP launcher (written by `duck-agent update`).\n# ACP hosts (Zed, JetBrains, Buzz) resolve the agent by this\n# command name on the login-shell PATH.\nexec "{hermes_cmd}" acp "$@"\n'
            acp_cmd.write_text(shim, encoding='utf-8')
            acp_cmd.chmod(acp_cmd.stat().st_mode | 493)
        except OSError:
            continue
        print(f'  ✓ Installed duck-agent-acp launcher → {acp_cmd}')
_PRE_UPDATE_SNAPSHOT_KEEP = 1
_PRE_UPDATE_SNAPSHOT_MAX_FILE_SIZE = 1 << 30

def _resolve_pre_update_backup_mode(args) -> str:
    """Resolve the pre-update backup mode: ``"off"``, ``"quick"``, or ``"full"``.

    CLI flags win over config; ``--no-backup`` beats ``--backup`` when both
    are set. Config accepts the mode strings plus legacy booleans:
    ``true`` → ``full`` (the old zip behavior), ``false`` → ``off``
    (an explicit opt-out now disables the quick snapshot too — previously
    it ran unconditionally, ignoring the user's setting). A missing key
    defaults to ``quick``.
    """
    if getattr(args, 'no_backup', False):
        return 'off'
    if getattr(args, 'backup', False):
        return 'full'
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
    except Exception as exc:
        logging.getLogger(__name__).debug('Could not load config for pre-update backup: %s', exc)
        cfg = {}
    updates_cfg = cfg.get('updates', {}) if isinstance(cfg, dict) else {}
    raw = updates_cfg.get('pre_update_backup', 'quick')
    if raw is True:
        return 'full'
    if raw is False:
        return 'off'
    mode = str(raw).strip().lower()
    if mode in ('off', 'false', 'none', 'disabled'):
        return 'off'
    if mode in ('full', 'zip', 'true'):
        return 'full'
    if mode == 'quick':
        return 'quick'
    logging.getLogger(__name__).warning("Unknown updates.pre_update_backup value %r — using 'quick'", raw)
    return 'quick'

def _run_pre_update_backup(args) -> Optional[str]:
    """Run the pre-update safety backup and return the quick-snapshot id.

    Single consolidated mechanism gated on ``updates.pre_update_backup``:

    - ``off``   — nothing runs. Explicit user opt-out is honored fully.
    - ``quick`` (default) — a state snapshot of critical small files
      (pairing JSONs, cron jobs, config, auth; see ``_QUICK_STATE_FILES``)
      under ``state-snapshots/``. Files over 1 GiB are skipped with a
      warning so a bloated state.db can never stall the update
      (issues #15733, #34600 are the reason this safety net exists).
    - ``full``  — the quick snapshot PLUS a full zip of DUCK_AGENT_HOME under
      ``backups/`` (restorable via ``duck-agent import``; the #48200 wrong-path
      wipe is the reason this level exists).

    ``--backup`` forces ``full`` for one run; ``--no-backup`` forces ``off``.
    Never raises — a backup failure should not block the update itself.

    Returns the quick-snapshot id (used by the post-update cron-jobs
    restore safety net), or ``None`` when mode is ``off`` or the snapshot
    failed.
    """
    mode = _resolve_pre_update_backup_mode(args)
    if mode == 'off':
        if getattr(args, 'no_backup', False):
            print('◆ Pre-update backup: skipped (--no-backup)')
            print()
        return None
    snapshot_id = None
    try:
        from hermes_cli.backup import _quick_snapshot_root, create_quick_snapshot, verify_sqlite_integrity
        from hermes_cli.config import get_hermes_home as _get_home
        snapshot_id = create_quick_snapshot(label='pre-update', keep=_PRE_UPDATE_SNAPSHOT_KEEP, max_file_size=_PRE_UPDATE_SNAPSHOT_MAX_FILE_SIZE)
        if snapshot_id:
            _src_path = _get_home() / 'state.db'
            if _src_path.exists():
                _integrity = verify_sqlite_integrity(_src_path, check_header=True, run_pragma=True, max_bytes=_PRE_UPDATE_SNAPSHOT_MAX_FILE_SIZE)
                if not _integrity.get('valid'):
                    _msg = _integrity.get('message', 'unknown error')
                    print(f'  ⚠ state.db integrity check FAILED after snapshot: {_msg}')
                    _snap_root = _quick_snapshot_root(_get_home())
                    _snap_state = _snap_root / snapshot_id / 'state.db'
                    if _snap_state.exists():
                        _snap_ok = verify_sqlite_integrity(_snap_state, check_header=True, run_pragma=True)
                        if _snap_ok.get('valid'):
                            print('  ✓ Snapshot copy is valid — continuing update.')
                            print('    If state.db is lost after update it will be auto-restored.')
                        else:
                            print('  ✗ Snapshot copy ALSO failed integrity — the source was already corrupted before the backup.')
                    else:
                        print('  ⚠ Snapshot does not contain state.db (was skipped or too large).')
                    print()
        if snapshot_id:
            print(f'◆ Pre-update snapshot: {snapshot_id}')
    except Exception as exc:
        logging.getLogger(__name__).debug('Pre-update snapshot failed: %s', exc)
    if mode != 'full':
        if snapshot_id:
            print()
        return snapshot_id
    try:
        from hermes_cli.backup import create_pre_update_backup
    except Exception as exc:
        print(f'⚠ Pre-update backup: could not load backup module ({exc}); continuing update.')
        print()
        return snapshot_id
    try:
        from hermes_cli.config import load_config
        _keep = (load_config() or {}).get('updates', {}).get('backup_keep', 5)
    except Exception:
        _keep = 5
    print('◆ Creating pre-update backup...')
    t0 = _time.monotonic()
    try:
        out_path = create_pre_update_backup(keep=int(_keep))
    except Exception as exc:
        print(f'  ⚠ Backup failed: {exc}')
        print('  Continuing with update.')
        print()
        return snapshot_id
    elapsed = _time.monotonic() - t0
    if out_path is None:
        print('  ⚠ Backup skipped (no files found or write failed); continuing update.')
        print()
        return snapshot_id
    try:
        size_bytes = out_path.stat().st_size
    except OSError:
        size_bytes = 0
    size_str = f'{size_bytes} B'
    for unit in ('KB', 'MB', 'GB'):
        if size_bytes < 1024:
            break
        size_bytes /= 1024
        size_str = f'{size_bytes:.1f} {unit}'
    try:
        from hermes_constants import get_hermes_home, display_hermes_home
        home = get_hermes_home()
        try:
            display_path = f'{display_hermes_home()}/{out_path.relative_to(home)}'
        except ValueError:
            display_path = str(out_path)
    except Exception:
        display_path = str(out_path)
    print(f'  Saved:    {display_path} ({size_str}, {elapsed:.1f}s)')
    print(f'  Restore:  duck-agent import {out_path}')
    print('  Disable:  set updates.pre_update_backup: quick (or off) in config.yaml')
    print()
    return snapshot_id

def _write_update_planned_stop_marker(profile_path: Path, pid: int) -> bool:
    """Write a planned-stop marker into a specific profile home."""
    try:
        from datetime import timezone
        from gateway.status import _get_process_start_time
        from utils import atomic_json_write
        record = {'target_pid': pid, 'target_start_time': _get_process_start_time(pid), 'stopper_pid': os.getpid(), 'written_at': datetime.now(timezone.utc).isoformat()}
        atomic_json_write(Path(profile_path) / '.gateway-planned-stop.json', record, indent=None, separators=(',', ':'))
        return True
    except (OSError, PermissionError):
        return False

def _wait_for_windows_update_gateway_exit(pids: list[int], *, timeout: float) -> set[int]:
    """Wait for the given gateway PIDs to exit, returning survivors."""
    if not pids:
        return set()
    from gateway.status import _pid_exists
    remaining = set(pids)
    deadline = _time.monotonic() + max(timeout, 0.0)
    while remaining and _time.monotonic() < deadline:
        for pid in list(remaining):
            try:
                if not _pid_exists(pid):
                    remaining.discard(pid)
            except Exception:
                remaining.discard(pid)
        if remaining:
            _time.sleep(0.25)
    survivors: set[int] = set()
    for pid in remaining:
        try:
            if _pid_exists(pid):
                survivors.add(pid)
        except Exception:
            pass
    return survivors

def _venv_core_imports_healthy() -> tuple[bool, str]:
    """Probe the project venv for the core imports the backend needs to boot.

    Runs a tiny import check inside the venv interpreter (NOT this process —
    ``duck-agent update`` may be driven by a different Python). Catches the
    half-updated-venv state: git checkout current but a dependency sync that
    failed or was killed partway (e.g. Windows access-denied on a loaded
    .pyd), leaving imports like ``fastapi``'s new transitive deps missing.
    Without this probe, ``duck-agent update`` on a current checkout prints
    "Already up to date!" and returns without ever re-syncing dependencies —
    the user's install stays broken no matter how many times they update
    (ryanc's incident, July 2026).

    Returns ``(healthy, detail)``. Never raises; unknown states report
    healthy so a probe failure can't force needless reinstalls.
    """
    venv_dir = _m().PROJECT_ROOT / 'venv'
    venv_python = venv_python_path(venv_dir, windows=_m()._is_windows())
    if not venv_python.exists():
        managed_markers = (_m().PROJECT_ROOT / '.duck-agent-bootstrap-complete', _m()._update_marker_path())
        if any((m.exists() for m in managed_markers)):
            return (False, f'venv python missing ({venv_python})')
        return (True, '')
    check = "import importlib\nmods = ['fastapi', 'uvicorn', 'pydantic', 'openai', 'yaml']\nmissing = []\nfor m in mods:\n    try: importlib.import_module(m)\n    except Exception as e: missing.append(f'{m}: {e}')\nprint('\\n'.join(missing))\n"
    try:
        result = subprocess.run([str(venv_python), '-c', check], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60, cwd=_m().PROJECT_ROOT)
    except Exception as exc:
        logger.debug('venv health probe failed to run: %s', exc)
        return (True, '')
    missing = [line.strip() for line in (result.stdout or '').splitlines() if line.strip()]
    if result.returncode != 0 and (not missing):
        detail = (result.stderr or '').strip().splitlines()
        return (False, detail[0] if detail else 'venv python failed to run')
    if missing:
        return (False, '; '.join(missing[:4]))
    return (True, '')

def _detect_venv_python_processes(*, exclude_pids: set[int] | None=None) -> list[tuple[int, str, str]]:
    """Find live processes running from the project venv's interpreter.

    The duck-agent.exe shim guard misses the biggest lock-holder class on
    Windows: the Desktop app's backend (``python.exe -m hermes_cli.main
    serve``) and anything else running straight off ``venv\\Scripts\\python
    (w).exe``. Those processes keep native ``.pyd`` extensions mapped, so a
    dependency sync mid-update dies with access-denied and strands the venv
    half-updated (ryanc's brotlicffi/_sodium.pyd incidents, July 2026).

    Killing them from here is pointless — the Desktop app supervises its
    backend and respawns it within seconds — so the caller should refuse and
    tell the user to close the app instead. Returns ``(pid, name, cmdline)``
    tuples; empty off-Windows / without psutil / when nothing matches. The
    calling process and its ancestors are always excluded (a CLI ``duck-agent
    update`` itself runs from the venv python). Never raises.
    """
    if not _m()._is_windows():
        return []
    try:
        import psutil
    except Exception:
        return []
    venv_dir = _m().PROJECT_ROOT / 'venv'
    try:
        venv_prefix = str(venv_dir.resolve()).lower().rstrip(os.sep) + os.sep
    except OSError:
        venv_prefix = str(venv_dir).lower().rstrip(os.sep) + os.sep
    try:
        root_prefix = str(_m().PROJECT_ROOT.resolve()).lower().rstrip(os.sep) + os.sep
    except OSError:
        root_prefix = str(_m().PROJECT_ROOT).lower().rstrip(os.sep) + os.sep
    skip: set[int] = set(exclude_pids or set())
    skip.add(os.getpid())
    try:
        for anc in psutil.Process().parents():
            skip.add(int(anc.pid))
    except Exception:
        pass
    matches: list[tuple[int, str, str]] = []
    try:
        proc_iter = psutil.process_iter(['pid', 'exe', 'name', 'cmdline', 'cwd'])
    except Exception:
        return []
    for proc in proc_iter:
        try:
            info = proc.info
        except Exception:
            continue
        pid = info.get('pid')
        exe = info.get('exe')
        if not exe or pid is None or int(pid) in skip:
            continue
        try:
            exe_norm = str(Path(exe).resolve()).lower()
        except (OSError, ValueError):
            exe_norm = str(exe).lower()
        cmdline_raw = ' '.join(info.get('cmdline') or [])
        cmdline_low = cmdline_raw.lower()
        cwd_low = str(info.get('cwd') or '').lower().rstrip(os.sep) + os.sep
        is_holder = exe_norm.startswith(venv_prefix)
        if not is_holder and venv_prefix in cmdline_low:
            is_holder = True
        if not is_holder and 'hermes_cli.main' in cmdline_low:
            if root_prefix in cmdline_low or cwd_low.startswith(root_prefix):
                is_holder = True
        if not is_holder:
            continue
        name = info.get('name') or Path(exe).name
        matches.append((int(pid), str(name), cmdline_raw[:120]))
    return matches

def _format_venv_python_holders_message(matches: list[tuple[int, str, str]]) -> str:
    """Explain which venv processes block the update and how to clear them."""
    lines = ["✗ Other Duck Agent processes are running from this install's venv:"]
    for pid, name, cmdline in matches[:6]:
        hint = ''
        low = cmdline.lower()
        if 'serve' in low or 'dashboard' in low:
            hint = '  ← Duck Agent Desktop backend (close the desktop app)'
        elif 'gateway' in low:
            hint = '  ← gateway'
        lines.append(f'  PID {pid}  {name}  {cmdline}{hint}')
    if len(matches) > 6:
        lines.append(f'  ... and {len(matches) - 6} more')
    lines.append('')
    lines.append('  On Windows these keep native extension files (.pyd) locked, so the')
    lines.append('  dependency update would fail partway and leave a broken install.')
    lines.append('  Close the Duck Agent desktop app / other Duck Agent terminals, then re-run:')
    lines.append('    duck-agent update')
    lines.append('  (or use `duck-agent update --force-venv` to proceed anyway at your own risk)')
    return '\n'.join(lines)

def _venv_launcher_ancestors(pids: list[int]) -> list[int]:
    """Return venv-interpreter ancestors of *pids* that hold the install open.

    On Windows a gateway started through the venv shim is a **two-process
    chain**: ``venv\\Scripts\\python.exe`` (the launcher, which keeps native
    ``.pyd`` files from the venv mapped) spawns the actual interpreter from
    uv's managed CPython directory (``AppData\\Roaming\\uv\\python\\...``).
    The gateway writes its PID file from the *child*, so
    ``find_gateway_pids()`` — and therefore this module's pause set — only
    ever sees the uv-side worker.

    ``_detect_venv_python_processes()`` matches on the venv path prefix, so
    the guard downstream of the pause sees the *launcher* instead. The two
    sets are disjoint, which meant a paused gateway still tripped the
    venv-holder guard and aborted the update every time (the Desktop
    "venv-blocked: N process(es) hold the install" dead-end, where the
    reported holder is a gateway the updater believes it already stopped).

    Walking one hop up from each mapped gateway PID and keeping ancestors
    that live under the project venv closes the gap. Only the venv-side
    parent is returned — unrelated ancestors (the Scheduled Task's
    ``cmd.exe``, an operator's shell) are ignored so we never widen the
    blast radius beyond the gateway's own launcher. Never raises.
    """
    if not _m()._is_windows() or not pids:
        return []
    try:
        import psutil
    except Exception:
        return []
    venv_dir = _m().PROJECT_ROOT / 'venv'
    try:
        venv_prefix = str(venv_dir.resolve()).lower().rstrip(os.sep) + os.sep
    except OSError:
        venv_prefix = str(venv_dir).lower().rstrip(os.sep) + os.sep
    skip: set[int] = {os.getpid()}
    try:
        for anc in psutil.Process().parents():
            skip.add(int(anc.pid))
    except Exception:
        pass
    found: list[int] = []
    for pid in pids:
        try:
            parent = psutil.Process(int(pid)).parent()
        except Exception:
            continue
        if parent is None:
            continue
        ppid = int(parent.pid)
        if ppid in skip or ppid in found or ppid in set(pids):
            continue
        try:
            exe = (parent.exe() or '').lower()
        except Exception:
            continue
        if exe.startswith(venv_prefix):
            found.append(ppid)
    return found

def _leftover_pausable_gateway_pids(matches: list[tuple[int, str, str]]) -> list[int] | None:
    """PIDs from *matches* when every remaining venv holder is a pausable gateway.

    ``_pause_windows_gateways_for_update()`` stops every gateway its discovery
    finds, but the venv-holder guard downstream sees the process table as it
    is *now*: a gateway respawned by its supervisor (Scheduled Task, login
    watchdog) inside the pause→guard window, or one started through a spawn
    path the discovery does not map, still holds venv ``.pyd`` files and
    would dead-end the update — an abort pointed at exactly the kind of
    process the pause machinery exists to stop.

    Holders are classified with the same matcher the Desktop preflight uses
    to exempt them (``_is_pausable_gateway``), so the preflight's exemption
    and this guard's tolerance cannot drift apart — matcher drift between
    two views of the same process table is what produced the launcher/worker
    dead-end fixed above. The scan captures only a 120-char cmdline prefix,
    so the live argv is re-read where psutil allows; an unreadable argv
    falls back to the captured prefix.

    Returns ``None`` when any holder is not a pausable gateway — an operator
    REPL, a stray script, or the Desktop backend has no pause machinery
    downstream, and the guard must keep refusing exactly as before.
    """
    from hermes_cli._scan_venv_blockers import _is_pausable_gateway
    try:
        import psutil
    except Exception:
        psutil = None
    pids: list[int] = []
    for pid, _name, cmdline in matches:
        argv = cmdline
        if psutil is not None:
            try:
                argv = ' '.join(psutil.Process(int(pid)).cmdline()) or cmdline
            except Exception:
                pass
        if not _is_pausable_gateway(argv):
            return None
        pids.append(int(pid))
    return pids

def _pause_windows_gateways_for_update() -> dict | None:
    """Stop running Windows gateways before mutating the checkout or venv.

    Windows scheduled/startup gateways run through pythonw.exe, so the generic
    duck-agent.exe concurrent-instance guard does not see them. They still import
    from the checkout and can keep files locked while ``git`` or ``uv`` updates
    the install. Stop only PIDs that the gateway discovery code identifies.
    """
    if not _m()._is_windows():
        return None
    try:
        from gateway.status import terminate_pid
        from hermes_cli.gateway import _capture_gateway_argv, _get_restart_drain_timeout, find_gateway_pids, find_profile_gateway_processes
    except Exception as exc:
        logger.debug('Could not prepare Windows gateway pause for update: %s', exc)
        return None
    try:
        running_pids = list(dict.fromkeys(find_gateway_pids(all_profiles=True)))
    except Exception as exc:
        logger.debug('Could not discover Windows gateway PIDs before update: %s', exc)
        return None
    if not running_pids:
        try:
            from hermes_cli import gateway_windows
            if gateway_windows.is_installed():
                return {'resume_needed': True, 'profiles': {}, 'unmapped_pids': [], 'unmapped': [], 'cold_start_if_installed': True}
        except Exception as exc:
            logger.debug('Could not check Windows gateway autostart state before update: %s', exc)
        return None
    profile_processes = {}
    try:
        profile_processes = {proc.pid: proc for proc in find_profile_gateway_processes()}
    except Exception as exc:
        logger.debug('Could not map Windows gateway PIDs to profiles: %s', exc)
    profiles: dict[str, int] = {}
    mapped_pids = []
    for pid in running_pids:
        proc = profile_processes.get(pid)
        if proc is None:
            continue
        profiles[str(proc.profile)] = int(pid)
        mapped_pids.append(int(pid))
        _write_update_planned_stop_marker(Path(proc.path), int(pid))
    launcher_pids = _m()._venv_launcher_ancestors(mapped_pids)
    print('→ Stopping Windows gateway process(es) before updating Duck Agent...')
    try:
        drain_timeout = max(float(_get_restart_drain_timeout()), 1.0)
    except Exception:
        drain_timeout = 10.0
    survivors = _m()._wait_for_windows_update_gateway_exit(mapped_pids, timeout=drain_timeout)
    unmapped_pids = [pid for pid in running_pids if pid not in profile_processes]
    unmapped: list[dict] = []
    for pid in unmapped_pids:
        argv = None
        try:
            argv = _capture_gateway_argv(int(pid))
        except Exception as exc:
            logger.debug('Could not capture argv for unmapped gateway %s: %s', pid, exc)
        unmapped.append({'pid': int(pid), 'argv': argv})
    force_killed = []
    for pid in sorted(set(survivors).union(unmapped_pids).union(launcher_pids)):
        try:
            terminate_pid(int(pid), force=True)
            force_killed.append(int(pid))
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if profiles:
        print(f"  ✓ Paused gateway profile(s): {', '.join(sorted(profiles))}")
    if force_killed:
        print(f'  → Force-stopped {len(force_killed)} gateway process(es)')
    if unmapped_pids:
        respawnable = sum((1 for u in unmapped if u.get('argv')))
        print(f'  → Stopped {len(unmapped_pids)} gateway process(es) without profile mapping')
        if respawnable < len(unmapped_pids):
            print('    Restart manually after update: duck-agent gateway run')
    return {'resume_needed': True, 'profiles': profiles, 'unmapped_pids': unmapped_pids, 'unmapped': unmapped}

def _cold_start_windows_gateway_after_update() -> None:
    """Start a fresh detached gateway after update when one is installed but down.

    Invoked from ``_resume_windows_gateways_after_update`` for the
    ``cold_start_if_installed`` case: no gateway was running when the update
    began, but an autostart entry (Scheduled Task / Startup-folder login item)
    is installed, signalling the user wants a gateway. Unlike the relaunch
    paths — which watch an old PID and respawn once it exits — this is a direct
    fresh spawn via the same hidden-console + breakaway path that
    ``duck-agent gateway start`` uses (``gateway_windows._spawn_detached``).

    Best-effort and idempotent: re-checks that nothing is running first so a
    concurrent start (e.g. the autostart entry firing) can't produce a
    duplicate gateway.
    """
    if not _m()._is_windows():
        return
    try:
        from hermes_cli import gateway_windows
        from hermes_cli.gateway import find_gateway_pids
    except Exception as exc:
        logger.debug('Could not load Windows gateway cold-start helpers: %s', exc)
        return
    try:
        if list(find_gateway_pids(all_profiles=True)):
            return
    except Exception as exc:
        logger.debug('Could not re-check gateway liveness before cold-start: %s', exc)
        return
    try:
        pid = gateway_windows._spawn_detached()
    except Exception as exc:
        logger.debug('Could not cold-start Windows gateway after update: %s', exc)
        return
    if pid:
        print()
        print(f'  ✓ Starting Windows gateway after update (PID {pid})')

def _for_each_systemd_gateway_unit(list_units_stdout: str, *, process_unit, on_unit_timeout) -> None:
    """Process each ``duck-agent-gateway*.service`` from ``systemctl list-units``.

    ``subprocess.TimeoutExpired`` raised by ``process_unit`` is isolated to
    that unit via ``on_unit_timeout`` so one wedged systemctl call cannot
    abort the rest of the fleet (#68523).
    """
    for line in (list_units_stdout or '').strip().splitlines():
        parts = line.split()
        if not parts:
            continue
        unit = parts[0]
        if not unit.endswith('.service'):
            continue
        if not unit.startswith('duck-agent-gateway'):
            continue
        svc_name = unit.removesuffix('.service')
        try:
            process_unit(svc_name)
        except subprocess.TimeoutExpired as exc:
            on_unit_timeout(svc_name, exc)

def _warn_incomplete_gateway_fleet_restart(failed_units: list) -> None:
    """Print an explicit incomplete-update warning for unrestarted units."""
    if not failed_units:
        return
    seen = set()
    ordered = []
    for name in failed_units:
        if name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    print()
    print('⚠ Update incomplete — some gateway units were not restarted:')
    for name in ordered:
        print(f'    - {name}')
    print('  Skipped units may still be running pre-update code (mixed')
    print('  sys.modules). Restart them manually, then verify:')
    print('    duck-agent gateway status')
    print('    systemctl --user restart <unit>   # user-scope')
    print('    sudo systemctl restart <unit>     # system-scope')

def _refresh_windows_gateway_launchers() -> None:
    """Regenerate installed Windows gateway launcher scripts after update.

    The Scheduled Task / Startup-folder launchers (``gateway.cmd`` +
    ``gateway.vbs``) are persistence artifacts written once at install time —
    ``duck-agent update`` never touched them, so installs created before the
    hidden-console rework (aa2ae36c3f) kept launching the gateway through
    ``pythonw.exe`` forever: every descendant spawn flashed a conhost
    (#54220/#56747) and, since #70344, the console-less gateway died at
    startup with ``RuntimeError: sys.stderr is None`` (#71671).

    The task's /TR points at a stable script path, so rewriting the files in
    place retargets the task without any schtasks call (no UAC needed).
    ``_write_task_script`` is idempotent and renders from current code, so
    this is a no-op for modern installs. Best-effort: a failed refresh must
    never fail the update.
    """
    if not _m()._is_windows():
        return
    try:
        from hermes_cli import gateway_windows
        if not gateway_windows.is_installed():
            return
        gateway_windows._write_task_script()
        print('  ✓ Refreshed Windows gateway launcher scripts')
    except Exception as exc:
        logger.debug('Could not refresh Windows gateway launchers after update: %s', exc)

def _resume_windows_gateways_after_update(token: dict | None) -> None:
    """Restart Windows profile gateways previously paused for update."""
    if not token or not token.get('resume_needed'):
        return
    token['resume_needed'] = False
    if not _m()._is_windows():
        return
    _m()._refresh_windows_gateway_launchers()
    profiles = token.get('profiles') or {}
    unmapped = token.get('unmapped') or []
    cold_start = bool(token.get('cold_start_if_installed'))
    if not profiles and (not any((u.get('argv') for u in unmapped))):
        if cold_start:
            _m()._cold_start_windows_gateway_after_update()
        return
    try:
        from hermes_cli.gateway import launch_detached_gateway_restart_by_cmdline, launch_detached_profile_gateway_restart
    except Exception as exc:
        logger.debug('Could not load Windows gateway restart helper: %s', exc)
        return
    relaunched = []
    for profile, old_pid in sorted(profiles.items()):
        try:
            if launch_detached_profile_gateway_restart(str(profile), int(old_pid)):
                relaunched.append(str(profile))
        except Exception as exc:
            logger.debug('Could not restart Windows gateway profile %s after update: %s', profile, exc)
    unmapped_relaunched = 0
    for entry in unmapped:
        argv = entry.get('argv')
        old_pid = entry.get('pid')
        if not argv or not old_pid:
            continue
        try:
            if launch_detached_gateway_restart_by_cmdline(int(old_pid), list(argv)):
                unmapped_relaunched += 1
        except Exception as exc:
            logger.debug('Could not restart unmapped Windows gateway (pid %s) after update: %s', old_pid, exc)
    if relaunched:
        print()
        print(f"  ✓ Restarting Windows gateway profile(s): {', '.join(relaunched)}")
    if unmapped_relaunched:
        if not relaunched:
            print()
        print(f'  ✓ Restarting {unmapped_relaunched} unmapped Windows gateway process(es)')

def _discard_lockfile_churn(git_cmd, repo_root):
    """Restore tracked ``package-lock.json`` files that npm dirtied locally.

    npm rewrites lockfiles non-deterministically at install/build time. On a
    managed install those diffs are never intentional, so we discard them so
    ``duck-agent update`` sees a clean tree instead of autostashing every run.
    Best-effort; only ever touches files named ``package-lock.json``.
    """
    try:
        diff = subprocess.run(git_cmd + ['diff', '--name-only'], cwd=repo_root, capture_output=True, text=True, encoding='utf-8', errors='replace')
        if diff.returncode != 0:
            return
        dirty_package_dirs = {Path(line.strip()).parent for line in diff.stdout.splitlines() if line.strip().endswith('package.json')}
        dirty = [line.strip() for line in diff.stdout.splitlines() if line.strip().endswith('package-lock.json') and Path(line.strip()).parent not in dirty_package_dirs]
        if not dirty:
            return
        subprocess.run(git_cmd + ['checkout', '--', *dirty], cwd=repo_root, capture_output=True, text=True, encoding='utf-8', errors='replace', check=False)
        print(f'→ Discarded npm lockfile churn ({len(dirty)} file(s))')
    except Exception:
        pass

def _normalize_managed_eol(git_cmd, repo_root):
    """Take a managed checkout off ``core.autocrlf=true`` without leaving it dirty.

    Git for Windows ships ``core.autocrlf=true`` in its system config, which
    renormalizes this repo's LF text files to CRLF in the working tree. That
    breaks ``git checkout`` on update with "Your local changes would be
    overwritten", so ``install.ps1`` pins ``core.autocrlf=false`` on the managed
    clone (#67730). Checkouts created before that landed never got the pin and
    cannot receive it — the bootstrap installer reuses its build-pinned
    ``install.ps1`` forever — so ``duck-agent update``, which ships with the checkout
    itself, is the only path left that can fix them.

    The pin and the cleanup are one operation. Under ``autocrlf=true`` git
    compares normalized content, so a CRLF working tree reads clean; pinning
    alone would expose every text file as modified and hand the update an
    autostash of the whole tree. So the pin is written only after the tree is
    verified clean under it, and a checkout we cannot fully normalize is left
    exactly as it was. Best-effort: never blocks an update.
    """
    probe = git_cmd + ['-c', 'core.autocrlf=false']

    def _dirty(*extra):
        out = subprocess.run(probe + ['diff', '-z', '--name-only', *extra], cwd=repo_root, capture_output=True, text=True, encoding='utf-8', errors='replace')
        if out.returncode != 0:
            return None
        return {p for p in out.stdout.split('\x00') if p}

    def _real_dirty():
        out = subprocess.run(probe + ['-c', 'core.quotepath=false', 'diff', '--numstat', '--ignore-cr-at-eol'], cwd=repo_root, capture_output=True, text=True, encoding='utf-8', errors='replace')
        if out.returncode != 0:
            return None
        paths = set()
        for line in out.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split('\t', 2)
            if len(parts) == 3 and parts[2]:
                paths.add(parts[2])
        return paths

    def _eol_only():
        all_dirty, real_dirty = (_dirty(), _real_dirty())
        if all_dirty is None or real_dirty is None:
            return None
        return all_dirty - real_dirty
    try:
        effective = subprocess.run(git_cmd + ['config', '--get', 'core.autocrlf'], cwd=repo_root, capture_output=True, text=True, encoding='utf-8', errors='replace')
        if effective.stdout.strip().lower() != 'true':
            return
        eol_only = _eol_only()
        if eol_only is None:
            return
        if eol_only:
            subprocess.run(probe + ['checkout', '--pathspec-from-file=-', '--pathspec-file-nul', '--'], cwd=repo_root, input='\x00'.join(sorted(eol_only)), capture_output=True, text=True, encoding='utf-8', errors='replace', check=False)
            if _eol_only():
                return
            print(f'→ Normalized line-ending churn ({len(eol_only)} file(s))')
        subprocess.run(git_cmd + ['config', 'core.autocrlf', 'false'], cwd=repo_root, capture_output=True, check=False)
    except Exception:
        pass

def _cmd_update_impl(args, gateway_mode: bool):
    """Body of ``cmd_update`` — kept separate so the wrapper can always
    restore stdio even on ``sys.exit``."""
    gw_input_fn = (lambda prompt, default='': _gateway_prompt(prompt, default)) if gateway_mode else None
    assume_yes = bool(getattr(args, 'yes', False))
    _non_interactive_update = gateway_mode or assume_yes or (not (sys.stdin.isatty() and sys.stdout.isatty()))
    discard_local_changes = False
    if _non_interactive_update:
        try:
            from hermes_cli.config import load_config
            _update_cfg = (load_config() or {}).get('updates', {})
            if isinstance(_update_cfg, dict):
                _mode = str(_update_cfg.get('non_interactive_local_changes', 'stash')).lower()
                discard_local_changes = _mode == 'discard'
        except Exception as exc:
            logger.debug('Could not read updates.non_interactive_local_changes: %s', exc)
            discard_local_changes = False
    print('⚕ Updating Duck Agent...')
    print()
    if _m()._is_windows() and (not getattr(args, 'force', False)):
        scripts_dir = _m()._venv_scripts_dir()
        if scripts_dir is not None:
            concurrent = _m()._detect_concurrent_hermes_instances(scripts_dir)
            if concurrent:
                print(_format_concurrent_instances_message(concurrent, scripts_dir))
                sys.exit(2)
    pre_update_snapshot_id = _m()._run_pre_update_backup(args)
    _windows_gateway_resume = _m()._pause_windows_gateways_for_update()
    if _windows_gateway_resume:
        import atexit as _atexit
        _atexit.register(_m()._resume_windows_gateways_after_update, _windows_gateway_resume)
    if _m()._is_windows() and (not getattr(args, 'force_venv', False)):
        _venv_holders = _m()._detect_venv_python_processes()
        if _venv_holders:
            _gateway_holders = _m()._leftover_pausable_gateway_pids(_venv_holders)
            if _gateway_holders is not None:
                from gateway.status import terminate_pid
                print(f'  ⚠ {len(_gateway_holders)} gateway process(es) still hold the venv after the pause; stopping them')
                for _pid in _gateway_holders:
                    try:
                        terminate_pid(int(_pid), force=True)
                    except Exception as exc:
                        logger.debug('Could not stop leftover gateway %s: %s', _pid, exc)
                _time.sleep(1.0)
                _venv_holders = _m()._detect_venv_python_processes()
        if _venv_holders:
            print(_format_venv_python_holders_message(_venv_holders))
            _m()._resume_windows_gateways_after_update(_windows_gateway_resume)
            sys.exit(2)
    use_zip_update = False
    git_dir = _m().PROJECT_ROOT / '.git'
    if not git_dir.exists():
        if sys.platform == 'win32':
            use_zip_update = True
        else:
            print('✗ Not a git repository. Please reinstall:')
            print('  curl -fsSL https://duck-agent.nousresearch.com/install.sh | bash')
            sys.exit(1)
    if sys.platform == 'win32' and git_dir.exists():
        subprocess.run(['git', '-c', 'windows.appendAtomically=false', 'config', 'windows.appendAtomically', 'false'], cwd=_m().PROJECT_ROOT, check=False, capture_output=True)
    git_cmd = ['git']
    if sys.platform == 'win32':
        git_cmd = ['git', '-c', 'windows.appendAtomically=false']
    _discard_lockfile_churn(git_cmd, _m().PROJECT_ROOT)
    _normalize_managed_eol(git_cmd, _m().PROJECT_ROOT)
    origin_url = _m()._get_origin_url(git_cmd, _m().PROJECT_ROOT)
    is_fork = _is_fork(origin_url)
    if is_fork:
        print('⚠ Updating from fork:')
        print(f'  {origin_url}')
        print()
    if use_zip_update:
        try:
            _update_via_zip(args)
        finally:
            _m()._resume_windows_gateways_after_update(_windows_gateway_resume)
        return
    try:
        branch = _m()._resolve_update_branch(args)
        print('→ Fetching updates...')
        fetch_result = subprocess.run(git_cmd + ['fetch', 'origin', branch], cwd=_m().PROJECT_ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace')
        if fetch_result.returncode != 0:
            stderr = fetch_result.stderr.strip()
            if 'Could not resolve host' in stderr or 'unable to access' in stderr:
                print('✗ Network error — cannot reach the remote repository.')
                print(f'  {stderr.splitlines()[0]}' if stderr else '')
            elif 'Authentication failed' in stderr or 'could not read Username' in stderr:
                print('✗ Authentication failed — check your git credentials or SSH key.')
            else:
                print('✗ Failed to fetch updates from origin.')
                if stderr:
                    print(f'  {stderr.splitlines()[0]}')
            sys.exit(1)
        result = subprocess.run(git_cmd + ['rev-parse', '--abbrev-ref', 'HEAD'], cwd=_m().PROJECT_ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace', check=True)
        current_branch = result.stdout.strip()
        if current_branch != branch:
            label = 'detached HEAD' if current_branch == 'HEAD' else f"branch '{current_branch}'"
            print(f'  ⚠ Currently on {label} — switching to {branch} for update...')
            auto_stash_ref = _m()._stash_local_changes_if_needed(git_cmd, _m().PROJECT_ROOT)
            checkout_result = subprocess.run(git_cmd + ['checkout', branch], cwd=_m().PROJECT_ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace')
            if checkout_result.returncode != 0:
                track_result = subprocess.run(git_cmd + ['checkout', '-B', branch, f'origin/{branch}'], cwd=_m().PROJECT_ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace')
                if track_result.returncode != 0:
                    if auto_stash_ref is not None:
                        _m()._restore_stashed_changes(git_cmd, _m().PROJECT_ROOT, auto_stash_ref, prompt_user=False, input_fn=gw_input_fn)
                    print(f"✗ Branch '{branch}' does not exist locally or on origin.")
                    if track_result.stderr.strip():
                        print(f'  {track_result.stderr.strip().splitlines()[0]}')
                    sys.exit(1)
        else:
            auto_stash_ref = _m()._stash_local_changes_if_needed(git_cmd, _m().PROJECT_ROOT)
        prompt_for_restore = auto_stash_ref is not None and (not assume_yes) and (gateway_mode or (sys.stdin.isatty() and sys.stdout.isatty()))
        result = subprocess.run(git_cmd + ['rev-list', f'HEAD..origin/{branch}', '--count'], cwd=_m().PROJECT_ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace', check=True)
        commit_count = int(result.stdout.strip())
        if commit_count == 0:
            _invalidate_update_cache()
            if is_fork and branch == 'main':
                _m()._sync_with_upstream_if_needed(git_cmd, _m().PROJECT_ROOT)
            if auto_stash_ref is not None:
                _m()._restore_stashed_changes(git_cmd, _m().PROJECT_ROOT, auto_stash_ref, prompt_user=prompt_for_restore, input_fn=gw_input_fn)
            if current_branch not in {branch, 'HEAD'}:
                subprocess.run(git_cmd + ['checkout', current_branch], cwd=_m().PROJECT_ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace', check=False)
            from hermes_cli.managed_uv import ensure_uv, update_managed_uv
            runtime_repairs = []
            update_managed_uv(repair_observer=runtime_repairs.append)
            ensure_uv(repair_observer=runtime_repairs.append)
            runtime_repaired = next((result for result in runtime_repairs if result.repaired), None)
            healthy, detail = _venv_core_imports_healthy()
            if not healthy:
                print('⚠ Checkout is current, but the venv is unhealthy:')
                print(f'  {detail}')
                print('→ Repairing Python dependencies...')
                _write_update_incomplete_marker()
                from hermes_cli.managed_uv import ensure_uv
                repair_uv = ensure_uv()
                venv_python_missing = not venv_python_path(_m().PROJECT_ROOT / 'venv', windows=_m()._is_windows()).exists()
                if venv_python_missing and repair_uv:
                    print('→ Recreating virtual environment...')
                    subprocess.run([repair_uv, 'venv', 'venv'], cwd=_m().PROJECT_ROOT, check=False)
                if repair_uv:
                    repair_env = {**os.environ, 'VIRTUAL_ENV': str(_m().PROJECT_ROOT / 'venv')}
                    _m()._install_python_dependencies_with_optional_fallback([repair_uv, 'pip'], env=repair_env, group='all')
                else:
                    _m()._install_python_dependencies_with_optional_fallback([sys.executable, '-m', 'pip'], group='all')
                _m()._clear_update_incomplete_marker()
                healthy_after, detail_after = _venv_core_imports_healthy()
                if healthy_after:
                    print('✓ Dependencies repaired!')
                    _print_update_completion('✓ Update complete!')
                else:
                    print(f'⚠ Venv still unhealthy after repair: {detail_after}')
                    print('  Close all Duck Agent windows/gateways and re-run: duck-agent update')
            else:
                _print_update_completion('✓ Already up to date!')
            if runtime_repaired is not None and (not _m()._is_windows()):
                print()
                print('⚠ Restart required to finish the managed Python runtime repair.')
                print('  Any running Duck Agent gateways, Desktop backends, or other long-lived processes still use the previous runtime.')
                print('  Restart each of them to pick up the repaired runtime.')
            _m()._resume_windows_gateways_after_update(_windows_gateway_resume)
            return
        print(f'→ Found {commit_count} new commit(s)')
        print('→ Pulling updates...')
        update_succeeded = False
        pre_pull_sha = _capture_head_sha(git_cmd, _m().PROJECT_ROOT)
        try:
            pull_result = subprocess.run(git_cmd + ['merge', '--ff-only', f'origin/{branch}'], cwd=_m().PROJECT_ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace')
            if pull_result.returncode != 0:
                print('  ⚠ Fast-forward not possible (history diverged), resetting to match remote...')
                reset_result = subprocess.run(git_cmd + ['reset', '--hard', f'origin/{branch}'], cwd=_m().PROJECT_ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace')
                if reset_result.returncode != 0:
                    print(f'✗ Failed to reset to origin/{branch}.')
                    if reset_result.stderr.strip():
                        print(f'  {reset_result.stderr.strip()}')
                    print(f'  Try manually: git fetch origin && git reset --hard origin/{branch}')
                    sys.exit(1)
            syntax_ok, failing_path, syntax_error = _validate_critical_files_syntax(_m().PROJECT_ROOT)
            if not syntax_ok:
                print()
                print('✗ Pulled code has a syntax error in a critical file:')
                print(f'  {failing_path}')
                if syntax_error:
                    for line in str(syntax_error).splitlines()[:6]:
                        print(f'    {line}')
                if pre_pull_sha:
                    print()
                    print(f'→ Rolling back to {pre_pull_sha[:10]}...')
                    rollback_result = subprocess.run(git_cmd + ['reset', '--hard', pre_pull_sha], cwd=_m().PROJECT_ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace')
                    if rollback_result.returncode == 0:
                        print('  ✓ Rollback complete — your install is unchanged.')
                        print('  Try ``duck-agent update`` again later once a fix lands.')
                    else:
                        print('  ✗ Rollback failed. Recover manually with:')
                        print(f'    cd {_m().PROJECT_ROOT} && git reset --hard {pre_pull_sha}')
                        if rollback_result.stderr.strip():
                            print(f'    ({rollback_result.stderr.strip().splitlines()[0]})')
                else:
                    print()
                    print('  Could not capture pre-pull SHA — recover manually with:')
                    print(f'    cd {_m().PROJECT_ROOT} && git reflog && git reset --hard <prev-sha>')
                sys.exit(1)
            update_succeeded = True
        finally:
            if auto_stash_ref is not None:
                if not update_succeeded:
                    print(f'  ℹ️  Local changes preserved in stash (ref: {auto_stash_ref})')
                    print('  Restore manually with: git stash apply')
                elif discard_local_changes:
                    _m()._discard_stashed_changes(git_cmd, _m().PROJECT_ROOT, auto_stash_ref)
                else:
                    _m()._restore_stashed_changes(git_cmd, _m().PROJECT_ROOT, auto_stash_ref, prompt_user=prompt_for_restore, input_fn=gw_input_fn)
        _invalidate_update_cache()
        removed = _m()._clear_bytecode_cache(_m().PROJECT_ROOT)
        if removed:
            print(f"  ✓ Cleared {removed} stale __pycache__ director{('y' if removed == 1 else 'ies')}")
        _m()._record_bytecode_fingerprint()
        if is_fork and branch == 'main':
            _m()._sync_with_upstream_if_needed(git_cmd, _m().PROJECT_ROOT)
        _write_update_incomplete_marker()
        print('→ Updating Python dependencies...')
        from hermes_cli.managed_uv import ensure_uv, update_managed_uv
        update_managed_uv()
        uv_bin = ensure_uv()
        pip_cmd = [sys.executable, '-m', 'pip']
        if not uv_bin:
            uv_bin = _ensure_uv_for_termux(pip_cmd)
        install_group = 'all'
        if uv_bin:
            uv_env = {**os.environ, 'VIRTUAL_ENV': str(_m().PROJECT_ROOT / 'venv')}
            if _m()._is_termux_env(uv_env):
                uv_env.pop('PYTHONPATH', None)
                uv_env.pop('PYTHONHOME', None)
                install_group = 'termux-all'
                print('  → Termux detected: using uv + curated termux-all optional profile...')
            if _m()._is_termux_env(uv_env) and _is_android_python():
                print('  → Termux/Android detected: prebuilding psutil with Linux source path compatibility...')
                _install_psutil_android_compat([uv_bin, 'pip'], env=uv_env)
            _m()._install_python_dependencies_with_optional_fallback([uv_bin, 'pip'], env=uv_env, group=install_group)
        else:
            pip_cmd = [sys.executable, '-m', 'pip']
            try:
                subprocess.run(pip_cmd + ['--version'], cwd=_m().PROJECT_ROOT, check=True, capture_output=True)
            except subprocess.CalledProcessError:
                subprocess.run([sys.executable, '-m', 'ensurepip', '--upgrade', '--default-pip'], cwd=_m().PROJECT_ROOT, check=True)
            if _m()._is_termux_env():
                install_group = 'termux-all'
                print('  → Termux detected: using curated termux-all optional profile...')
            if _m()._is_termux_env() and _is_android_python():
                print('  → Termux/Android detected: prebuilding psutil with Linux source path compatibility...')
                _install_psutil_android_compat(pip_cmd)
            _m()._install_python_dependencies_with_optional_fallback(pip_cmd, group=install_group)
        install_prefix = [uv_bin, 'pip'] if uv_bin else pip_cmd
        lazy_env = uv_env if uv_bin else None
        _m()._clear_update_incomplete_marker()
        removed = _m()._clear_bytecode_cache(_m().PROJECT_ROOT)
        if removed:
            print(f"  ✓ Cleared {removed} stale __pycache__ director{('y' if removed == 1 else 'ies')}")
        _m()._record_bytecode_fingerprint()
        _m()._reload_updated_runtime_modules()
        _write_lazy_refresh_incomplete_marker()
        _m()._upgrade_pip_before_lazy_refresh(install_prefix, env=lazy_env)
        lazy_ok = _m()._refresh_active_lazy_features(install_prefix, env=lazy_env)
        if lazy_ok:
            _m()._clear_lazy_refresh_incomplete_marker()
        else:
            print('  ⚠ Lazy-refresh recovery incomplete — run `duck-agent` again to finish import-based venv repair.')
        _m()._refresh_active_memory_provider_dependencies()
        import_ok, failing_module, import_error = _validate_critical_modules_import(_m().PROJECT_ROOT)
        if not import_ok:
            print()
            print(f'  ⚠ {failing_module} still fails to import after updating:')
            print(f'      {import_error}')
            print('    Run `duck-agent update` again — if it persists, reinstall:')
            print('    https://duck-agent.nousresearch.com')
        node_failures = _update_node_dependencies()
        _m()._build_web_ui(_m().PROJECT_ROOT / 'web')
        desktop_dir = _m().PROJECT_ROOT / 'apps' / 'desktop'
        has_desktop_app = _m()._desktop_packaged_executable(desktop_dir) is not None or _m()._desktop_dist_exists(desktop_dir)
        if (desktop_dir / 'package.json').exists() and _m()._resolve_node_runtime_npm() and has_desktop_app:
            print('→ Checking if desktop app needs rebuilding...')
            _skip_desktop_build = False
            try:
                _skip_desktop_build = not _m()._desktop_build_needed(desktop_dir, _m().PROJECT_ROOT, source_mode=False)
            except Exception:
                _skip_desktop_build = False
            if _skip_desktop_build:
                print('  ✓ Desktop app up to date')
            else:
                _desktop_build_cmd = [sys.executable, '-m', 'hermes_cli.main', 'desktop', '--build-only']
                from hermes_constants import with_hermes_node_path
                _build_env = with_hermes_node_path()
                build_result = _m()._run_logged_subprocess(_desktop_build_cmd, cwd=_m().PROJECT_ROOT, env=_build_env)
                if build_result.returncode != 0:
                    build_result = _m()._run_logged_subprocess(_desktop_build_cmd, cwd=_m().PROJECT_ROOT, env=_build_env)
                if build_result.returncode != 0:
                    print('  ⚠ Desktop build failed (non-fatal; run `duck-agent desktop` to retry)')
                    tail = '\n'.join((build_result.stdout or '').strip().splitlines()[-15:])
                    if tail:
                        print(tail)
                    from hermes_constants import display_hermes_home as _dhh
                    print(f'  Full build log: {_dhh()}/logs/update.log')
                else:
                    print('  ✓ Desktop app up to date')
        print()
        print('✓ Code updated!')
        try:
            from hermes_cli.backup import _quick_snapshot_root, verify_sqlite_integrity
            _state_path = get_hermes_home() / 'state.db'
            if _state_path.exists():
                _state_ok = verify_sqlite_integrity(_state_path, check_header=True, run_pragma=True)
                if _state_ok.get('valid'):
                    logger.debug('Post-update state.db integrity check: %s', _state_ok.get('message'))
                else:
                    print()
                    print('⚠ state.db is corrupted after update: ' + _state_ok.get('message', 'unknown error'))
                    _pre_snap_id = pre_update_snapshot_id
                    if _pre_snap_id:
                        _snap_state = _quick_snapshot_root(get_hermes_home()) / _pre_snap_id / 'state.db'
                        if _snap_state.exists():
                            _snap_ok = verify_sqlite_integrity(_snap_state, check_header=True, run_pragma=True)
                            if _snap_ok.get('valid'):
                                try:
                                    import shutil as _shutil
                                    _shutil.copy2(_snap_state, _state_path)
                                    _restored_ok = verify_sqlite_integrity(_state_path, check_header=True, run_pragma=True)
                                    if _restored_ok.get('valid'):
                                        print(f'  ✓ Auto-restored from pre-update snapshot ({_pre_snap_id})')
                                    else:
                                        print('  ✗ Auto-restore FAILED — restored copy also failed integrity')
                                except OSError as _exc:
                                    print(f'  ✗ Auto-restore file copy failed: {_exc}')
                            else:
                                print('  ✗ Pre-update snapshot also failed integrity')
                        else:
                            print('  ⚠ Pre-update snapshot does not contain state.db')
                    else:
                        print('  ⚠ No pre-update snapshot was taken')
                    print()
        except Exception as exc:
            logger.debug('Post-update state.db integrity check failed: %s', exc)
        try:
            from hermes_cli.model_catalog import seed_cache_from_checkout
            if seed_cache_from_checkout(_m().PROJECT_ROOT):
                print('  ✓ Model catalog cache refreshed from checkout')
        except Exception as e:
            logger.debug('Model catalog seed during update failed: %s', e)
        try:
            from tools.skills_sync import sync_skills
            print()
            print('→ Syncing bundled skills...')
            result = sync_skills(quiet=True)
            if result['copied']:
                print(f"  + {len(result['copied'])} new: {', '.join(result['copied'])}")
            if result.get('updated'):
                print(f"  ↑ {len(result['updated'])} updated: {', '.join(result['updated'])}")
            if result.get('user_modified'):
                print(f"  ~ {len(result['user_modified'])} user-modified (kept)")
                print('    → see them: duck-agent skills list-modified  (diff/reset to resume updates)')
            if result.get('cleaned'):
                print(f"  − {len(result['cleaned'])} removed from manifest")
            if result.get('relocated'):
                print(f"  → {len(result['relocated'])} moved to new upstream paths: {', '.join(result['relocated'])}")
            if not result['copied'] and (not result.get('updated')):
                print('  ✓ Skills are up to date')
        except Exception as e:
            logger.debug('Skills sync during update failed: %s', e)
        try:
            from hermes_cli.profiles import list_profiles, seed_profile_skills
            all_profiles = list_profiles()
            if all_profiles:
                print()
                print('→ Syncing bundled skills to all profiles...')
                for p in all_profiles:
                    try:
                        r = seed_profile_skills(p.path, quiet=True)
                        if r and r.get('skipped_opt_out'):
                            status = 'opted out (--no-skills)'
                        elif r:
                            copied = len(r.get('copied', []))
                            updated = len(r.get('updated', []))
                            modified = len(r.get('user_modified', []))
                            parts = []
                            if copied:
                                parts.append(f'+{copied} new')
                            if updated:
                                parts.append(f'↑{updated} updated')
                            if modified:
                                parts.append(f'~{modified} user-modified')
                            status = ', '.join(parts) if parts else 'up to date'
                        else:
                            status = 'sync failed'
                        print(f'  {p.name}: {status}')
                    except Exception as pe:
                        print(f'  {p.name}: error ({pe})')
        except Exception:
            pass
        try:
            from hermes_cli.profiles import backfill_profile_envs
            backfilled = backfill_profile_envs(quiet=True)
            if backfilled:
                print()
                print(f"→ Seeded .env for {len(backfilled)} profile(s) (copied from default): {', '.join(backfilled)}")
        except Exception:
            pass
        try:
            from plugins.memory.honcho.cli import sync_honcho_profiles_quiet
            synced = sync_honcho_profiles_quiet()
            if synced:
                print(f'\n-> Honcho: synced {synced} profile(s)')
        except Exception:
            pass
        print()
        print('→ Checking configuration for new options...')
        from hermes_cli.config import get_missing_env_vars, get_missing_config_fields, check_config_version, migrate_config
        missing_env = get_missing_env_vars(required_only=True)
        missing_config = get_missing_config_fields()
        current_ver, latest_ver = check_config_version()
        has_new_options = bool(missing_env or missing_config)
        version_bump_only = not has_new_options and current_ver < latest_ver
        needs_migration = has_new_options or current_ver < latest_ver
        if version_bump_only:
            print()
            print(f'  ℹ Updating config format (v{current_ver} → v{latest_ver})…')
            try:
                migrate_config(interactive=False, quiet=True)
                print('  ✓ Config format updated (no new settings to configure)')
            except Exception as _mig_err:
                print(f'  ⚠️  Config format update failed: {_mig_err}')
                print("     Run 'duck-agent config migrate' to retry.")
        elif needs_migration:
            print()

            def _print_items(items, label, key, fallback_key=None):
                if not items:
                    return
                print(f'  {label}:')
                shown = items[:8]
                for it in shown:
                    if isinstance(it, dict):
                        name = it.get(key) or (fallback_key and it.get(fallback_key)) or '?'
                        desc = (it.get('description') or '').strip()
                    else:
                        name = str(it)
                        desc = ''
                    if desc:
                        print(f'      • {name} — {desc}')
                    else:
                        print(f'      • {name}')
                extra = len(items) - len(shown)
                if extra > 0:
                    print(f'      … and {extra} more')
            if missing_env:
                print(f'  ⚠️  {len(missing_env)} new required setting(s) need configuration')
                _print_items(missing_env, 'New settings', 'name')
            if missing_config:
                print(f'  ℹ️  {len(missing_config)} new config option(s) available')
                _print_items(missing_config, 'New options', 'key')
            print()
            if assume_yes:
                print('  ℹ --yes: auto-applying config migration (skipping API-key prompts).')
                response = 'y'
            elif gateway_mode:
                response = _gateway_prompt('Would you like to configure new options now? [Y/n]', 'n').strip().lower()
            elif not (sys.stdin.isatty() and sys.stdout.isatty()):
                print('  ℹ Non-interactive session — applying safe config migrations.')
                response = 'auto'
            else:
                try:
                    response = input('Would you like to configure them now? [Y/n]: ').strip().lower()
                except EOFError:
                    response = 'n'
            if response in {'', 'y', 'yes', 'auto'}:
                print()
                interactive_migration = not (gateway_mode or assume_yes or response == 'auto')
                results = migrate_config(interactive=interactive_migration, quiet=False)
                if results['env_added'] or results['config_added']:
                    print()
                    print('✓ Configuration updated!')
                if (gateway_mode or assume_yes or response == 'auto') and missing_env:
                    print('  ℹ API keys require manual entry: duck-agent config migrate')
            else:
                print()
                print("Skipped. Run 'duck-agent config migrate' later to configure.")
        else:
            print('  ✓ Configuration is up to date')
        try:
            from hermes_cli.backup import restore_cron_jobs_if_emptied
            cron_restore = restore_cron_jobs_if_emptied(pre_update_snapshot_id)
            if cron_restore:
                print()
                print(f"  ⚠️  cron/jobs.json lost jobs during this update — restored {cron_restore['job_count']} job(s) from pre-update snapshot {cron_restore['snapshot_id']}.")
        except Exception as exc:
            logger.debug('Cron jobs auto-restore check failed: %s', exc)
        print()
        if node_failures:
            print(f"⚠ Update partially complete — Node.js dependencies for {', '.join(node_failures)} did not refresh.")
            print('  Code and Python deps are updated, but the dashboard/TUI may')
            print('  be in a mixed state until the Node deps are rebuilt.')
        else:
            _print_update_completion('✓ Update complete!')
        try:
            _print_fts_optimize_available_notice()
        except Exception as e:
            logger.debug('FTS optimize notice failed: %s', e)
        try:
            _print_curator_first_run_notice()
        except Exception as e:
            logger.debug('Curator first-run notice failed: %s', e)
        try:
            _print_curator_recent_run_notice()
        except Exception as e:
            logger.debug('Curator recent-run notice failed: %s', e)
        try:
            _ensure_fhs_path_guard()
        except Exception as e:
            logger.debug('FHS PATH guard check failed: %s', e)
        try:
            _ensure_acp_launcher()
        except Exception as e:
            logger.debug('duck-agent-acp launcher self-heal failed: %s', e)
        try:
            refresh_cua_driver = True
            try:
                from hermes_cli.config import load_config
                _update_cfg = (load_config() or {}).get('updates', {})
                if isinstance(_update_cfg, dict):
                    refresh_cua_driver = bool(_update_cfg.get('refresh_cua_driver', True))
            except Exception as cfg_exc:
                logger.debug('Could not read updates.refresh_cua_driver: %s', cfg_exc)
            if refresh_cua_driver and sys.platform in ('darwin', 'win32', 'linux') and shutil.which('cua-driver'):
                from hermes_cli.tools_config import install_cua_driver
                print()
                print('→ Refreshing cua-driver (Computer Use)...')
                install_cua_driver(upgrade=True, require_confirmed_update=True, show_installer_progress=False)
        except Exception as e:
            logger.debug('cua-driver refresh failed: %s', e)
        if gateway_mode:
            _exit_code_path = get_hermes_home() / '.update_exit_code'
            try:
                _exit_code_path.write_text('0', encoding='utf-8')
            except OSError:
                pass
        gateway_fleet_restart_incomplete = False
        try:
            from hermes_cli.gateway import is_macos, supports_systemd_services, _ensure_user_systemd_env, find_gateway_pids, find_profile_gateway_processes, _prepare_profile_gateway_update_restart, _get_service_pids, _graceful_restart_via_sigusr1, _wait_for_gateway_exit
            import signal as _signal

            def _wait_for_service_active(scope_cmd_: list, svc_name_: str, timeout: float=10.0) -> bool:
                """Poll ``systemctl is-active`` until the unit reports active.

                systemd's Stopped -> Started transition after a graceful exit
                (or a hard restart) is not instantaneous; a one-shot check
                races that window and falsely reports the unit as down.
                Poll every 0.5s up to ``timeout`` seconds before giving up.
                """
                deadline = _time.monotonic() + max(timeout, 0.5)
                while True:
                    try:
                        _verify = subprocess.run(scope_cmd_ + ['is-active', svc_name_], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
                        if _verify.stdout.strip() == 'active':
                            return True
                    except (FileNotFoundError, subprocess.TimeoutExpired):
                        pass
                    if _time.monotonic() >= deadline:
                        return False
                    _time.sleep(0.5)

            def _service_restart_sec(scope_cmd_: list, svc_name_: str, default: float=0.0) -> float:
                """Read the unit's ``RestartUSec`` (RestartSec) in seconds.

                After a graceful exit-75, systemd waits ``RestartSec`` before
                respawning the unit.  Callers that poll for ``is-active``
                must use a timeout >= ``RestartSec`` + transition slack, or
                they'll give up *during* the cooldown window and wrongly
                conclude the unit didn't relaunch.
                """
                try:
                    _show = subprocess.run(scope_cmd_ + ['show', svc_name_, '--property=RestartUSec', '--value'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    return default
                raw = (_show.stdout or '').strip()
                if not raw or raw == 'infinity':
                    return default
                total = 0.0
                matched = False
                for part in raw.split():
                    for _suf, _mult in (('ms', 0.001), ('us', 1e-06), ('min', 60.0), ('s', 1.0)):
                        if part.endswith(_suf):
                            try:
                                total += float(part[:-len(_suf)]) * _mult
                                matched = True
                            except ValueError:
                                pass
                            break
                return total if matched else default
            _manage_cmd_cache: dict = {}

            def _resolve_manage_cmd(scope_: str, scope_cmd_: list, svc_name_: str):
                """Resolve the command prefix for manage-units operations.

                Read-only systemctl calls (``is-active``, ``show``,
                ``list-units``) work unprivileged, but manage-units verbs
                (``reset-failed``, ``start``, ``restart``) on a *system*
                service trigger a polkit ``org.freedesktop.systemd1.manage-units``
                authentication prompt when run as a non-root user.  That
                interactive prompt runs inside our captured subprocess with a
                10-15s timeout — the user sees the prompt flash and "exit
                directly" before they can answer, and the resulting
                TimeoutExpired used to be swallowed silently.

                Strategy: if root, plain systemctl.  If not root, try
                non-interactive sudo (``sudo -n``) — first a blanket probe,
                then a targeted ``systemctl reset-failed`` probe so a
                least-privilege sudoers entry scoped to
                ``systemctl ... duck-agent-gateway*`` also qualifies
                (``reset-failed`` is an idempotent no-op we run before every
                privileged restart anyway).  If neither works, return None —
                the caller must SKIP the restart (without draining the
                gateway first!) and tell the user how to restart manually.
                ``--no-ask-password`` guarantees polkit can never hang a
                captured subprocess on this path.
                """
                if scope_ in _manage_cmd_cache:
                    return _manage_cmd_cache[scope_]
                cmd = scope_cmd_ + ['--no-ask-password']
                if scope_ == 'system' and hasattr(os, 'geteuid') and (os.geteuid() != 0):
                    sudo_cmd = ['sudo', '-n'] + scope_cmd_ + ['--no-ask-password']
                    sudo_ok = False
                    try:
                        _probe = subprocess.run(['sudo', '-n', 'true'], capture_output=True, timeout=5)
                        sudo_ok = _probe.returncode == 0
                        if not sudo_ok:
                            _probe = subprocess.run(sudo_cmd + ['reset-failed', svc_name_], capture_output=True, timeout=5)
                            sudo_ok = _probe.returncode == 0
                    except (FileNotFoundError, subprocess.TimeoutExpired):
                        sudo_ok = False
                    cmd = sudo_cmd if sudo_ok else None
                _manage_cmd_cache[scope_] = cmd
                return cmd
            try:
                from hermes_cli.gateway import _get_restart_exit_wait_budget
                _drain_budget = max(float(_get_restart_exit_wait_budget()), 45.0)
            except Exception:
                _drain_budget = 45.0
            restarted_services = []
            failed_or_stale_units = []
            killed_pids = set()
            relaunched_profiles = []
            externally_supervised_profiles = []
            if supports_systemd_services():
                try:
                    _ensure_user_systemd_env()
                except Exception:
                    pass
                for scope, scope_cmd in [('user', ['systemctl', '--user']), ('system', ['systemctl'])]:
                    try:
                        result = subprocess.run(scope_cmd + ['list-units', 'duck-agent-gateway*', '--plain', '--no-legend', '--no-pager'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
                    except FileNotFoundError:
                        continue
                    except subprocess.TimeoutExpired as exc:
                        print(f"  ⚠ systemctl timed out listing {scope}-scope gateway units ({(exc.cmd if exc.cmd else 'unknown command')}). Check the gateway with: duck-agent gateway status")
                        continue

                    def _restart_one_systemd_gateway_unit(svc_name: str) -> None:
                        check = subprocess.run(scope_cmd + ['is-active', svc_name], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
                        if check.stdout.strip() != 'active':
                            return
                        _manage_cmd = _resolve_manage_cmd(scope, scope_cmd, svc_name)
                        _main_pid = 0
                        try:
                            _show = subprocess.run(scope_cmd + ['show', svc_name, '--property=MainPID', '--value'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
                            _main_pid = int((_show.stdout or '').strip() or 0)
                        except (ValueError, subprocess.TimeoutExpired, FileNotFoundError):
                            _main_pid = 0
                        _graceful_ok = False
                        if _main_pid > 0:
                            print(f'  → {svc_name}: draining (up to {int(_drain_budget)}s)...')
                            _graceful_ok = _graceful_restart_via_sigusr1(_main_pid, drain_timeout=_drain_budget)
                        if _graceful_ok:
                            if _manage_cmd is not None:
                                subprocess.run(_manage_cmd + ['reset-failed', svc_name], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
                                subprocess.run(_manage_cmd + ['start', svc_name], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15)
                                if _wait_for_service_active(scope_cmd, svc_name, timeout=10.0):
                                    restarted_services.append(svc_name)
                                    return
                            _restart_sec = _service_restart_sec(scope_cmd, svc_name, default=0.0)
                            _post_drain_timeout = max(10.0, _restart_sec + 10.0)
                            if _manage_cmd is None and _restart_sec > 5.0:
                                print(f'  → {svc_name}: waiting for systemd auto-restart (~{int(_restart_sec)}s; no root for an immediate restart)...')
                            if _wait_for_service_active(scope_cmd, svc_name, timeout=_post_drain_timeout):
                                restarted_services.append(svc_name)
                                return
                            print(f"  ⚠ {svc_name} drained but didn't relaunch — forcing restart")
                        if _manage_cmd is None:
                            failed_or_stale_units.append(svc_name)
                            print(f'  ⚠ {svc_name} is a system service and restarting it needs root.\n    Restart it manually to load the new version:\n      sudo systemctl restart {svc_name}\n    To let `duck-agent update` restart it automatically, allow\n    passwordless sudo for systemctl, or run updates with sudo.')
                            return
                        subprocess.run(_manage_cmd + ['reset-failed', svc_name], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
                        restart = subprocess.run(_manage_cmd + ['restart', svc_name], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15)
                        if restart.returncode == 0:
                            if _wait_for_service_active(scope_cmd, svc_name, timeout=10.0):
                                restarted_services.append(svc_name)
                            else:
                                print(f'  ⚠ {svc_name} died after restart, retrying...')
                                subprocess.run(_manage_cmd + ['reset-failed', svc_name], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
                                subprocess.run(_manage_cmd + ['restart', svc_name], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15)
                                if _wait_for_service_active(scope_cmd, svc_name, timeout=10.0):
                                    restarted_services.append(svc_name)
                                    print(f'  ✓ {svc_name} recovered on retry')
                                else:
                                    failed_or_stale_units.append(svc_name)
                                    _scope_flag = '--user ' if scope == 'user' else ''
                                    _sudo_hint = 'sudo ' if scope == 'system' else ''
                                    print(f"  ✗ {svc_name} failed to stay running after restart.\n    Check logs: {_sudo_hint}journalctl {_scope_flag}-u {svc_name} --since '2 min ago'\n    Recover manually:\n      {_sudo_hint}systemctl {_scope_flag}reset-failed {svc_name}\n      {_sudo_hint}systemctl {_scope_flag}restart {svc_name}")
                        else:
                            failed_or_stale_units.append(svc_name)
                            print(f'  ⚠ Failed to restart {svc_name}: {restart.stderr.strip()}')

                    def _on_unit_timeout(svc_name: str, exc: subprocess.TimeoutExpired) -> None:
                        failed_or_stale_units.append(svc_name)
                        print(f"  ⚠ systemctl timed out restarting {svc_name} ({(exc.cmd if exc.cmd else 'unknown command')}); continuing with remaining gateways")
                    _for_each_systemd_gateway_unit(result.stdout, process_unit=_restart_one_systemd_gateway_unit, on_unit_timeout=_on_unit_timeout)
            if is_macos():
                try:
                    from hermes_cli.gateway import launchd_restart, get_launchd_label, get_launchd_plist_path
                    plist_path = get_launchd_plist_path()
                    if plist_path.exists():
                        check = subprocess.run(['launchctl', 'list', get_launchd_label()], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
                        if check.returncode == 0:
                            try:
                                launchd_restart()
                                restarted_services.append(get_launchd_label())
                            except subprocess.CalledProcessError as e:
                                stderr = (getattr(e, 'stderr', '') or '').strip()
                                print(f'  ⚠ Gateway restart failed: {stderr}')
                except (FileNotFoundError, subprocess.TimeoutExpired, ImportError):
                    pass
            service_pids = _get_service_pids()
            manual_pids = find_gateway_pids(exclude_pids=service_pids, all_profiles=True)
            profile_processes = {proc.pid: proc for proc in find_profile_gateway_processes(exclude_pids=service_pids) if proc.pid in manual_pids}
            for pid, proc in profile_processes.items():
                restart_mode = _prepare_profile_gateway_update_restart(proc.profile, pid)
                if restart_mode is None:
                    continue
                print(f'  → {proc.profile}: draining gateway PID {pid} (up to {int(_drain_budget)}s)...')
                drained = _graceful_restart_via_sigusr1(pid, drain_timeout=_drain_budget)
                if not drained:
                    try:
                        os.kill(pid, _signal.SIGTERM)
                    except (ProcessLookupError, PermissionError):
                        pass
                _wait_for_gateway_exit(timeout=5.0, force_after=None)
                killed_pids.add(pid)
                if restart_mode == 'external-supervisor':
                    externally_supervised_profiles.append(proc.profile)
                else:
                    relaunched_profiles.append(proc.profile)
            for pid in manual_pids:
                if pid in profile_processes:
                    continue
                try:
                    os.kill(pid, _signal.SIGTERM)
                    killed_pids.add(pid)
                except (ProcessLookupError, PermissionError):
                    pass
            if restarted_services or killed_pids:
                print()
                for svc in restarted_services:
                    print(f'  ✓ Restarted {svc}')
                if relaunched_profiles:
                    names = ', '.join(relaunched_profiles)
                    print(f'  ✓ Restarting manual gateway profile(s): {names}')
                if externally_supervised_profiles:
                    names = ', '.join(externally_supervised_profiles)
                    print(f'  ✓ Handed gateway profile(s) back to their external supervisor: {names}')
                unmapped_count = len(killed_pids) - len(relaunched_profiles) - len(externally_supervised_profiles)
                if unmapped_count:
                    print(f'  → Stopped {unmapped_count} manual gateway process(es)')
                    print('    Restart manually: duck-agent gateway run')
                    if unmapped_count > 1:
                        print('    (or: duck-agent -p <profile> gateway run  for each profile)')
            if failed_or_stale_units:
                gateway_fleet_restart_incomplete = True
                if gateway_mode:
                    _exit_code_path = get_hermes_home() / '.update_exit_code'
                    try:
                        _exit_code_path.write_text('1', encoding='utf-8')
                    except OSError:
                        pass
            _warn_incomplete_gateway_fleet_restart(failed_or_stale_units)
            if not restarted_services and (not killed_pids):
                pass
            try:
                _time.sleep(3.0)
                _service_pids_after = _get_service_pids()
                _surviving = find_gateway_pids(exclude_pids=_service_pids_after, all_profiles=True)
                _stuck = [pid for pid in _surviving if pid in killed_pids]
                if _stuck:
                    print()
                    print(f'  ⚠ {len(_stuck)} gateway process(es) ignored SIGTERM — force-killing')
                    from gateway.status import terminate_pid as _terminate_pid
                    for pid in _stuck:
                        try:
                            _terminate_pid(pid, force=True)
                        except (ProcessLookupError, PermissionError, OSError):
                            pass
                    _time.sleep(1.5)
            except Exception as _sweep_exc:
                logger.debug('Post-restart survivor sweep failed: %s', _sweep_exc)
        except Exception as e:
            logger.debug('Gateway restart during update failed: %s', e)
        _m()._resume_windows_gateways_after_update(_windows_gateway_resume)
        try:
            from hermes_cli.gateway import has_legacy_hermes_units, _find_legacy_hermes_units, supports_systemd_services
            if supports_systemd_services() and has_legacy_hermes_units():
                print()
                print('⚠ Legacy Duck Agent gateway unit(s) detected:')
                for name, path, is_sys in _find_legacy_hermes_units():
                    scope = 'system' if is_sys else 'user'
                    print(f'    {path}  ({scope} scope)')
                print()
                print('  These pre-rename units (duck-agent.service) fight the current')
                print('  duck-agent-gateway.service for the bot token and cause SIGTERM')
                print('  flap loops. Remove them with:')
                print()
                print('    duck-agent gateway migrate-legacy')
                print()
                print('  (add `sudo` if any are in system scope)')
        except Exception as e:
            logger.debug('Legacy unit check during update failed: %s', e)
        _finish_dashboard_update_cleanup(node_failures)
        print()
        print('Tip: You can now select a provider and model:')
        print('  duck-agent model              # Select provider and model')
        if gateway_fleet_restart_incomplete:
            sys.exit(1)
    except subprocess.CalledProcessError as e:
        if sys.platform == 'win32':
            print(f'⚠ Git update failed: {e}')
            print('→ Falling back to ZIP download...')
            print()
            _update_via_zip(args)
        else:
            print(f'✗ Update failed: {e}')
            sys.exit(1)

def _print_items(items, label, key, fallback_key=None):
    if not items:
        return
    print(f'  {label}:')
    shown = items[:8]
    for it in shown:
        if isinstance(it, dict):
            name = it.get(key) or (fallback_key and it.get(fallback_key)) or '?'
            desc = (it.get('description') or '').strip()
        else:
            name = str(it)
            desc = ''
        if desc:
            print(f'      • {name} — {desc}')
        else:
            print(f'      • {name}')
    extra = len(items) - len(shown)
    if extra > 0:
        print(f'      … and {extra} more')

def _wait_for_service_active(scope_cmd_: list, svc_name_: str, timeout: float=10.0) -> bool:
    """Poll ``systemctl is-active`` until the unit reports active.

    systemd's Stopped -> Started transition after a graceful exit
    (or a hard restart) is not instantaneous; a one-shot check
    races that window and falsely reports the unit as down.
    Poll every 0.5s up to ``timeout`` seconds before giving up.
    """
    deadline = _time.monotonic() + max(timeout, 0.5)
    while True:
        try:
            _verify = subprocess.run(scope_cmd_ + ['is-active', svc_name_], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
            if _verify.stdout.strip() == 'active':
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        if _time.monotonic() >= deadline:
            return False
        _time.sleep(0.5)

def _service_restart_sec(scope_cmd_: list, svc_name_: str, default: float=0.0) -> float:
    """Read the unit's ``RestartUSec`` (RestartSec) in seconds.

    After a graceful exit-75, systemd waits ``RestartSec`` before
    respawning the unit.  Callers that poll for ``is-active``
    must use a timeout >= ``RestartSec`` + transition slack, or
    they'll give up *during* the cooldown window and wrongly
    conclude the unit didn't relaunch.
    """
    try:
        _show = subprocess.run(scope_cmd_ + ['show', svc_name_, '--property=RestartUSec', '--value'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return default
    raw = (_show.stdout or '').strip()
    if not raw or raw == 'infinity':
        return default
    total = 0.0
    matched = False
    for part in raw.split():
        for _suf, _mult in (('ms', 0.001), ('us', 1e-06), ('min', 60.0), ('s', 1.0)):
            if part.endswith(_suf):
                try:
                    total += float(part[:-len(_suf)]) * _mult
                    matched = True
                except ValueError:
                    pass
                break
    return total if matched else default