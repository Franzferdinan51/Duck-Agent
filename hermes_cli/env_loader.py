"""Helpers for loading Duck Agent .env files consistently across entrypoints."""
from __future__ import annotations
import codecs
import io
import os
import sys
import threading
from pathlib import Path
from dotenv import load_dotenv
from utils import atomic_replace, fast_safe_load
_CREDENTIAL_SUFFIXES = ('_API_KEY', '_TOKEN', '_SECRET', '_KEY')
_WARNED_KEYS: set[str] = set()
_WARNED_UTF32_PATHS: set[str] = set()
_SECRET_SOURCES: dict[str, str] = {}
_SECRET_SOURCE_VALUES_BY_HOME: dict[str, dict[str, str]] = {}
_APPLIED_HOMES: set[str] = set()
_SECRET_SOURCE_CACHE_LOCK = threading.RLock()

def _known_hermes_env_keys() -> set[str]:
    """Return the combined set of known Duck Agent env-var keys.

    Includes both ``OPTIONAL_ENV_VARS`` (setup-flow vars with metadata) and
    ``_EXTRA_ENV_KEYS`` (provider/platform keys managed outside the setup
    wizard).  Lazy-imported to avoid circular-dependency during early-bootstrap
    ``load_hermes_dotenv()`` calls.
    """
    from hermes_cli.config import _EXTRA_ENV_KEYS
    from hermes_cli.config_defaults import OPTIONAL_ENV_VARS
    return set(OPTIONAL_ENV_VARS.keys()) | set(_EXTRA_ENV_KEYS)
_PROFILE_MANAGED_ENV_KEYS: frozenset[str] = frozenset({'HERMES_ACP_AUTH_METHOD', 'HERMES_ACP_AUTO_APPROVE', 'HERMES_COPILOT_ACP_COMMAND', 'HERMES_COPILOT_ACP_ARGS', 'COPILOT_CLI_PATH', 'COPILOT_ACP_BASE_URL'})

def _env_keys_defined_in_dotenv(path: Path) -> set[str]:
    """Return KEY names assigned in a dotenv file (including empty ``KEY=``).

    Uses a fast line scanner rather than full dotenv parsing so it works
    during early bootstrap without importing python-dotenv.  Ignores comment
    and blank lines.  Non-ASCII encoding errors fall back to ``latin-1``,
    matching ``_load_dotenv_with_fallback``.
    """
    keys: set[str] = set()
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except Exception:
        try:
            text = path.read_text(encoding='latin-1', errors='replace')
        except Exception:
            return keys
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        if line.startswith('export '):
            line = line[7:]
        key = line.split('=', 1)[0].strip()
        if key:
            keys.add(key)
    return keys

def _clear_known_keys_missing_from_dotenv(path: Path) -> None:
    """Remove inherited profile-managed Duck Agent keys absent from ``.env``.

    After the profile's ``.env`` has been loaded with ``override=True``,
    scan the file for which profile-managed keys it explicitly defines and
    delete any such key that exists in ``os.environ`` but is *not* present
    in the file.

    Scope is deliberately NARROW: only ``_PROFILE_MANAGED_ENV_KEYS`` —
    behavioral routing keys (ACP auth method, copilot-ACP endpoints) that a
    parent Duck Agent process injects and that silently change *which provider
    path* a profile uses. Provider API keys (OPENAI_API_KEY, …) are
    intentionally excluded: users legitimately export those in their shell
    (``export OPENAI_API_KEY=…`` is a documented flow — see
    ``tests/hermes_cli/test_dump_env_visibility.py``), and a startup scrub
    cannot distinguish a shell export from parent-process leakage. Clearing
    the full known-key set would delete user-exported credentials on every
    ``duck-agent`` invocation.

    Cross-profile *credential* isolation is handled at read time by
    ``agent.secret_scope.get_secret`` (scope authoritative under
    multiplexing), not by mutating ``os.environ`` here.

    Does **not** run when the ``.env`` file does not exist (bare-profile
    case, which follows ``#66930`` / ``#67027`` semantics).
    """
    if not path.exists():
        return
    defined = _env_keys_defined_in_dotenv(path)
    for key in _PROFILE_MANAGED_ENV_KEYS:
        if key not in defined and key in os.environ:
            del os.environ[key]

def get_secret_source(env_var: str) -> str | None:
    """Return the label of the secret source that supplied ``env_var``, if any.

    Returns ``"bitwarden"`` for keys pulled from Bitwarden Secrets Manager
    during the current process's ``load_hermes_dotenv()`` call.  Returns
    ``None`` for keys that came from ``.env``, the shell environment, or
    aren't tracked.  The returned label is metadata only: credential-pool
    persistence may store it to explain the origin of a borrowed secret, but
    must never treat it as authorization to persist the raw value.
    """
    return _SECRET_SOURCES.get(env_var)

def get_secret_source_values(hermes_home: str | os.PathLike) -> dict[str, str]:
    """Return the external-secret value snapshot for ``duck_agent_home``."""
    home_key = str(Path(hermes_home).resolve())
    return dict(_SECRET_SOURCE_VALUES_BY_HOME.get(home_key, {}))

def hydrate_profile_secret_sources(hermes_home: str | os.PathLike) -> dict[str, str]:
    """Resolve one profile's configured sources without mutating ``os.environ``.

    Multiplex gateways can route a first turn to a secondary profile that has
    never run the process-global dotenv startup path.  Resolve that profile's
    sources against a private mapping seeded from its own ``.env`` and record
    the usual per-home snapshot for ``build_profile_secret_scope()``.

    Fail-open and once-per-home semantics intentionally mirror
    ``_apply_external_secret_sources``.  The returned mapping contains only
    values actually contributed by external sources, never the profile's
    plaintext ``.env`` entries.
    """
    with _SECRET_SOURCE_CACHE_LOCK:
        return _hydrate_profile_secret_sources(Path(hermes_home))

def _hydrate_profile_secret_sources(home: Path) -> dict[str, str]:
    """Locked implementation for :func:`hydrate_profile_secret_sources`."""
    home_key = str(home.resolve())
    if home_key in _APPLIED_HOMES:
        return get_secret_source_values(home)
    try:
        cfg = _load_secrets_config(home)
    except Exception:
        return {}
    if not cfg:
        return {}
    try:
        from agent.secret_scope import _is_global_env, load_env_file
        from agent.secret_sources.registry import apply_all
        local_env = {name: value for name, value in os.environ.items() if _is_global_env(name)}
        local_env.update(load_env_file(home / '.env'))
        op_env = home / '.op.env'
        if op_env.exists():
            for _name, _value in load_env_file(op_env).items():
                local_env.setdefault(_name, _value)
        local_env['DUCK_AGENT_HOME'] = str(home)
        report = apply_all(cfg, home, environ=local_env)
    except Exception:
        return {}
    if not report.sources:
        return {}
    _APPLIED_HOMES.add(home_key)
    values: dict[str, str] = {}
    for name, applied in report.provenance.items():
        value = local_env.get(name)
        if value is None:
            continue
        _SECRET_SOURCES[name] = applied.source
        values[name] = value
    if values:
        _SECRET_SOURCE_VALUES_BY_HOME[home_key] = values
    return dict(values)

def reset_secret_source_cache() -> None:
    """Forget which DUCK_AGENT_HOME paths have already had external secrets applied.

    The first call to ``_apply_external_secret_sources(home_path)`` in a
    process pulls from Bitwarden (or other configured backend), records the
    applied keys in ``_SECRET_SOURCES``, and remembers ``home_path`` so
    subsequent calls in the same process are no-ops.  Call this to force the
    next call to re-pull — useful for tests, and for long-running processes
    that want to refresh after a config change.
    """
    _APPLIED_HOMES.clear()
    _SECRET_SOURCES.clear()
    _SECRET_SOURCE_VALUES_BY_HOME.clear()

def format_secret_source_suffix(env_var: str) -> str:
    """Return a human-readable suffix like ``" (from Bitwarden)"`` or ``""``.

    Use this when printing a detected credential so the user can see where
    it came from.  Empty string when the credential came from ``.env`` or
    the shell — those are the implicit / "default" cases users already
    understand.
    """
    source = get_secret_source(env_var)
    if not source:
        return ''
    if source == 'bitwarden':
        return ' (from Bitwarden)'
    try:
        from agent.secret_sources.registry import get_source
        registered = get_source(source)
        if registered is not None and registered.label:
            return f' (from {registered.label})'
    except Exception:
        pass
    return f' (from {source})'

def _format_offending_chars(value: str, limit: int=3) -> str:
    """Return a compact 'U+XXXX ('c'), ...' summary of non-ASCII codepoints."""
    seen: list[str] = []
    for ch in value:
        if ord(ch) > 127:
            label = f'U+{ord(ch):04X}'
            if ch.isprintable():
                label += f' ({ch!r})'
            if label not in seen:
                seen.append(label)
            if len(seen) >= limit:
                break
    return ', '.join(seen)

def _sanitize_loaded_credentials() -> None:
    """Strip non-ASCII characters from credential env vars in os.environ.

    Called after dotenv loads so the rest of the codebase never sees
    non-ASCII API keys.  Only touches env vars whose names end with
    known credential suffixes (``_API_KEY``, ``_TOKEN``, etc.).

    Emits a one-line warning to stderr when characters are stripped.
    Silent stripping would mask copy-paste corruption (Unicode lookalike
    glyphs from PDFs / rich-text editors, ZWSP from web pages) as opaque
    provider-side "invalid API key" errors (see #6843).
    """
    for key, value in list(os.environ.items()):
        if not any((key.endswith(suffix) for suffix in _CREDENTIAL_SUFFIXES)):
            continue
        try:
            value.encode('ascii')
            continue
        except UnicodeEncodeError:
            pass
        cleaned = value.encode('ascii', errors='ignore').decode('ascii')
        os.environ[key] = cleaned
        if key in _WARNED_KEYS:
            continue
        _WARNED_KEYS.add(key)
        stripped = len(value) - len(cleaned)
        detail = _format_offending_chars(value) or 'non-printable'
        print(f"  Warning: {key} contained {stripped} non-ASCII character{('s' if stripped != 1 else '')} ({detail}) — stripped so the key can be sent as an HTTP header.", file=sys.stderr)
        print('  This usually means the key was copy-pasted from a PDF, rich-text editor, or web page that substituted lookalike\n  Unicode glyphs for ASCII letters. If authentication fails (e.g. "API key not valid"), re-copy the key from the\n  provider\'s dashboard and run `duck-agent setup` (or edit the .env file in a plain-text editor).', file=sys.stderr)

def _load_dotenv_with_fallback(path: Path, *, override: bool) -> None:
    try:
        load_dotenv(dotenv_path=path, override=override, encoding='utf-8')
    except UnicodeDecodeError:
        load_dotenv(dotenv_path=path, override=override, encoding='latin-1')
    _sanitize_loaded_credentials()

def _sanitize_env_file_if_needed(path: Path) -> None:
    """Pre-sanitize a .env file before python-dotenv reads it.

    Strips embedded null bytes which crash ``os.environ[k] = v``
    with ``ValueError: embedded null byte`` — typically introduced by
    copy-pasting API keys from terminals or rich-text editors.

    Encoding: sniffs a leading BOM *before* any text decode. UTF-16
    (Notepad "Unicode") is decoded correctly and rewritten as clean
    UTF-8. UTF-32 is refused (left untouched) so we never fall through
    to the errors=replace corruption path. Order of BOM checks matters:
    UTF-32-LE's BOM starts with UTF-16-LE's FF FE.

    ``hermes_cli.config._sanitize_env_lines`` normalizes line endings while
    treating content after the first ``=`` as opaque for boundary discovery.
    """
    if not path.exists():
        return
    try:
        from hermes_cli.config import _sanitize_env_lines
    except ImportError:
        return
    try:
        raw = path.read_bytes()
    except Exception:
        return
    force_utf8_rewrite = False
    if raw.startswith(codecs.BOM_UTF32_LE) or raw.startswith(codecs.BOM_UTF32_BE):
        path_key = str(path.resolve())
        if path_key not in _WARNED_UTF32_PATHS:
            _WARNED_UTF32_PATHS.add(path_key)
            import logging
            logging.getLogger(__name__).warning('Skipping .env sanitize for %s: UTF-32 BOM detected; leaving file untouched to avoid corruption', path)
        return
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        try:
            with io.TextIOWrapper(io.BytesIO(raw), encoding='utf-16', newline=None) as f:
                original = f.readlines()
        except UnicodeDecodeError:
            return
        force_utf8_rewrite = True
    else:
        try:
            with open(path, encoding='utf-8-sig', errors='replace') as f:
                original = f.readlines()
        except Exception:
            return
        if original and original[0].startswith('�'):
            return
    try:
        stripped = [line.replace('\x00', '') for line in original]
        sanitized = _sanitize_env_lines(stripped)
        if sanitized != original or force_utf8_rewrite:
            import tempfile
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix='.tmp', prefix='.env_')
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    f.writelines(sanitized)
                    f.flush()
                    os.fsync(f.fileno())
                atomic_replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
    except Exception:
        pass

def load_hermes_dotenv(*, hermes_home: str | os.PathLike | None=None, project_env: str | os.PathLike | None=None) -> list[Path]:
    """Load Duck Agent environment files with user config taking precedence.

    Behavior:
    - `~/.duck-agent/.env` overrides stale shell-exported values when present.
    - project `.env` acts as a dev fallback and only fills missing values when
      the user env exists.
    - if no user env exists, the project `.env` also overrides stale shell vars.
    """
    loaded: list[Path] = []
    home_path = Path(hermes_home or os.getenv('DUCK_AGENT_HOME', Path.home() / '.duck-agent'))
    user_env = home_path / '.env'
    project_env_path = Path(project_env) if project_env else None
    if user_env.exists():
        _sanitize_env_file_if_needed(user_env)
    if project_env_path and project_env_path.exists():
        _sanitize_env_file_if_needed(project_env_path)
    if user_env.exists():
        _load_dotenv_with_fallback(user_env, override=True)
        loaded.append(user_env)
        _clear_known_keys_missing_from_dotenv(user_env)
    op_env = home_path / '.op.env'
    if op_env.exists() and (not os.environ.get('OP_SERVICE_ACCOUNT_TOKEN')):
        _load_dotenv_with_fallback(op_env, override=False)
    if project_env_path and project_env_path.exists():
        _load_dotenv_with_fallback(project_env_path, override=not loaded)
        loaded.append(project_env_path)
    _apply_external_secret_sources(home_path)
    _apply_managed_env()
    _reapply_terminal_config_bridge(home_path)
    return loaded

def _reapply_terminal_config_bridge(home_path: Path) -> None:
    """Re-assert config.yaml's explicit ``terminal.*`` keys over reloaded .env.

    Delegates to ``hermes_cli.config.apply_terminal_config_to_env`` — the
    single shared bridge (same one terminal_tool's fallback and the TUI/
    dashboard launchers use) — so key coverage, explicit-keys-only override
    semantics, cwd placeholder handling, and the managed-scope overlay can't
    drift from the other bridge sites. Only keys the user actually wrote in
    config.yaml's ``terminal`` section override env values; a config.yaml
    without a terminal section leaves .env/shell selections untouched.

    Scoped to the process DUCK_AGENT_HOME: the shared bridge reads the
    process-global config, so re-applying it for a *different* profile's
    ``load_hermes_dotenv(duck_agent_home=...)`` call would bridge the wrong
    profile's config. Fail-open — a config problem must never break dotenv
    loading (the historical env-driven behavior still applies).
    """
    try:
        if Path(home_path).resolve() != _process_hermes_home().resolve():
            return
        from hermes_cli.config import apply_terminal_config_to_env
        apply_terminal_config_to_env(env=None)
    except Exception:
        pass

def _apply_managed_env() -> None:
    """Apply the managed-scope .env last, with override, so it beats user/shell.

    Managed scope is machine-global (independent of DUCK_AGENT_HOME / profile). v1
    enforcement is "applied last with override=True" — at the end of startup load
    ``os.environ`` holds the managed value for every managed key, beating both the
    user ``.env`` and any pre-existing shell export. This deliberately inverts the
    usual env-over-config precedence for the pinned keys (see
    ``docs/design/managed-scope.md`` §4.1).

    This does NOT prevent the agent from later mutating ``os.environ`` in-process
    or ``export``-ing in a subprocess shell; that hard boundary is a documented
    v2 item (design §8.1). v1 relies on filesystem permissions only.

    Fail-open: a missing managed dir or .env is the common case and a no-op; any
    error here is swallowed so managed scope can never block startup.
    """
    try:
        from hermes_cli import managed_scope
        managed_dir = managed_scope.get_managed_dir()
    except Exception:
        return
    if managed_dir is None:
        return
    managed_env = managed_dir / '.env'
    if not managed_env.exists():
        return
    _sanitize_env_file_if_needed(managed_env)
    _load_dotenv_with_fallback(managed_env, override=True)

def _apply_external_secret_sources(home_path: Path) -> None:
    """Pull secrets from every enabled external source into env.

    Runs AFTER dotenv loads so .env values are visible (sources use them
    to locate bootstrap tokens) but BEFORE the rest of Duck Agent reads
    ``os.environ`` for credentials.  Any failure here is logged and
    swallowed — external secret sources must never block startup.

    The heavy lifting (source ordering, mapped-beats-bulk precedence,
    first-claim-wins conflict handling, override semantics, provenance)
    lives in ``agent.secret_sources.registry.apply_all``; this wrapper
    owns the once-per-DUCK_AGENT_HOME guard, the post-apply ASCII
    sanitization sweep, the ``_SECRET_SOURCES`` provenance map that
    UI surfaces read, and the startup status lines.

    Idempotent within a process: subsequent calls for the same
    ``home_path`` are no-ops.  ``load_hermes_dotenv()`` runs at import
    time from several hot modules (cli.py, hermes_cli/main.py,
    run_agent.py, trajectory_compressor.py, ...), so without this guard
    the status lines would print 3-5x per CLI startup.  Use
    ``reset_secret_source_cache()`` if you need to force a re-pull
    (tests, long-running processes after a config change).
    """
    home_key = str(Path(home_path).resolve())
    if home_key in _APPLIED_HOMES:
        return
    try:
        cfg = _load_secrets_config(home_path)
    except Exception:
        return
    if not cfg:
        return
    try:
        from agent.secret_sources.registry import apply_all
    except ImportError:
        return
    try:
        report = apply_all(cfg, home_path)
    except Exception:
        return
    if not report.sources:
        return
    _APPLIED_HOMES.add(home_key)
    if report.applied_any:
        _sanitize_loaded_credentials()
        values: dict[str, str] = {}
        for name, applied in report.provenance.items():
            _SECRET_SOURCES[name] = applied.source
            if name in os.environ:
                values[name] = os.environ[name]
        _SECRET_SOURCE_VALUES_BY_HOME[home_key] = values
    for src in report.sources:
        if src.applied:
            print(f"  {src.label}: applied {len(src.applied)} secret{('s' if len(src.applied) != 1 else '')}", file=sys.stderr)
        if src.result.error:
            print(f'  {src.label}: {src.result.error}', file=sys.stderr)
            hint = _remediation_hint(src.name, src.result.error_kind, cfg)
            if hint:
                print(f'  {src.label}: → {hint}', file=sys.stderr)
        for warn in src.result.warnings:
            print(f'  {src.label}: {warn}', file=sys.stderr)
    for conflict in report.conflicts:
        print(f'  Secret sources: {conflict}', file=sys.stderr)

def _remediation_hint(source_name: str, error_kind, secrets_cfg: dict) -> str:
    """Ask the failed source for its one-line fix-it hint.

    Defensive wrapper: remediation() is a pure mapping and shouldn't
    raise, but a plugin source could — and startup must never break on
    a status line.
    """
    try:
        from agent.secret_sources.registry import get_source
        source = get_source(source_name)
        if source is None:
            return ''
        src_cfg = secrets_cfg.get(source_name)
        src_cfg = src_cfg if isinstance(src_cfg, dict) else {}
        return str(source.remediation(error_kind, src_cfg) or '').strip()
    except Exception:
        return ''

def _load_secrets_config(home_path: Path) -> dict:
    """Read just the ``secrets:`` section out of config.yaml.

    Imported lazily and isolated from the main config loader so a
    malformed config can't take down dotenv loading entirely.
    """
    config_path = home_path / 'config.yaml'
    if not config_path.exists():
        return {}
    if home_path == _process_hermes_home():
        try:
            from hermes_cli.config import read_raw_config
            data = read_raw_config() or {}
            return data.get('secrets') or {}
        except Exception:
            pass
    try:
        import yaml
    except ImportError:
        return {}
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            data = fast_safe_load(f) or {}
    except Exception:
        return {}
    return data.get('secrets') or {}

def _process_hermes_home() -> Path:
    """The DUCK_AGENT_HOME the shared config cache is keyed to."""
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home()
    except Exception:
        return Path.home() / '.duck-agent'