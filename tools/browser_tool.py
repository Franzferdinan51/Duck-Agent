"""
Browser Tool Module

This module provides browser automation tools using agent-browser CLI.  It
supports multiple backends — **Browser Use** (cloud, default for Nous
subscribers), **Browserbase** (cloud, direct credentials), and **local
Chromium** — with identical agent-facing behaviour.  The backend is
auto-detected from config and available credentials.

The tool uses agent-browser's accessibility tree (ariaSnapshot) for text-based
page representation, making it ideal for LLM agents without vision capabilities.

Features:
- **Local mode** (default): zero-cost headless Chromium via agent-browser.
  Works on Linux servers without a display.  One-time setup:
  ``agent-browser install`` (downloads Chromium) or
  ``agent-browser install --with-deps`` (also installs system libraries for
  Debian/Ubuntu/Docker).
- **Cloud mode**: Browserbase or Browser Use cloud execution when configured.
- Session isolation per task ID
- Text-based page snapshots using accessibility tree
- Element interaction via ref selectors (@e1, @e2, etc.)
- Task-aware content extraction using LLM summarization
- Automatic cleanup of browser sessions

Environment Variables:
- BROWSERBASE_API_KEY: API key for direct Browserbase cloud mode
- BROWSERBASE_PROJECT_ID: Project ID for direct Browserbase cloud mode
- BROWSER_USE_API_KEY: API key for direct Browser Use cloud mode
- BROWSERBASE_PROXIES: Enable/disable residential proxies (default: "true")
- BROWSERBASE_ADVANCED_STEALTH: Enable advanced stealth mode with custom Chromium,
  requires Scale Plan (default: "false")
- BROWSERBASE_KEEP_ALIVE: Enable keepAlive for session reconnection after disconnects,
  requires paid plan (default: "true")
- BROWSERBASE_SESSION_TIMEOUT: Custom session timeout in seconds (max 21600 = 6h).
  Set to extend beyond project default. Common values: 600 (10min), 1800 (30min) (default: none)

Usage:
    from tools.browser_tool import browser_navigate, browser_snapshot, browser_click

    # Navigate to a page
    result = browser_navigate("https://example.com", task_id="task_123")

    # Get page snapshot
    snapshot = browser_snapshot(task_id="task_123")

    # Click an element
    browser_click("@e5", task_id="task_123")
"""
import atexit
import functools
import json
import logging
import os
import re
import subprocess
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple, Union
from pathlib import Path
from agent.redact import redact_cdp_url
from hermes_constants import agent_browser_runnable, get_hermes_home, get_hermes_home_override
from utils import env_int, is_truthy_value
from hermes_cli.config import DEFAULT_CONFIG, cfg_get
from hermes_cli._subprocess_compat import windows_hide_flags

def __getattr__(name: str):
    """Lazy module attributes (PEP 562) — import diet for cold start.

    ``requests`` (~40 ms) and ``agent.auxiliary_client.call_llm`` (~65 ms)
    are only needed on specific code paths, so they load on first use. The
    module-level names are preserved for the test-patch surface
    (``patch("tools.browser_tool.requests.get")`` /
    ``patch("tools.browser_tool.call_llm")``): first attribute access imports
    the real object and binds it into module globals.
    """
    if name == 'requests':
        import requests as _requests
        globals()['requests'] = _requests
        return _requests
    if name == 'call_llm':
        from agent.auxiliary_client import call_llm as _call_llm
        globals()['call_llm'] = _call_llm
        return _call_llm
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')

def _lazy_call_llm(*args, **kwargs):
    """Invoke ``call_llm`` through module globals so test patches of
    ``tools.browser_tool.call_llm`` are honored, importing lazily otherwise."""
    fn = globals().get('call_llm')
    if fn is None:
        fn = __getattr__('call_llm')
    return fn(*args, **kwargs)
_BROWSER_PASSTHROUGH_KEYS: tuple[str, ...] = ('BROWSERBASE_API_KEY', 'BROWSERBASE_PROJECT_ID', 'BROWSER_USE_API_KEY', 'FIRECRAWL_API_KEY', 'FIRECRAWL_API_URL', 'FIRECRAWL_BROWSER_TTL')

def _build_browser_env() -> dict:
    """Credential-scrubbed env for an agent-browser subprocess.

    Strips Duck Agent-managed secrets (provider keys, gateway tokens, GitHub auth,
    infra secrets) then re-adds only the browser-backend keys the worker needs.
    The ``hermes_subprocess_env`` import is deferred to keep ``browser_tool``
    importable under test harnesses that load it against a stubbed ``tools``
    package (tests/tools/test_managed_browserbase_and_modal.py).
    """
    from tools.environments.local import hermes_subprocess_env
    env = hermes_subprocess_env(inherit_credentials=False)
    for _key in _BROWSER_PASSTHROUGH_KEYS:
        if _key in os.environ:
            env[_key] = os.environ[_key]
    return env
try:
    from tools.website_policy import check_website_access
except Exception:
    check_website_access = lambda url: None
try:
    from tools.url_safety import is_safe_url as _is_safe_url, is_always_blocked_url as _is_always_blocked_url, normalize_url_for_request as _normalize_url_for_request, sensitive_query_param_name as _sensitive_query_param_name
except Exception:
    _is_safe_url = lambda url: False
    _is_always_blocked_url = lambda url: True
    _normalize_url_for_request = lambda url: url
    _sensitive_query_param_name = lambda url: None
from agent.browser_provider import BrowserProvider as CloudBrowserProvider
from agent.browser_registry import get_provider as _registry_get_browser_provider
from plugins.browser.browserbase.provider import BrowserbaseBrowserProvider as BrowserbaseProvider
from plugins.browser.browser_use.provider import BrowserUseBrowserProvider as BrowserUseProvider
from plugins.browser.firecrawl.provider import FirecrawlBrowserProvider as FirecrawlProvider
from tools.tool_backend_helpers import normalize_browser_cloud_provider
try:
    from tools.browser_camofox import is_camofox_mode as _is_camofox_mode
except ImportError:
    _is_camofox_mode = lambda: False
logger = logging.getLogger(__name__)
_SANE_PATH_DIRS = ('/data/data/com.termux/files/usr/bin', '/data/data/com.termux/files/usr/sbin', '/opt/homebrew/bin', '/opt/homebrew/sbin', '/usr/local/sbin', '/usr/local/bin', '/usr/sbin', '/usr/bin', '/sbin', '/bin')
_SANE_PATH = os.pathsep.join(_SANE_PATH_DIRS)

@functools.lru_cache(maxsize=1)
def _discover_homebrew_node_dirs() -> tuple[str, ...]:
    """Find Homebrew versioned Node.js bin directories (e.g. node@20, node@24).

    When Node is installed via ``brew install node@24`` and NOT linked into
    /opt/homebrew/bin, agent-browser isn't discoverable on the default PATH.
    This function finds those directories so they can be prepended.
    """
    dirs: list[str] = []
    homebrew_opt = '/opt/homebrew/opt'
    if not os.path.isdir(homebrew_opt):
        return tuple(dirs)
    try:
        for entry in os.listdir(homebrew_opt):
            if entry.startswith('node') and entry != 'node':
                bin_dir = os.path.join(homebrew_opt, entry, 'bin')
                if os.path.isdir(bin_dir):
                    dirs.append(bin_dir)
    except OSError:
        pass
    return tuple(dirs)

def _browser_candidate_path_dirs() -> list[str]:
    """Return ordered browser CLI PATH candidates shared by discovery and execution."""
    hermes_home = get_hermes_home()
    hermes_node_bin = str(hermes_home / 'node' / 'bin')
    hermes_node_root = str(hermes_home / 'node')
    hermes_nm_bin = str(hermes_home / 'node_modules' / '.bin')
    return [hermes_node_bin, hermes_node_root, hermes_nm_bin, *list(_discover_homebrew_node_dirs()), *_SANE_PATH_DIRS]

def _merge_browser_path(existing_path: str='') -> str:
    """Prepend browser-specific PATH fallbacks without reordering existing entries."""
    path_parts = [p for p in (existing_path or '').split(os.pathsep) if p]
    existing_parts = set(path_parts)
    prefix_parts: list[str] = []
    for part in _browser_candidate_path_dirs():
        if not part or part in existing_parts or part in prefix_parts:
            continue
        if os.path.isdir(part):
            prefix_parts.append(part)
    return os.pathsep.join(prefix_parts + path_parts)
_last_screenshot_cleanup_by_dir: dict[str, float] = {}
DEFAULT_COMMAND_TIMEOUT = 30
MIN_OPEN_TIMEOUT = 60
MIN_FIRST_OPEN_TIMEOUT = 120
SNAPSHOT_SUMMARIZE_THRESHOLD = 15000
MAX_STORED_SNAPSHOT_CHARS = 2000000
_EMPTY_OK_COMMANDS: frozenset = frozenset({'close', 'record'})
_cached_command_timeout: Optional[int] = None
_command_timeout_resolved = False

def _sanitize_url_for_logs(value: object) -> str:
    """Mask secrets in logged browser endpoint URLs and URL-like errors.

    Thin wrapper over :func:`agent.redact.redact_cdp_url`, which is the single
    source of truth for CDP-URL log redaction. Kept as a local name because
    several browser-tool log sites reference it; the redaction policy itself
    lives once in ``redact.py`` so the browser tool and the CDP supervisor
    cannot drift apart.
    """
    return redact_cdp_url(value)

def _get_command_timeout() -> int:
    """Return the configured browser command timeout from config.yaml.

    Reads ``config["browser"]["command_timeout"]`` and falls back to
    ``DEFAULT_COMMAND_TIMEOUT`` (30s) if unset or unreadable.  Result is
    cached after the first call and cleared by ``cleanup_all_browsers()``.
    """
    global _cached_command_timeout, _command_timeout_resolved
    if _command_timeout_resolved and _cached_command_timeout is not None:
        return _cached_command_timeout
    result = DEFAULT_COMMAND_TIMEOUT
    try:
        from hermes_cli.config import read_raw_config
        cfg = read_raw_config()
        val = cfg_get(cfg, 'browser', 'command_timeout')
        if val is not None:
            result = max(int(val), 5)
    except Exception as e:
        logger.debug('Could not read command_timeout from config: %s', e)
    _cached_command_timeout = result
    _command_timeout_resolved = True
    return result

def _safe_command_timeout() -> int:
    """Like ``_get_command_timeout`` but guaranteed non-None.

    Defense in depth against the race fixed in ``_get_command_timeout``:
    if anything ever returns ``None`` (e.g. cache reset mid-flight), fall
    back to ``DEFAULT_COMMAND_TIMEOUT``. Uses ``is not None`` rather than
    ``or`` so a legitimately configured ``0`` is preserved.
    """
    val = _get_command_timeout()
    return val if val is not None else DEFAULT_COMMAND_TIMEOUT

def _get_open_command_timeout(*, first_open: bool=False) -> int:
    """Timeout for agent-browser ``open`` (navigation / daemon cold start)."""
    base = _safe_command_timeout()
    floor = MIN_FIRST_OPEN_TIMEOUT if first_open else MIN_OPEN_TIMEOUT
    return max(base, floor)

def _needs_chromium_sandbox_bypass() -> bool:
    """Return True when Chromium needs --no-sandbox to start reliably."""
    if hasattr(os, 'geteuid') and os.geteuid() == 0:
        return True
    if _running_in_docker():
        return True
    userns_restrict = '/proc/sys/kernel/apparmor_restrict_unprivileged_userns'
    try:
        with open(userns_restrict, encoding='utf-8') as f:
            if f.read().strip() == '1':
                return True
    except OSError:
        pass
    return False

def _read_command_output_files(stdout_path: str, stderr_path: str) -> tuple[str, str]:
    """Best-effort read of agent-browser stdout/stderr temp files."""
    stdout = stderr = ''
    for path, slot in ((stdout_path, 'stdout'), (stderr_path, 'stderr')):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                text = f.read().strip()
        except OSError:
            continue
        if slot == 'stdout':
            stdout = text
        else:
            stderr = text
    return (stdout, stderr)

def _unlink_command_output_files(*paths: str) -> None:
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass

def _format_browser_timeout_error(command: str, timeout: int, stdout: str, stderr: str) -> str:
    """Build an actionable timeout message from captured daemon output."""
    parts = [f'Command timed out after {timeout} seconds']
    detail = (stderr or stdout or '').strip()
    if detail:
        parts.append(detail[:1500])
    combined = f'{stderr}\n{stdout}'.lower()
    hints: list[str] = []
    if 'sandbox' in combined:
        hints.append("Chromium sandbox launch failed. Set AGENT_BROWSER_ARGS='--no-sandbox,--disable-dev-shm-usage' in your environment, or run: npx agent-browser install --with-deps")
    elif command == 'open' and _is_local_mode():
        if _running_in_docker():
            hints.append('The browser daemon may still be starting or Chromium may be missing. Pull the latest image: docker pull ghcr.io/nousresearch/duck-agent:latest')
        else:
            hints.append('The browser daemon may still be starting, or Chromium may be missing system libraries. Install/repair with: npx agent-browser install --with-deps (or: npx playwright install --with-deps chromium)')
    if hints:
        parts.extend(hints)
    return '\n'.join(parts)

def _get_vision_model() -> Optional[str]:
    """Model for browser_vision (screenshot analysis — multimodal)."""
    return os.getenv('AUXILIARY_VISION_MODEL', '').strip() or None

def _get_extraction_model() -> Optional[str]:
    """Model for page snapshot text summarization — same as web_extract."""
    return os.getenv('AUXILIARY_WEB_EXTRACT_MODEL', '').strip() or None

def _resolve_cdp_override(cdp_url: str) -> str:
    """Normalize a user-supplied CDP endpoint into a concrete connectable URL.

    Accepts:
    - full websocket endpoints: ws://host:port/devtools/browser/...
    - HTTP discovery endpoints: http://host:port or http://host:port/json/version
    - bare websocket host:port values like ws://host:port

    For discovery-style endpoints we fetch /json/version and return the
    webSocketDebuggerUrl so downstream tools always receive a concrete browser
    websocket instead of an ambiguous host:port URL.
    """
    raw = (cdp_url or '').strip()
    if not raw:
        return ''
    lowered = raw.lower()
    if '/devtools/browser/' in lowered:
        return raw
    discovery_url = raw
    if lowered.startswith(('ws://', 'wss://')):
        if raw.count(':') == 2 and raw.rstrip('/').rsplit(':', 1)[-1].isdigit() and ('/' not in raw.split(':', 2)[-1]):
            discovery_url = ('http://' if lowered.startswith('ws://') else 'https://') + raw.split('://', 1)[1]
        else:
            return raw
    if discovery_url.lower().endswith('/json/version'):
        version_url = discovery_url
    else:
        version_url = discovery_url.rstrip('/') + '/json/version'
    try:
        import requests
        response = requests.get(version_url, timeout=10)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        logger.warning('Failed to resolve CDP endpoint %s via %s: %s', _sanitize_url_for_logs(raw), _sanitize_url_for_logs(version_url), _sanitize_url_for_logs(exc))
        return raw
    ws_url = str(payload.get('webSocketDebuggerUrl') or '').strip()
    if ws_url:
        logger.info('Resolved CDP endpoint %s -> %s', _sanitize_url_for_logs(raw), _sanitize_url_for_logs(ws_url))
        return ws_url
    logger.warning('CDP discovery at %s did not return webSocketDebuggerUrl; using raw endpoint', _sanitize_url_for_logs(version_url))
    return raw

def _get_cdp_override_raw() -> str:
    """Return the *configured* CDP override without any network I/O.

    Precedence is:
    1. ``BROWSER_CDP_URL`` env var (live override from ``/browser connect``)
    2. ``browser.cdp_url`` in config.yaml (persistent config)

    This is the availability-check variant: callers that only need to know
    *whether* a CDP override is configured (tool ``check_fn`` gates,
    ``_is_local_mode`` / ``_is_local_backend`` routing decisions,
    ``duck-agent doctor``) MUST use this instead of :func:`_get_cdp_override`.

    Rationale: ``_get_cdp_override`` resolves the endpoint over HTTP
    (``/json/version`` discovery, 10s timeout). Tool-schema assembly runs at
    every CLI/Desktop startup and probes several browser-family check_fns;
    when a *stale* ``browser.cdp_url`` points at a dead endpoint (the debug
    Chrome it referenced is long gone), each check blocked on a failing
    socket connect and startup stalled for 10+ seconds before the banner —
    with no error, just mystery slowness. Same principle as the existing
    "do not execute ``agent-browser --version`` here" rule in
    ``check_browser_requirements``: no side effects during schema build.
    """
    env_override = os.environ.get('BROWSER_CDP_URL', '').strip()
    if env_override:
        return env_override
    try:
        from hermes_cli.config import read_raw_config
        cfg = read_raw_config()
        browser_cfg = cfg.get('browser', {})
        if isinstance(browser_cfg, dict):
            return str(browser_cfg.get('cdp_url', '') or '').strip()
    except Exception as e:
        logger.debug('Could not read browser.cdp_url from config: %s', e)
    return ''

def _get_cdp_override() -> str:
    """Return a normalized CDP URL override, or empty string.

    Precedence is:
    1. ``BROWSER_CDP_URL`` env var (live override from ``/browser connect``)
    2. ``browser.cdp_url`` in config.yaml (persistent config)

    When either is set, we skip both Browserbase and the local headless
    launcher and connect directly to the supplied Chrome DevTools Protocol
    endpoint.

    NOTE: resolution may perform an HTTP ``/json/version`` discovery request.
    Only call this on paths that are about to *connect* (session creation,
    supervisor attach). Pure is-it-configured gates must use
    :func:`_get_cdp_override_raw`.
    """
    raw = _get_cdp_override_raw()
    if not raw:
        return ''
    return _resolve_cdp_override(raw)

def _get_dialog_policy_config() -> Tuple[str, float]:
    """Read ``browser.dialog_policy`` + ``browser.dialog_timeout_s`` from config.

    Returns a ``(policy, timeout_s)`` tuple, falling back to the supervisor's
    defaults when keys are absent or invalid.
    """
    from tools.browser_supervisor import DEFAULT_DIALOG_POLICY, DEFAULT_DIALOG_TIMEOUT_S, _VALID_POLICIES
    try:
        from hermes_cli.config import read_raw_config
        cfg = read_raw_config()
        browser_cfg = cfg.get('browser', {}) if isinstance(cfg, dict) else {}
        if not isinstance(browser_cfg, dict):
            return (DEFAULT_DIALOG_POLICY, DEFAULT_DIALOG_TIMEOUT_S)
        policy = str(browser_cfg.get('dialog_policy') or DEFAULT_DIALOG_POLICY)
        if policy not in _VALID_POLICIES:
            logger.debug('Invalid browser.dialog_policy=%r; using default', policy)
            policy = DEFAULT_DIALOG_POLICY
        timeout_raw = browser_cfg.get('dialog_timeout_s')
        try:
            timeout_s = float(timeout_raw) if timeout_raw is not None else DEFAULT_DIALOG_TIMEOUT_S
            if timeout_s <= 0:
                timeout_s = DEFAULT_DIALOG_TIMEOUT_S
        except (TypeError, ValueError):
            timeout_s = DEFAULT_DIALOG_TIMEOUT_S
        return (policy, timeout_s)
    except Exception:
        return (DEFAULT_DIALOG_POLICY, DEFAULT_DIALOG_TIMEOUT_S)

def _ensure_cdp_supervisor(task_id: str) -> None:
    """Start a CDP supervisor for ``task_id`` if an endpoint is reachable.

    Idempotent — delegates to ``SupervisorRegistry.get_or_start`` which skips
    when a supervisor for this ``(task_id, cdp_url)`` already exists and
    tears down + restarts on URL change. Safe to call on every
    ``browser_navigate`` / ``/browser connect`` without worrying about
    double-attach.

    Resolves the CDP URL in this order:
      1. ``BROWSER_CDP_URL`` / ``browser.cdp_url`` — covers ``/browser connect``
         and config-set overrides.
      2. ``_active_sessions[task_id]["cdp_url"]`` — covers Browserbase + any
         other cloud provider whose ``create_session`` returns a raw CDP URL.

    Swallows all errors — failing to attach the supervisor must not break
    the browser session itself.  The agent simply won't see
    ``pending_dialogs`` / ``frame_tree`` fields in snapshots.
    """
    cdp_url = _get_cdp_override()
    if not cdp_url:
        with _cleanup_lock:
            session_info = _active_sessions.get(task_id, {})
        maybe = str(session_info.get('cdp_url') or '')
        if maybe:
            cdp_url = _resolve_cdp_override(maybe)
    if not cdp_url:
        return
    try:
        from tools.browser_supervisor import SUPERVISOR_REGISTRY
        policy, timeout_s = _get_dialog_policy_config()
        SUPERVISOR_REGISTRY.get_or_start(task_id=task_id, cdp_url=cdp_url, dialog_policy=policy, dialog_timeout_s=timeout_s)
    except Exception as exc:
        logger.debug('CDP supervisor attach for task=%s failed (non-fatal): %s', task_id, exc)

def _stop_cdp_supervisor(task_id: str) -> None:
    """Stop the CDP supervisor for ``task_id`` if one exists. No-op otherwise."""
    try:
        from tools.browser_supervisor import SUPERVISOR_REGISTRY
        SUPERVISOR_REGISTRY.stop(task_id)
    except Exception as exc:
        logger.debug('CDP supervisor stop for task=%s failed (non-fatal): %s', task_id, exc)
_PROVIDER_REGISTRY: Dict[str, type] = {'browserbase': BrowserbaseProvider, 'browser-use': BrowserUseProvider, 'firecrawl': FirecrawlProvider}
_DEFAULT_PROVIDER_REGISTRY: Dict[str, type] = dict(_PROVIDER_REGISTRY)
_cached_cloud_provider: Optional[CloudBrowserProvider] = None
_cloud_provider_resolved = False
_allow_private_urls_resolved = False
_cached_allow_private_urls: Optional[bool] = None
_cached_agent_browser: Optional[str] = None
_agent_browser_resolved = False
_cached_browser_engine: Optional[str] = None
_browser_engine_resolved = False

def _is_legacy_provider_registry_overridden() -> bool:
    """Return True when a test has patched ``_PROVIDER_REGISTRY`` to a custom value.

    Detected by spotting any registered class that *isn't* the canonical
    plugin-backed class for that name. Tests that
    ``monkeypatch.setattr(browser_tool, "_PROVIDER_REGISTRY", ...)`` install
    custom factories (`exploding_factory`, `lambda: fake_provider`, etc.);
    those entries fail the canonical-class identity check below.

    Note: a future maintainer adding a 4th built-in provider only needs to
    extend ``_DEFAULT_PROVIDER_REGISTRY`` below — they do NOT need to update
    a hardcoded set of keys here. The detection just compares each registered
    value against the corresponding canonical class.
    """
    try:
        for key, default_cls in _DEFAULT_PROVIDER_REGISTRY.items():
            if _PROVIDER_REGISTRY.get(key) is not default_cls:
                return True
        return len(_PROVIDER_REGISTRY) != len(_DEFAULT_PROVIDER_REGISTRY)
    except Exception:
        return False

def _ensure_browser_plugins_loaded() -> None:
    """Idempotently trigger plugin discovery so the browser registry is populated.

    Normally `model_tools` is imported early in any session and that
    triggers `discover_plugins()` as a side effect. But `_get_cloud_provider`
    can be called from contexts that haven't gone through `model_tools` —
    standalone scripts, certain unit-test paths, the parity-sweep harness.
    Make discovery idempotent and side-effect-only here so users always
    see registered plugins regardless of import order. Cheap: subsequent
    calls early-return inside `_ensure_plugins_discovered`.
    """
    try:
        from hermes_cli.plugins import _ensure_plugins_discovered
        _ensure_plugins_discovered()
    except Exception as exc:
        logger.debug('Browser plugin discovery failed (non-fatal): %s', exc)

def _get_cloud_provider() -> Optional[CloudBrowserProvider]:
    """Return the configured cloud browser provider, or None for local mode.

    Reads ``config["browser"]["cloud_provider"]`` once and caches the result
    for the process lifetime. An explicit ``local`` provider disables cloud
    fallback. If unset, fall back to Browser Use (managed Nous gateway or
    direct API key) and then Browserbase (direct credentials only) — the
    historic auto-detect order, now expressed as the
    :data:`agent.browser_registry._LEGACY_PREFERENCE` walk.

    Selection routes through :mod:`agent.browser_registry` so third-party
    browser plugins (``~/.duck-agent/plugins/browser/<vendor>/``) participate
    in explicit-config resolution. Test fixtures that override
    ``_PROVIDER_REGISTRY`` or ``BrowserUseProvider`` / ``BrowserbaseProvider``
    on this module still drive the function — see
    ``_is_legacy_provider_registry_overridden``.
    """
    global _cached_cloud_provider, _cloud_provider_resolved
    if _cloud_provider_resolved:
        return _cached_cloud_provider
    resolved: Optional[CloudBrowserProvider] = None
    try:
        from hermes_cli.config import read_raw_config
        cfg = read_raw_config()
        browser_cfg = cfg.get('browser', {})
        provider_key = None
        if isinstance(browser_cfg, dict) and 'cloud_provider' in browser_cfg:
            provider_key = normalize_browser_cloud_provider(browser_cfg.get('cloud_provider'))
            if provider_key == 'local':
                _cached_cloud_provider = None
                _cloud_provider_resolved = True
                return None
        if provider_key:
            try:
                if _is_legacy_provider_registry_overridden():
                    factory = _PROVIDER_REGISTRY.get(provider_key)
                    if factory is not None:
                        resolved = factory()
                else:
                    _ensure_browser_plugins_loaded()
                    resolved = _registry_get_browser_provider(provider_key)
                    if resolved is None:
                        logger.warning('browser.cloud_provider=%r is not a registered browser plugin; falling back to auto-detect (install the corresponding plugin or fix the config key spelling).', provider_key)
            except Exception:
                logger.warning('Failed to instantiate explicit cloud_provider %r; will retry on next call', provider_key, exc_info=True)
                return None
    except Exception as e:
        logger.debug('Could not read cloud_provider from config: %s', e)
    if resolved is None:
        try:
            fallback_provider = BrowserUseProvider()
            if fallback_provider.is_configured():
                resolved = fallback_provider
            else:
                fallback_provider = BrowserbaseProvider()
                if fallback_provider.is_configured():
                    resolved = fallback_provider
        except Exception:
            logger.debug('Cloud provider auto-detect failed', exc_info=True)
            return None
    if resolved is None:
        return None
    _cached_cloud_provider = resolved
    _cloud_provider_resolved = True
    return _cached_cloud_provider
from hermes_constants import is_termux as _is_termux_environment

def _browser_install_hint() -> str:
    if _is_termux_environment():
        return 'npm install -g agent-browser && agent-browser install'
    return 'npm install -g agent-browser && agent-browser install --with-deps'

def _requires_real_termux_browser_install(browser_cmd: str) -> bool:
    return _is_termux_environment() and _is_local_mode() and (browser_cmd.strip() == 'npx agent-browser')

def _termux_browser_install_error() -> str:
    return f'Local browser automation on Termux cannot rely on the bare npx fallback. Install agent-browser explicitly first: {_browser_install_hint()}'

def _is_local_mode() -> bool:
    """Return True when the browser tool will use a local browser backend."""
    if _get_cdp_override_raw():
        return False
    return _get_cloud_provider() is None

def _is_local_backend() -> bool:
    """Return True when the browser runs locally AND the terminal is also local.

    SSRF protection is only meaningful for cloud backends (Browserbase,
    BrowserUse) where the agent could reach internal resources on a remote
    machine.  For local backends — Camofox, or the built-in headless
    Chromium without a cloud provider — the user already has full terminal
    and network access on the same machine, so the check adds no security
    value.

    However, when the terminal runs in a container (docker, modal, daytona,
    ssh, singularity), the browser on the host can access internal networks
    that the terminal cannot.  In this case, SSRF protection should be
    enabled even though the browser is technically "local".
    """
    if _get_cdp_override_raw():
        return False
    if _is_camofox_mode():
        return True
    if _get_cloud_provider() is not None:
        return False
    terminal_backend = os.getenv('TERMINAL_ENV', 'local').strip().lower()
    return terminal_backend in ('local', '')
_auto_local_for_private_urls_resolved = False
_cached_auto_local_for_private_urls: bool = True

def _get_browser_engine() -> str:
    """Return the configured browser engine (``auto``, ``lightpanda``, or ``chrome``).

    Reads ``config["browser"]["engine"]`` once and caches the result.
    Falls back to the ``AGENT_BROWSER_ENGINE`` env var, then ``auto``.

    ``auto`` means: don't pass ``--engine`` at all (agent-browser defaults to
    Chrome).  ``lightpanda`` or ``chrome`` are forwarded as
    ``--engine <value>`` to agent-browser v0.25.3+.

    Lightpanda is 1.3-5.8x faster on navigation but has no graphical
    renderer (no screenshots).
    """
    global _cached_browser_engine, _browser_engine_resolved
    if _browser_engine_resolved:
        return _cached_browser_engine
    _browser_engine_resolved = True
    _cached_browser_engine = 'auto'
    try:
        from hermes_cli.config import read_raw_config
        cfg = read_raw_config()
        val = cfg.get('browser', {}).get('engine')
        if val and str(val).strip():
            _cached_browser_engine = str(val).strip().lower()
    except Exception as e:
        logger.debug('Could not read browser.engine from config: %s', e)
    if _cached_browser_engine == 'auto':
        env_val = os.environ.get('AGENT_BROWSER_ENGINE', '').strip().lower()
        if env_val:
            _cached_browser_engine = env_val
    _VALID_ENGINES = {'auto', 'lightpanda', 'chrome'}
    if _cached_browser_engine not in _VALID_ENGINES:
        logger.warning("Unknown browser engine %r (valid: %s), falling back to 'auto'", _cached_browser_engine, ', '.join(sorted(_VALID_ENGINES)))
        _cached_browser_engine = 'auto'
    return _cached_browser_engine
_cached_headed_mode: Optional[bool] = None
_headed_mode_resolved = False

def _is_headed_mode() -> bool:
    """Return True when the browser should launch in headed (visible) mode.

    Reads ``config["browser"]["headed"]`` with ``AGENT_BROWSER_HEADED`` env
    var as fallback.  Result is cached after the first call.
    """
    global _cached_headed_mode, _headed_mode_resolved
    if _headed_mode_resolved:
        return _cached_headed_mode
    _headed_mode_resolved = True
    _cached_headed_mode = False
    try:
        from hermes_cli.config import read_raw_config
        cfg = read_raw_config()
        val = cfg.get('browser', {}).get('headed')
        if val is not None:
            _cached_headed_mode = str(val).strip().lower() in ('true', '1', 'yes')
    except Exception as e:
        logger.debug('Could not read browser.headed from config: %s', e)
    if not _cached_headed_mode:
        env_val = os.environ.get('AGENT_BROWSER_HEADED', '').strip()
        if env_val and env_val.lower() in ('true', '1', 'yes'):
            _cached_headed_mode = True
    return _cached_headed_mode

def _should_inject_engine(engine: str) -> bool:
    """Return True when the engine flag should be added to agent-browser commands.

    Only inject ``--engine`` for non-cloud, non-camofox local sessions where
    the engine is explicitly set (not ``auto``).
    """
    if engine == 'auto':
        return False
    if _is_camofox_mode():
        return False
    return _is_local_mode()

def _using_lightpanda_engine() -> bool:
    """Return True when local browser commands are configured for Lightpanda."""
    return _get_browser_engine() == 'lightpanda'

def _lightpanda_fallback_reason(engine: str, command: str, result: Dict[str, Any]) -> Optional[str]:
    """Return the user-visible reason a Lightpanda result needs Chrome fallback.

    ``None`` means no fallback should run.  The returned string is copied into
    the fallback result so CLI/TUI/gateway users can see when Duck Agent silently
    switched from Lightpanda to Chrome for completeness.
    """
    if engine != 'lightpanda':
        return None
    _FALLBACK_ELIGIBLE = {'open', 'snapshot', 'screenshot', 'eval', 'click', 'fill', 'scroll', 'back', 'press', 'console', 'errors'}
    if command not in _FALLBACK_ELIGIBLE:
        return None
    if not result.get('success'):
        error = str(result.get('error') or 'command failed').strip()
        return f'Lightpanda {command!r} failed ({error}); retried with Chrome.'
    data = result.get('data', {})
    if command == 'snapshot':
        snap = data.get('snapshot', '')
        if not snap or len(snap.strip()) < 20:
            return 'Lightpanda returned an empty/too-short snapshot; retried with Chrome.'
    if command == 'screenshot':
        path = data.get('path', '')
        if path:
            try:
                size = os.path.getsize(path)
                if size < 20480:
                    logger.debug('Lightpanda screenshot is suspiciously small (%d bytes), triggering Chrome fallback', size)
                    return f'Lightpanda screenshot was suspiciously small ({size} bytes); retried with Chrome.'
            except OSError:
                return 'Lightpanda screenshot file was missing/unreadable; retried with Chrome.'
    return None

def _needs_lightpanda_fallback(engine: str, command: str, result: Dict[str, Any]) -> bool:
    """Check if a Lightpanda result should trigger an automatic Chrome fallback."""
    return _lightpanda_fallback_reason(engine, command, result) is not None

def _annotate_lightpanda_fallback(result: Dict[str, Any], reason: str) -> Dict[str, Any]:
    """Add a user-visible Chrome fallback warning to a browser command result."""
    warning = f'⚠ Lightpanda fallback: Chrome was used for this browser action. {reason}'
    annotated = dict(result)
    annotated['fallback_warning'] = warning
    annotated['browser_engine'] = 'chrome'
    annotated['browser_engine_fallback'] = {'from': 'lightpanda', 'to': 'chrome', 'reason': reason}
    data = annotated.get('data')
    if isinstance(data, dict):
        data = dict(data)
        data.setdefault('fallback_warning', warning)
        data.setdefault('browser_engine', 'chrome')
        data.setdefault('browser_engine_fallback', {'from': 'lightpanda', 'to': 'chrome', 'reason': reason})
        annotated['data'] = data
    return annotated

def _copy_fallback_warning(target: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    """Copy browser fallback metadata from an internal result into a tool response."""
    if result.get('fallback_warning'):
        target['fallback_warning'] = result['fallback_warning']
        target['browser_engine'] = result.get('browser_engine')
        target['browser_engine_fallback'] = result.get('browser_engine_fallback')
    return target

def _run_chrome_fallback_command(task_id: str, command: str, args: List[str], timeout: int) -> Dict[str, Any]:
    """Run a browser command in a temporary Chrome session at the current URL.

    agent-browser locks the engine when a named daemon starts. Passing
    ``--engine chrome`` to the same Lightpanda ``--session`` cannot change that
    running daemon. This helper always uses a fresh temporary Chrome session,
    navigates it to the current Lightpanda URL, runs ``command``, then tears it
    down.
    """
    import uuid
    url_result = _run_browser_command(task_id, 'eval', ['window.location.href'], timeout=10, _engine_override='auto')
    current_url = None
    if url_result.get('success'):
        current_url = url_result.get('data', {}).get('result', '').strip().strip('"').strip("'")
    if not current_url:
        logger.warning('Chrome fallback: could not determine current URL from LP session')
        return {'success': False, 'error': 'Chrome fallback failed: could not determine current URL'}
    tmp_session = f'h_cfb_{uuid.uuid4().hex[:8]}'
    try:
        browser_cmd = _find_agent_browser()
    except FileNotFoundError as e:
        return {'success': False, 'error': str(e)}
    if not _chromium_installed():
        if _running_in_docker():
            hint = "Chrome fallback requires Chromium, but it is missing. You're running in Docker — pull the latest image: docker pull ghcr.io/nousresearch/duck-agent:latest"
        else:
            hint = 'Chrome fallback requires Chromium, but it is missing. Install it with: npx agent-browser install --with-deps (or: npx playwright install --with-deps chromium)'
        return {'success': False, 'error': hint}
    if browser_cmd == 'npx agent-browser':
        _npx_bin = shutil.which('npx') or 'npx'
        cmd_prefix = [_npx_bin, 'agent-browser']
    else:
        cmd_prefix = [browser_cmd]
    base_args = cmd_prefix + ['--engine', 'chrome', '--session', tmp_session, '--json']
    task_socket_dir = os.path.join(_socket_safe_tmpdir(), f'agent-browser-{tmp_session}')
    os.makedirs(task_socket_dir, mode=448, exist_ok=True)
    browser_env = _build_browser_env()
    browser_env['AGENT_BROWSER_SOCKET_DIR'] = task_socket_dir
    browser_env['PATH'] = _merge_browser_path(browser_env.get('PATH', ''))
    if 'AGENT_BROWSER_IDLE_TIMEOUT_MS' not in browser_env:
        browser_env['AGENT_BROWSER_IDLE_TIMEOUT_MS'] = str(BROWSER_SESSION_INACTIVITY_TIMEOUT * 1000)

    def _run_tmp(cmd: str, cmd_args: List[str]) -> Dict[str, Any]:
        full = base_args + [cmd] + cmd_args
        stdout_path = os.path.join(task_socket_dir, f'_stdout_{cmd}')
        stderr_path = os.path.join(task_socket_dir, f'_stderr_{cmd}')
        stdout_fd = os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 384)
        stderr_fd = os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 384)
        try:
            _popen_extra: dict = {}
            if os.name == 'nt':
                _popen_extra['creationflags'] = windows_hide_flags()
                _popen_extra['close_fds'] = True
                _si = subprocess.STARTUPINFO()
                _si.dwFlags |= subprocess.STARTF_USESTDHANDLES
                _popen_extra['startupinfo'] = _si
            proc = subprocess.Popen(full, stdout=stdout_fd, stderr=stderr_fd, stdin=subprocess.DEVNULL, env=browser_env, **_popen_extra)
        finally:
            os.close(stdout_fd)
            os.close(stderr_fd)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return {'success': False, 'error': f"Chrome fallback '{cmd}' timed out"}
        try:
            with open(stdout_path, 'r', encoding='utf-8') as f:
                stdout = f.read().strip()
            if stdout:
                return json.loads(stdout.split('\n')[-1])
        except Exception as exc:
            logger.debug("Chrome fallback tmp cmd '%s' error: %s", cmd, exc)
        finally:
            for pth in (stdout_path, stderr_path):
                try:
                    os.unlink(pth)
                except OSError:
                    pass
        return {'success': False, 'error': f"Chrome fallback '{cmd}' failed"}
    try:
        nav = _run_tmp('open', [current_url])
        if not nav.get('success'):
            logger.warning('Chrome fallback: navigate failed: %s', nav.get('error'))
            return {'success': False, 'error': f"Chrome fallback navigate failed: {nav.get('error')}"}
        return _run_tmp(command, args)
    finally:
        try:
            _run_tmp('close', [])
        except Exception:
            pass
        import shutil as _shutil
        _shutil.rmtree(task_socket_dir, ignore_errors=True)

def _chrome_fallback_screenshot(task_id: str, args: List[str], timeout: int) -> Dict[str, Any]:
    """Take a screenshot using a temporary Chrome session."""
    return _run_chrome_fallback_command(task_id, 'screenshot', args, timeout)

def _auto_local_for_private_urls() -> bool:
    """Return whether a cloud-configured install should auto-spawn a local
    Chromium for LAN/localhost URLs.

    Reads ``browser.auto_local_for_private_urls`` once (default ``True``) and
    caches it for the process lifetime.  When enabled, ``browser_navigate``
    routes URLs whose host resolves to a private/loopback/LAN address to a
    local headless Chromium sidecar even when a cloud provider (Browserbase
    / Browser-Use / Firecrawl) is configured globally.  Public URLs continue
    to use the cloud provider in the same conversation.
    """
    global _auto_local_for_private_urls_resolved, _cached_auto_local_for_private_urls
    if _auto_local_for_private_urls_resolved:
        return _cached_auto_local_for_private_urls
    _auto_local_for_private_urls_resolved = True
    try:
        from hermes_cli.config import read_raw_config
        cfg = read_raw_config()
        browser_cfg = cfg.get('browser', {})
        if isinstance(browser_cfg, dict) and 'auto_local_for_private_urls' in browser_cfg:
            _cached_auto_local_for_private_urls = bool(browser_cfg.get('auto_local_for_private_urls'))
    except Exception as e:
        logger.debug('Could not read auto_local_for_private_urls from config: %s', e)
    return _cached_auto_local_for_private_urls

def _url_is_private(url: str) -> bool:
    """Return True when the URL's host resolves to a private/LAN/loopback address.

    Reuses ``tools.url_safety.is_safe_url`` as the oracle — if the SSRF check
    would reject the URL, we treat it as "private" for routing purposes.  DNS
    resolution failures are treated as NOT private (fall through to whatever
    backend is configured, which will surface the DNS error naturally).
    """
    try:
        from urllib.parse import urlparse
        import ipaddress
        import socket
        parsed = urlparse(url)
        hostname = (parsed.hostname or '').strip().lower().rstrip('.')
        if not hostname:
            return False
        try:
            ip = ipaddress.ip_address(hostname)
            return ip.is_private or ip.is_loopback or ip.is_link_local or (ip in ipaddress.ip_network('172.16.0.0/12')) or (ip in ipaddress.ip_network('100.64.0.0/10'))
        except ValueError:
            pass
        if hostname in {'localhost'} or hostname.endswith('.localhost'):
            return True
        if hostname.endswith('.local') or hostname.endswith('.lan') or hostname.endswith('.internal'):
            return True
        try:
            addr_info = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            return False
        for _, _, _, _, sockaddr in addr_info:
            try:
                ip = ipaddress.ip_address(sockaddr[0])
            except ValueError:
                continue
            if ip.is_private or ip.is_loopback or ip.is_link_local or (ip in ipaddress.ip_network('100.64.0.0/10')):
                return True
        return False
    except Exception as exc:
        logger.debug('URL-privacy check failed for %s: %s', url, exc)
        return False

def _navigation_session_key(task_id: str, url: str) -> str:
    """Pick the session key that should handle ``url`` for ``task_id``.

    Returns the bare task_id unless ALL of these are true:
      1. A cloud provider is configured (``_get_cloud_provider()`` is not None).
      2. Auto-local routing is enabled (``browser.auto_local_for_private_urls``,
         default True).
      3. The URL resolves to a private/LAN/loopback address.
      4. A CDP override is not active (that path owns the whole session).
      5. Camofox mode is not active (Camofox is already local-only).

    When all are true, returns ``f"{task_id}::local"`` so the hybrid-routing
    path spawns a local Chromium sidecar while the cloud session (if any)
    continues to serve public URLs.
    """
    if task_id is None:
        task_id = 'default'
    if _get_cdp_override_raw():
        return task_id
    if _is_camofox_mode():
        return task_id
    if _get_cloud_provider() is None:
        return task_id
    if not _auto_local_for_private_urls():
        return task_id
    if not _url_is_private(url):
        return task_id
    return f'{task_id}{_LOCAL_SUFFIX}'

def _is_local_sidecar_key(session_key: str) -> bool:
    """Return True when ``session_key`` is a hybrid-routing local sidecar."""
    return session_key.endswith(_LOCAL_SUFFIX)

def _bare_task_id_for_session_key(session_key: str) -> str:
    """Return the owning bare task id for an opaque browser session key."""
    if _is_local_sidecar_key(session_key):
        return session_key[:-len(_LOCAL_SUFFIX)]
    return session_key

def _session_info_owned_by_task(session_info: Dict[str, Any], task_id: str, session_key: str) -> bool:
    """Return whether ``session_info`` still belongs to ``task_id``/``session_key``.

    Sessions created by current code carry explicit ownership metadata. Treat
    older in-memory entries without those fields as valid for hot-reload/test
    compatibility, but reject any explicit mismatch before a non-navigation
    tool can act on the wrong tab/session.
    """
    owner = session_info.get('owner_task_id')
    key = session_info.get('session_key')
    if owner is not None and owner != task_id:
        return False
    if key is not None and key != session_key:
        return False
    return True

def _last_session_key(task_id: str) -> str:
    """Return the live session key to use for a non-nav browser tool call.

    ``browser_navigate`` records which concrete session key served a task's
    most recent successful navigation. Non-navigation tools must reuse that key
    so click/fill/snapshot land in the same browser. If the recorded owner was
    later cleaned up or ownership metadata no longer matches, fail closed by
    dropping the stale binding instead of silently recreating or mutating the
    wrong browser.
    """
    if task_id is None:
        task_id = 'default'
    recorded_key = _last_active_session_key.get(task_id)
    if not recorded_key:
        return task_id
    with _cleanup_lock:
        session_info = _active_sessions.get(recorded_key)
        if session_info and _session_info_owned_by_task(session_info, task_id, recorded_key):
            return recorded_key
        _last_active_session_key.pop(task_id, None)
    logger.debug('browser session ownership: dropping stale/mismatched last-active binding %s -> %s', task_id, recorded_key)
    return task_id

def _allow_private_urls() -> bool:
    """Return whether the browser is allowed to navigate to private/internal addresses.

    Reads ``config["browser"]["allow_private_urls"]``. Single-profile calls
    cache the result for the process lifetime; multiplexed profile turns resolve
    their context-local config on each call. Defaults to ``False`` (SSRF
    protection active).
    """
    global _cached_allow_private_urls, _allow_private_urls_resolved
    if get_hermes_home_override() is not None:
        return _resolve_allow_private_urls()
    if _allow_private_urls_resolved:
        return _cached_allow_private_urls
    _allow_private_urls_resolved = True
    _cached_allow_private_urls = _resolve_allow_private_urls()
    return _cached_allow_private_urls

def _resolve_allow_private_urls() -> bool:
    """Read the browser private-URL toggle from the active config scope."""
    try:
        from hermes_cli.config import read_raw_config
        cfg = read_raw_config()
        browser_cfg = cfg.get('browser', {})
        if isinstance(browser_cfg, dict):
            return is_truthy_value(browser_cfg.get('allow_private_urls'), default=False)
    except Exception as e:
        logger.debug('Could not read allow_private_urls from config: %s', e)
    return False

def _socket_safe_tmpdir() -> str:
    """Return a short temp directory path suitable for Unix domain sockets.

    macOS sets ``TMPDIR`` to ``/var/folders/xx/.../T/`` (~51 chars).  When we
    append ``agent-browser-hermes_…`` the resulting socket path exceeds the
    104-byte macOS limit for ``AF_UNIX`` addresses, causing agent-browser to
    fail with "Failed to create socket directory" or silent screenshot failures.

    Linux ``tempfile.gettempdir()`` already returns ``/tmp``, so this is a
    no-op there.  On macOS we bypass ``TMPDIR`` and use ``/tmp`` directly
    (symlink to ``/private/tmp``, sticky-bit protected, always available).
    """
    if sys.platform == 'darwin':
        return '/tmp'
    return tempfile.gettempdir()
_active_sessions: Dict[str, Dict[str, Any]] = {}
_recording_sessions: set = set()
_last_active_session_key: Dict[str, str] = {}
_LOCAL_SUFFIX = '::local'
_cleanup_done = False
DEFAULT_SESSION_INACTIVITY_TIMEOUT = int(DEFAULT_CONFIG.get('browser', {}).get('inactivity_timeout', 120))

def _get_session_inactivity_timeout() -> int:
    result = env_int('BROWSER_INACTIVITY_TIMEOUT', DEFAULT_SESSION_INACTIVITY_TIMEOUT)
    try:
        from hermes_cli.config import read_raw_config
        cfg = read_raw_config()
        val = cfg_get(cfg, 'browser', 'inactivity_timeout')
        if val is not None:
            result = max(int(val), 30)
    except Exception as e:
        logger.debug('Could not read inactivity_timeout from config: %s', e)
    return result
BROWSER_SESSION_INACTIVITY_TIMEOUT = _get_session_inactivity_timeout()
_session_last_activity: Dict[str, float] = {}
_cleanup_thread = None
_cleanup_running = False
_cleanup_lock = threading.Lock()

def _session_expiry_timestamp(session_info: Dict[str, Any]) -> Optional[float]:
    """Return a provider-authoritative session expiry as epoch seconds.

    Cloud providers may omit ``expires_at``. Unknown or malformed values are
    therefore treated as having no known expiry, preserving the existing
    lifecycle for local browsers and providers without an expiry contract.
    """
    value = session_info.get('expires_at')
    if isinstance(value, (int, float)) and (not isinstance(value, bool)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith(('Z', 'z')):
        normalized = f'{normalized[:-1]}+00:00'
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        logger.warning('Ignoring invalid cloud browser session expiry timestamp')
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()

def _session_has_expired(session_info: Dict[str, Any], *, now: Optional[float]=None) -> bool:
    """Return whether a cached browser session crossed its provider deadline."""
    expires_at = _session_expiry_timestamp(session_info)
    if expires_at is None:
        return False
    return (time.time() if now is None else now) >= expires_at

def _emergency_cleanup_all_sessions():
    """
    Emergency cleanup of all active browser sessions.
    Called on process exit or interrupt to prevent orphaned sessions.

    Also runs the orphan reaper to clean up daemons left behind by previously
    crashed duck-agent processes — this way every clean duck-agent exit sweeps
    accumulated orphans, not just ones that actively used the browser tool.
    """
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True
    if _active_sessions:
        logger.info('Emergency cleanup: closing %s active session(s)...', len(_active_sessions))
        try:
            cleanup_all_browsers()
        except Exception as e:
            logger.error('Emergency cleanup error: %s', e)
        finally:
            with _cleanup_lock:
                _active_sessions.clear()
                _session_last_activity.clear()
                _recording_sessions.clear()
    try:
        _reap_orphaned_browser_sessions()
    except Exception as e:
        logger.debug('Orphan reap on exit failed: %s', e)
atexit.register(_emergency_cleanup_all_sessions)

def _cleanup_inactive_browser_sessions():
    """
    Clean up browser sessions that have been inactive for longer than the timeout.

    This function is called periodically by the background cleanup thread to
    automatically close sessions that haven't been used recently, preventing
    orphaned sessions (local or Browserbase) from accumulating.
    """
    current_time = time.time()
    sessions_to_cleanup = []
    with _cleanup_lock:
        for task_id, last_time in list(_session_last_activity.items()):
            if current_time - last_time > BROWSER_SESSION_INACTIVITY_TIMEOUT:
                sessions_to_cleanup.append(task_id)
    for task_id in sessions_to_cleanup:
        try:
            elapsed = int(current_time - _session_last_activity.get(task_id, current_time))
            logger.info('Cleaning up inactive session for task: %s (inactive for %ss)', task_id, elapsed)
            cleanup_browser(task_id)
            with _cleanup_lock:
                if task_id in _session_last_activity:
                    del _session_last_activity[task_id]
        except Exception as e:
            logger.warning('Error cleaning up inactive session %s: %s', task_id, e)

def _write_owner_pid(socket_dir: str, session_name: str) -> None:
    """Record the current duck-agent PID as the owner of a browser socket dir.

    Written atomically to ``<socket_dir>/<session_name>.owner_pid`` so the
    orphan reaper can distinguish daemons owned by a live duck-agent process
    (don't reap) from daemons whose owner crashed (reap).  Best-effort —
    an OSError here just falls back to the legacy ``tracked_names``
    heuristic in the reaper.
    """
    try:
        path = os.path.join(socket_dir, f'{session_name}.owner_pid')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(str(os.getpid()))
    except OSError as exc:
        logger.debug('Could not write owner_pid file for %s: %s', session_name, exc)

def _verify_reapable_browser_daemon(daemon_pid: int, socket_dir: str, session_name: str) -> bool:
    """Confirm a live PID is genuinely *this* session's agent-browser daemon.

    The orphan reaper scans world-writable, predictably-named temp paths
    (``/tmp/agent-browser-h_*`` etc.) and reads a daemon PID from a ``.pid``
    file we do not write ourselves — the agent-browser daemon writes it.  A
    same-user actor can therefore plant a fake socket dir whose ``.pid`` points
    at an arbitrary victim process, or a recycled PID can land on an unrelated
    process after the real daemon exits.  Either way, terminating that PID
    (a *tree* kill via ``_terminate_host_pid``) is an arbitrary-process DoS.

    Before reaping we require, via ``psutil`` (a hard dependency, cross-platform
    for same-user processes — the only processes the reaper can signal):

      1. **Identity** — the process looks like agent-browser: ``agent-browser``
         appears in its name or command line.
      2. **Binding** — the process is bound to *this* session's socket dir: the
         socket dir path (or its basename) appears in the command line, or in
         ``AGENT_BROWSER_SOCKET_DIR`` in the process environment.

    Requirement (2) is the real spoof defense: a planted process pointing at a
    victim PID will not have the victim's cmdline/environ referencing our
    socket dir.  An attacker would need a process that genuinely embeds this
    exact session path — i.e. a real daemon they already own and could signal
    directly.  Fail-closed: any ambiguity (unreadable cmdline, no match) means
    we refuse to reap and leave the process and its socket dir alone.

    Returns ``True`` only when both checks pass.
    """
    try:
        import psutil
    except ImportError:
        logger.warning('Refusing to reap browser daemon PID %d (session %s): psutil unavailable for identity verification', daemon_pid, session_name)
        return False
    try:
        proc = psutil.Process(daemon_pid)
        name = (proc.name() or '').lower()
        cmdline = ' '.join(proc.cmdline() or []).lower()
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, OSError) as exc:
        logger.warning('Refusing to reap browser daemon PID %d (session %s): could not read process identity (%s)', daemon_pid, session_name, exc)
        return False
    looks_like_browser = 'agent-browser' in name or 'agent-browser' in cmdline
    if not looks_like_browser:
        logger.warning('Refusing to reap PID %d (session %s): not an agent-browser process (name=%r)', daemon_pid, session_name, name)
        return False
    socket_dir_l = socket_dir.lower()
    socket_base_l = os.path.basename(socket_dir).lower()
    bound = socket_dir_l in cmdline or (socket_base_l and socket_base_l in cmdline)
    if not bound:
        try:
            env_dir = (proc.environ() or {}).get('AGENT_BROWSER_SOCKET_DIR', '')
            bound = bool(env_dir) and os.path.normpath(env_dir) == os.path.normpath(socket_dir)
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            bound = False
    if not bound:
        logger.warning('Refusing to reap agent-browser PID %d: not bound to session socket dir %s (possible recycled PID or planted pid file)', daemon_pid, socket_dir)
        return False
    return True

def _reap_orphaned_browser_sessions():
    """Scan for orphaned agent-browser daemon processes from previous runs.

    When the Python process that created a browser session exits uncleanly
    (SIGKILL, crash, gateway restart), the in-memory ``_active_sessions``
    tracking is lost but the node + Chromium processes keep running.

    This function scans the tmp directory for ``agent-browser-*`` socket dirs
    left behind by previous runs, reads the daemon PID files, and kills any
    daemons whose owning duck-agent process is no longer alive.

    Ownership detection priority:
      1. ``<session>.owner_pid`` file (written by current code) — if the
         referenced duck-agent PID is alive, leave the daemon alone regardless
         of whether it's in *this* process's ``_active_sessions``.  This is
         cross-process safe: two concurrent duck-agent instances won't reap each
         other's daemons.
      2. Fallback for daemons that predate owner_pid: check
         ``_active_sessions`` in the current process.  If not tracked here,
         treat as orphan (legacy behavior).

    Safe to call from any context — atexit, cleanup thread, or on demand.
    """
    import glob
    tmpdir = _socket_safe_tmpdir()
    pattern = os.path.join(tmpdir, 'agent-browser-h_*')
    socket_dirs = glob.glob(pattern)
    socket_dirs += glob.glob(os.path.join(tmpdir, 'agent-browser-cdp_*'))
    socket_dirs += glob.glob(os.path.join(tmpdir, 'agent-browser-hermes_*'))
    if not socket_dirs:
        return
    with _cleanup_lock:
        tracked_names = {info.get('session_name') for info in _active_sessions.values() if info.get('session_name')}
    reaped = 0
    for socket_dir in socket_dirs:
        dir_name = os.path.basename(socket_dir)
        session_name = dir_name.removeprefix('agent-browser-')
        if not session_name:
            continue
        owner_pid_file = os.path.join(socket_dir, f'{session_name}.owner_pid')
        owner_alive: Optional[bool] = None
        if os.path.isfile(owner_pid_file):
            try:
                owner_pid = int(Path(owner_pid_file).read_text(encoding='utf-8').strip())
                from gateway.status import _pid_exists
                owner_alive = _pid_exists(owner_pid)
            except (ValueError, OSError):
                owner_alive = None
        if owner_alive is True:
            continue
        if owner_alive is None:
            if session_name in tracked_names:
                continue
        pid_file = os.path.join(socket_dir, f'{session_name}.pid')
        if not os.path.isfile(pid_file):
            shutil.rmtree(socket_dir, ignore_errors=True)
            continue
        try:
            daemon_pid = int(Path(pid_file).read_text(encoding='utf-8').strip())
        except (ValueError, OSError):
            shutil.rmtree(socket_dir, ignore_errors=True)
            continue
        from gateway.status import _pid_exists
        if not _pid_exists(daemon_pid):
            shutil.rmtree(socket_dir, ignore_errors=True)
            continue
        if not _verify_reapable_browser_daemon(daemon_pid, socket_dir, session_name):
            continue
        try:
            from tools.process_registry import ProcessRegistry
            ProcessRegistry._terminate_host_pid(daemon_pid)
            logger.info('Reaped orphaned browser daemon PID %d (session %s)', daemon_pid, session_name)
            reaped += 1
        except (ProcessLookupError, PermissionError, OSError):
            pass
        shutil.rmtree(socket_dir, ignore_errors=True)
    if reaped:
        logger.info('Reaped %d orphaned browser session(s) from previous run(s)', reaped)

def _browser_cleanup_thread_worker():
    """
    Background thread that periodically cleans up inactive browser sessions.

    Runs every 30 seconds and checks for sessions that haven't been used
    within the BROWSER_SESSION_INACTIVITY_TIMEOUT period.
    On first run, also reaps orphaned sessions from previous process lifetimes.
    """
    try:
        _reap_orphaned_browser_sessions()
    except Exception as e:
        logger.warning('Orphan reap error: %s', e)
    while _cleanup_running:
        try:
            _cleanup_inactive_browser_sessions()
        except Exception as e:
            logger.warning('Cleanup thread error: %s', e)
        for _ in range(30):
            if not _cleanup_running:
                break
            time.sleep(1)

def _start_browser_cleanup_thread():
    """Start the background cleanup thread if not already running."""
    global _cleanup_thread, _cleanup_running
    with _cleanup_lock:
        if _cleanup_thread is None or not _cleanup_thread.is_alive():
            _cleanup_running = True
            _cleanup_thread = threading.Thread(target=_browser_cleanup_thread_worker, daemon=True, name='browser-cleanup')
            _cleanup_thread.start()
            logger.info('Started inactivity cleanup thread (timeout: %ss)', BROWSER_SESSION_INACTIVITY_TIMEOUT)

def _stop_browser_cleanup_thread():
    """Stop the background cleanup thread."""
    global _cleanup_running
    _cleanup_running = False
    if _cleanup_thread is not None:
        _cleanup_thread.join(timeout=5)

def _update_session_activity(task_id: str):
    """Update the last activity timestamp for a session."""
    with _cleanup_lock:
        _session_last_activity[task_id] = time.time()
atexit.register(_stop_browser_cleanup_thread)
BROWSER_TOOL_SCHEMAS = [{'name': 'browser_navigate', 'description': 'Navigate to a URL in the browser. Initializes the session and loads the page. Must be called before other browser tools. For simple information retrieval, prefer web_search or web_extract (faster, cheaper). For plain-text endpoints — URLs ending in .md, .txt, .json, .yaml, .yml, .csv, .xml, raw.githubusercontent.com, or any documented API endpoint — prefer curl via the terminal tool or web_extract; the browser stack is overkill and much slower for these. Use browser tools when you need to interact with a page (click, fill forms, dynamic content). Returns a compact page snapshot with interactive elements and ref IDs — no need to call browser_snapshot separately after navigating.', 'parameters': {'type': 'object', 'properties': {'url': {'type': 'string', 'description': "The URL to navigate to (e.g., 'https://example.com')"}}, 'required': ['url']}}, {'name': 'browser_snapshot', 'description': "Get a text-based snapshot of the current page's accessibility tree. Returns interactive elements with ref IDs (like @e1, @e2) for browser_click and browser_type. full=false (default): compact view with interactive elements. full=true: complete page content. Snapshots over 15000 chars are truncated or LLM-summarized; when that happens the complete snapshot is saved to a file and the output includes its path so you can page through the rest with read_file. Requires browser_navigate first. Note: browser_navigate already returns a compact snapshot — use this to refresh after interactions that change the page, or with full=true for complete content.", 'parameters': {'type': 'object', 'properties': {'full': {'type': 'boolean', 'description': 'If true, returns complete page content. If false (default), returns compact view with interactive elements only.', 'default': False}}, 'required': []}}, {'name': 'browser_click', 'description': "Click on an element identified by its ref ID from the snapshot (e.g., '@e5'). The ref IDs are shown in square brackets in the snapshot output. Requires browser_navigate and browser_snapshot to be called first.", 'parameters': {'type': 'object', 'properties': {'ref': {'type': 'string', 'description': "The element reference from the snapshot (e.g., '@e5', '@e12')"}}, 'required': ['ref']}}, {'name': 'browser_type', 'description': 'Type text into an input field identified by its ref ID. Clears the field first, then types the new text. Requires browser_navigate and browser_snapshot to be called first.', 'parameters': {'type': 'object', 'properties': {'ref': {'type': 'string', 'description': "The element reference from the snapshot (e.g., '@e3')"}, 'text': {'type': 'string', 'description': 'The text to type into the field'}}, 'required': ['ref', 'text']}}, {'name': 'browser_scroll', 'description': 'Scroll the page in a direction. Use this to reveal more content that may be below or above the current viewport. Requires browser_navigate to be called first.', 'parameters': {'type': 'object', 'properties': {'direction': {'type': 'string', 'enum': ['up', 'down'], 'description': 'Direction to scroll'}}, 'required': ['direction']}}, {'name': 'browser_back', 'description': 'Navigate back to the previous page in browser history. Requires browser_navigate to be called first.', 'parameters': {'type': 'object', 'properties': {}, 'required': []}}, {'name': 'browser_press', 'description': 'Press a keyboard key. Useful for submitting forms (Enter), navigating (Tab), or keyboard shortcuts. Requires browser_navigate to be called first.', 'parameters': {'type': 'object', 'properties': {'key': {'type': 'string', 'description': "Key to press (e.g., 'Enter', 'Tab', 'Escape', 'ArrowDown')"}}, 'required': ['key']}}, {'name': 'browser_get_images', 'description': 'Get a list of all images on the current page with their URLs and alt text. Useful for finding images to analyze with the vision tool. Requires browser_navigate to be called first.', 'parameters': {'type': 'object', 'properties': {}, 'required': []}}, {'name': 'browser_vision', 'description': 'Take a screenshot of the current page so you can inspect it visually. Use this when you need to understand what the page looks like - especially for CAPTCHAs, visual verification challenges, complex layouts, or cases where the text snapshot misses important visual information. When your active model has native vision, the screenshot is attached to your context directly and you inspect it on the next turn; otherwise Duck Agent falls back to an auxiliary vision model and returns a text analysis. Includes a screenshot_path that you can share with the user by including MEDIA:<screenshot_path> in your response. Requires browser_navigate to be called first.', 'parameters': {'type': 'object', 'properties': {'question': {'type': 'string', 'description': "What you want to know about the page visually. Be specific about what you're looking for."}, 'annotate': {'type': 'boolean', 'default': False, 'description': 'If true, overlay numbered [N] labels on interactive elements. Each [N] maps to ref @eN for subsequent browser commands. Useful for QA and spatial reasoning about page layout.'}}, 'required': ['question']}}, {'name': 'browser_console', 'description': "Get browser console output and JavaScript errors from the current page. Returns console.log/warn/error/info messages and uncaught JS exceptions. Use this to detect silent JavaScript errors, failed API calls, and application warnings. Requires browser_navigate to be called first. When 'expression' is provided, evaluates JavaScript in the page context and returns the result — use this for DOM inspection, reading page state, or extracting data programmatically.", 'parameters': {'type': 'object', 'properties': {'clear': {'type': 'boolean', 'default': False, 'description': 'If true, clear the message buffers after reading'}, 'expression': {'type': 'string', 'description': 'JavaScript expression to evaluate in the page context. Runs in the browser like DevTools console — full access to DOM, window, document. Return values are serialized to JSON. Example: \'document.title\' or \'document.querySelectorAll("a").length\''}}, 'required': []}}]

def _create_local_session(task_id: str) -> Dict[str, str]:
    import uuid
    session_name = f'h_{uuid.uuid4().hex[:10]}'
    logger.info('Created local browser session %s for task %s', session_name, task_id)
    return {'session_name': session_name, 'bb_session_id': None, 'cdp_url': None, 'features': {'local': True}}

def _create_cdp_session(task_id: str, cdp_url: str) -> Dict[str, str]:
    """Create a session that connects to a user-supplied CDP endpoint."""
    import uuid
    session_name = f'cdp_{uuid.uuid4().hex[:10]}'
    logger.info('Created CDP browser session %s → %s for task %s', session_name, _sanitize_url_for_logs(cdp_url), task_id)
    return {'session_name': session_name, 'bb_session_id': None, 'cdp_url': cdp_url, 'features': {'cdp_override': True}}

def _get_session_info(task_id: Optional[str]=None) -> Dict[str, Any]:
    """
    Get or create session info for the given session key.

    In cloud mode, creates a Browserbase session with proxies enabled.
    In local mode, generates a session name for agent-browser --session.
    Also starts the inactivity cleanup thread and updates activity tracking.
    Thread-safe: multiple subagents can call this concurrently.

    Args:
        task_id: Session key.  Normally the task_id as-is, but may carry the
            ``::local`` suffix for the hybrid-routing local sidecar — in that
            case the cloud provider is skipped even when one is configured,
            and a local Chromium session is created instead.

    Returns:
        Dict with session_name (always), bb_session_id + cdp_url (cloud only)
    """
    if task_id is None:
        task_id = 'default'
    _start_browser_cleanup_thread()
    _update_session_activity(task_id)
    with _cleanup_lock:
        existing_session = _active_sessions.get(task_id)
    if existing_session is not None:
        if not _session_has_expired(existing_session):
            return existing_session
        logger.info('Replacing expired cloud browser session for task %s', task_id)
        _cleanup_single_browser_session(task_id)
        _update_session_activity(task_id)
        with _cleanup_lock:
            replacement = _active_sessions.get(task_id)
        if replacement is not None and replacement is not existing_session:
            return replacement
    force_local = _is_local_sidecar_key(task_id)
    cdp_override = _get_cdp_override()
    if cdp_override and (not force_local):
        session_info = _create_cdp_session(task_id, cdp_override)
    elif force_local:
        session_info = _create_local_session(task_id)
    else:
        provider = _get_cloud_provider()
        if provider is None:
            session_info = _create_local_session(task_id)
        else:
            try:
                session_info = provider.create_session(task_id)
                if not session_info or not isinstance(session_info, dict):
                    raise ValueError(f'Cloud provider returned invalid session: {session_info!r}')
                if session_info.get('cdp_url'):
                    session_info = dict(session_info)
                    session_info['cdp_url'] = _resolve_cdp_override(str(session_info['cdp_url']))
            except Exception as e:
                provider_name = type(provider).__name__
                logger.warning('Cloud provider %s failed (%s); attempting fallback to local Chromium for task %s', provider_name, e, task_id, exc_info=True)
                try:
                    session_info = _create_local_session(task_id)
                except Exception as local_error:
                    raise RuntimeError(f'Cloud provider {provider_name} failed ({e}) and local fallback also failed ({local_error})') from e
                if isinstance(session_info, dict):
                    session_info = dict(session_info)
                    session_info['fallback_from_cloud'] = True
                    session_info['fallback_reason'] = str(e)
                    session_info['fallback_provider'] = provider_name
    with _cleanup_lock:
        if task_id in _active_sessions:
            return _active_sessions[task_id]
        session_info = dict(session_info)
        session_info.setdefault('session_key', task_id)
        session_info.setdefault('owner_task_id', _bare_task_id_for_session_key(task_id))
        _active_sessions[task_id] = session_info
    if not force_local:
        _ensure_cdp_supervisor(task_id)
    return session_info

def _agent_browser_candidate_present(path: str | None) -> bool:
    if not path:
        return False
    if ' ' in path and path.split()[0].endswith('npx'):
        return True
    return os.path.exists(path) and (os.name == 'nt' or os.access(path, os.X_OK))

def _find_agent_browser(*, validate: bool=True) -> str:
    """
    Find the agent-browser CLI executable.

    Checks in order: current PATH, Homebrew/common bin dirs, Duck Agent-managed
    node, local node_modules/.bin/, npx fallback.

    Returns:
        Path to agent-browser executable

    Raises:
        FileNotFoundError: If agent-browser is not installed
    """
    global _cached_agent_browser, _agent_browser_resolved
    if _agent_browser_resolved:
        if _cached_agent_browser is None:
            raise FileNotFoundError(f"agent-browser CLI not found (cached). Install it with: {_browser_install_hint()}\nOr run 'npm install' in the repo root to install locally.\nOr ensure npx is available in your PATH.")
        return _cached_agent_browser
    which_result = shutil.which('agent-browser')
    if which_result and (agent_browser_runnable(which_result) if validate else _agent_browser_candidate_present(which_result)):
        if not validate:
            return which_result
        _cached_agent_browser = which_result
        _agent_browser_resolved = True
        return which_result
    extended_path = _merge_browser_path('')
    if extended_path:
        which_result = shutil.which('agent-browser', path=extended_path)
        if which_result and (agent_browser_runnable(which_result) if validate else _agent_browser_candidate_present(which_result)):
            if not validate:
                return which_result
            _cached_agent_browser = which_result
            _agent_browser_resolved = True
            return which_result
    repo_root = Path(__file__).parent.parent
    local_bin_dir = repo_root / 'node_modules' / '.bin'
    if local_bin_dir.is_dir():
        local_which = shutil.which('agent-browser', path=str(local_bin_dir))
        if local_which and (agent_browser_runnable(local_which) if validate else _agent_browser_candidate_present(local_which)):
            if not validate:
                return local_which
            _cached_agent_browser = local_which
            _agent_browser_resolved = True
            return _cached_agent_browser
    npx_path = shutil.which('npx')
    if not npx_path and extended_path:
        npx_path = shutil.which('npx', path=extended_path)
    if npx_path:
        if not validate:
            return 'npx agent-browser'
        _cached_agent_browser = 'npx agent-browser'
        _agent_browser_resolved = True
        return _cached_agent_browser
    if not validate:
        raise FileNotFoundError('agent-browser CLI not found')
    try:
        from hermes_cli.dep_ensure import ensure_dependency
        if ensure_dependency('browser'):
            candidates = [shutil.which('agent-browser'), shutil.which('agent-browser', path=extended_path) if extended_path else None, shutil.which('agent-browser', path=str(get_hermes_home() / 'node_modules' / '.bin')), shutil.which('agent-browser', path=str(get_hermes_home() / 'node' / 'bin')), shutil.which('agent-browser', path=str(get_hermes_home() / 'node'))]
            for recheck in candidates:
                if recheck and agent_browser_runnable(recheck):
                    _cached_agent_browser = recheck
                    _agent_browser_resolved = True
                    return recheck
    except Exception:
        pass
    _agent_browser_resolved = True
    raise FileNotFoundError(f"agent-browser CLI not found. Install it with: {_browser_install_hint()}\nOr run 'npm install' in the repo root to install locally.\nOr ensure npx is available in your PATH.")

def _extract_screenshot_path_from_text(text: str) -> Optional[str]:
    """Extract a screenshot file path from agent-browser human-readable output."""
    if not text:
        return None
    patterns = ['Screenshot saved to [\'\\"](?P<path>/[^\'\\"]+?\\.png)[\'\\"]', 'Screenshot saved to (?P<path>/\\S+?\\.png)(?:\\s|$)', '(?P<path>/\\S+?\\.png)(?:\\s|$)']
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            path = match.group('path').strip().strip('\'"')
            if path:
                return path
    return None

def _run_browser_command(task_id: str, command: str, args: List[str]=None, timeout: Optional[int]=None, _engine_override: Optional[str]=None) -> Dict[str, Any]:
    """
    Run an agent-browser CLI command using our pre-created Browserbase session.

    Args:
        task_id: Task identifier to get the right session
        command: The command to run (e.g., "open", "click")
        args: Additional arguments for the command
        timeout: Command timeout in seconds.  ``None`` reads
                 ``browser.command_timeout`` from config (default 30s).
        _engine_override: Force a specific engine for this call only.  Used
                          internally by the Lightpanda fallback to retry with
                          Chrome without touching global state.

    Returns:
        Parsed JSON response from agent-browser
    """
    if timeout is None:
        timeout = _safe_command_timeout()
    args = args or []
    try:
        browser_cmd = _find_agent_browser()
    except FileNotFoundError as e:
        logger.warning('agent-browser CLI not found: %s', e)
        return {'success': False, 'error': str(e)}
    if _requires_real_termux_browser_install(browser_cmd):
        error = _termux_browser_install_error()
        logger.warning('browser command blocked on Termux: %s', error)
        return {'success': False, 'error': error}
    if _is_local_mode() and (not _chromium_installed()) and (_get_browser_engine() != 'lightpanda') and (not _maybe_autoinstall_chromium()):
        if _running_in_docker():
            hint = "Chromium browser is missing. You're running in Docker — pull the latest image to get the bundled Chromium: docker pull ghcr.io/nousresearch/duck-agent:latest"
        else:
            hint = 'Chromium browser is missing. Install it with: npx agent-browser install --with-deps (or: npx playwright install --with-deps chromium)'
        logger.warning('browser command blocked: %s', hint)
        return {'success': False, 'error': hint}
    from tools.interrupt import is_interrupted
    if is_interrupted():
        return {'success': False, 'error': 'Interrupted'}
    try:
        session_info = _get_session_info(task_id)
    except Exception as e:
        logger.warning('Failed to create browser session for task=%s: %s', task_id, e)
        return {'success': False, 'error': f'Failed to create browser session: {str(e)}'}
    if session_info.get('cdp_url'):
        backend_args = ['--cdp', session_info['cdp_url']]
    else:
        backend_args = ['--session', session_info['session_name']]
        if _is_headed_mode():
            backend_args.append('--headed')
    engine = _engine_override or _get_browser_engine()
    if engine != 'auto' and (not _is_camofox_mode()) and (not session_info.get('cdp_url')):
        backend_args += ['--engine', engine]
    if browser_cmd == 'npx agent-browser':
        _npx_bin = shutil.which('npx') or 'npx'
        cmd_prefix = [_npx_bin, 'agent-browser']
    else:
        cmd_prefix = [browser_cmd]
    cmd_parts = cmd_prefix + backend_args + ['--json', command] + args
    try:
        task_socket_dir = os.path.join(_socket_safe_tmpdir(), f"agent-browser-{session_info['session_name']}")
        os.makedirs(task_socket_dir, mode=448, exist_ok=True)
        _write_owner_pid(task_socket_dir, session_info['session_name'])
        logger.debug('browser cmd=%s task=%s socket_dir=%s (%d chars)', command, task_id, task_socket_dir, len(task_socket_dir))
        browser_env = _build_browser_env()
        browser_env['PATH'] = _merge_browser_path(browser_env.get('PATH', ''))
        browser_env['AGENT_BROWSER_SOCKET_DIR'] = task_socket_dir
        if 'AGENT_BROWSER_IDLE_TIMEOUT_MS' not in browser_env:
            idle_ms = str(BROWSER_SESSION_INACTIVITY_TIMEOUT * 1000)
            browser_env['AGENT_BROWSER_IDLE_TIMEOUT_MS'] = idle_ms
        if 'AGENT_BROWSER_ARGS' not in browser_env and 'AGENT_BROWSER_CHROME_FLAGS' not in browser_env:
            if _needs_chromium_sandbox_bypass():
                logger.debug('browser: sandbox bypass needed (root/docker/AppArmor userns) — injecting --no-sandbox')
                browser_env['AGENT_BROWSER_ARGS'] = '--no-sandbox,--disable-dev-shm-usage'
        stdout_path = os.path.join(task_socket_dir, f'_stdout_{command}')
        stderr_path = os.path.join(task_socket_dir, f'_stderr_{command}')
        stdout_fd = os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 384)
        stderr_fd = os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 384)
        try:
            _popen_extra: dict = {}
            if os.name == 'nt':
                _popen_extra['creationflags'] = windows_hide_flags()
                _popen_extra['close_fds'] = True
                _si = subprocess.STARTUPINFO()
                _si.dwFlags |= subprocess.STARTF_USESTDHANDLES
                _popen_extra['startupinfo'] = _si
            proc = subprocess.Popen(cmd_parts, stdout=stdout_fd, stderr=stderr_fd, stdin=subprocess.DEVNULL, env=browser_env, **_popen_extra)
        finally:
            os.close(stdout_fd)
            os.close(stderr_fd)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            stdout, stderr = _read_command_output_files(stdout_path, stderr_path)
            _unlink_command_output_files(stdout_path, stderr_path)
            if stderr and stderr.strip():
                logger.warning("browser '%s' stderr after timeout: %s", command, stderr.strip()[:500])
            logger.warning("browser '%s' timed out after %ds (task=%s, socket_dir=%s)", command, timeout, task_id, task_socket_dir)
            result = {'success': False, 'error': _format_browser_timeout_error(command, timeout, stdout, stderr)}
        else:
            with open(stdout_path, 'r', encoding='utf-8') as f:
                stdout = f.read()
            with open(stderr_path, 'r', encoding='utf-8') as f:
                stderr = f.read()
            returncode = proc.returncode
            for p in (stdout_path, stderr_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass
            if stderr and stderr.strip():
                level = logging.WARNING if returncode != 0 else logging.DEBUG
                logger.log(level, "browser '%s' stderr: %s", command, stderr.strip()[:500])
            stdout_text = stdout.strip()
            if not stdout_text and returncode == 0 and (command not in _EMPTY_OK_COMMANDS):
                logger.warning("browser '%s' returned empty output (rc=0)", command)
                result = {'success': False, 'error': f"Browser command '{command}' returned no output"}
            elif stdout_text:
                try:
                    parsed = json.loads(stdout_text)
                    if command == 'snapshot' and parsed.get('success'):
                        snap_data = parsed.get('data', {})
                        if not snap_data.get('snapshot') and (not snap_data.get('refs')):
                            logger.warning('snapshot returned empty content. Possible stale daemon or CDP connection issue. returncode=%s', returncode)
                    result = parsed
                except json.JSONDecodeError:
                    raw = stdout_text[:2000]
                    logger.warning("browser '%s' returned non-JSON output (rc=%s): %s", command, returncode, raw[:500])
                    if command == 'screenshot':
                        stderr_text = (stderr or '').strip()
                        combined_text = '\n'.join((part for part in [stdout_text, stderr_text] if part))
                        recovered_path = _extract_screenshot_path_from_text(combined_text)
                        if recovered_path and Path(recovered_path).exists():
                            logger.info("browser 'screenshot' recovered file from non-JSON output: %s", recovered_path)
                            result = {'success': True, 'data': {'path': recovered_path, 'raw': raw}}
                        else:
                            result = {'success': False, 'error': f"Non-JSON output from agent-browser for '{command}': {raw}"}
                    else:
                        result = {'success': False, 'error': f"Non-JSON output from agent-browser for '{command}': {raw}"}
            elif returncode != 0:
                error_msg = stderr.strip() if stderr else f'Command failed with code {returncode}'
                logger.warning("browser '%s' failed (rc=%s): %s", command, returncode, error_msg[:300])
                result = {'success': False, 'error': error_msg}
            else:
                result = {'success': True, 'data': {}}
    except Exception as e:
        logger.warning("browser '%s' exception: %s", command, e, exc_info=True)
        result = {'success': False, 'error': str(e)}
    fallback_reason = _lightpanda_fallback_reason(engine, command, result)
    if fallback_reason:
        logger.info("Lightpanda fallback: retrying '%s' with Chrome (task=%s): %s", command, task_id, fallback_reason)
        if command == 'screenshot':
            fallback_result = _chrome_fallback_screenshot(task_id, args or [], timeout)
        else:
            fallback_result = _run_chrome_fallback_command(task_id, command, args, timeout)
        return _annotate_lightpanda_fallback(fallback_result, fallback_reason)
    return result

def _store_full_snapshot(snapshot_text: str) -> Optional[str]:
    """Write a full page snapshot to cache/web and return its absolute path.

    Called whenever a snapshot exceeds SNAPSHOT_SUMMARIZE_THRESHOLD and the
    model is about to receive a truncated or LLM-summarized view. Mirrors
    ``web_tools._store_full_text``: the file lands in the same cache/web
    directory (mounted read-only into remote backends via
    credential_files._CACHE_DIRS) so the agent's read_file/terminal tools can
    page through the complete accessibility tree — including element refs that
    the truncated view dropped — on any backend.

    The stored copy is secret-redacted (same force-redaction boundary as
    ``_redact_browser_output``) since page-rendered API keys or tokens must
    not be written to disk unmasked. The filename is keyed on a content hash,
    so repeated snapshots of the same page state dedupe to one file. Returns
    None on failure (storage is best-effort; the truncated view is still
    returned to the model).
    """
    try:
        import hashlib
        from hermes_constants import get_hermes_dir
        from agent.redact import redact_sensitive_text
        content = redact_sensitive_text(snapshot_text, force=True)
        if len(content) > MAX_STORED_SNAPSHOT_CHARS:
            content = content[:MAX_STORED_SNAPSHOT_CHARS] + f'\n\n[... stored copy truncated at {MAX_STORED_SNAPSHOT_CHARS:,} chars of {len(content):,} ...]'
        cache_dir = get_hermes_dir('cache/web', 'web_cache')
        cache_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(content.encode('utf-8')).hexdigest()[:10]
        path = cache_dir / f'browser-snapshot-{digest}.txt'
        path.write_text(content, encoding='utf-8')
        return str(path)
    except Exception as exc:
        logger.debug('Failed to store full browser snapshot: %s', exc)
        return None

def _extract_relevant_content(snapshot_text: str, user_task: Optional[str]=None) -> str:
    """Use LLM to extract relevant content from a snapshot based on the user's task.

    The full snapshot is stored to cache/web first (summarization is lossy —
    the pointer lets the agent read anything the summary dropped). Falls back
    to simple truncation when no auxiliary text model is configured.
    """
    stored_path = _store_full_snapshot(snapshot_text)
    stored_note = f'\n\n[Summarized from a {len(snapshot_text):,}-char snapshot. Full snapshot saved to: {stored_path} — read it with read_file if anything is missing.]' if stored_path else ''
    if user_task:
        extraction_prompt = f"You are a content extractor for a browser automation agent.\n\nThe user's task is: {user_task}\n\nGiven the following page snapshot (accessibility tree representation), extract and summarize the most relevant information for completing this task. Focus on:\n1. Interactive elements (buttons, links, inputs) that might be needed\n2. Text content relevant to the task (prices, descriptions, headings, important info)\n3. Navigation structure if relevant\n\nKeep ref IDs (like [ref=e5]) for interactive elements so the agent can use them.\n\nPage Snapshot:\n{snapshot_text}\n\nProvide a concise summary that preserves actionable information and relevant content."
    else:
        extraction_prompt = f'Summarize this page snapshot, preserving:\n1. All interactive elements with their ref IDs (like [ref=e5])\n2. Key text content and headings\n3. Important information visible on the page\n\nPage Snapshot:\n{snapshot_text}\n\nProvide a concise summary focused on interactive elements and key content.'
    from agent.redact import redact_sensitive_text
    extraction_prompt = redact_sensitive_text(extraction_prompt)
    try:
        call_kwargs = {'task': 'web_extract', 'messages': [{'role': 'user', 'content': extraction_prompt}], 'max_tokens': 4000, 'temperature': 0.1}
        model = _get_extraction_model()
        if model:
            call_kwargs['model'] = model
        response = _lazy_call_llm(**call_kwargs)
        extracted = (response.choices[0].message.content or '').strip()
        if not extracted:
            return _truncate_snapshot(snapshot_text)
        return redact_sensitive_text(extracted) + stored_note
    except Exception:
        return _truncate_snapshot(snapshot_text)

def _truncate_snapshot(snapshot_text: str, max_chars: int=SNAPSHOT_SUMMARIZE_THRESHOLD) -> str:
    """Structure-aware truncation for snapshots.

    Cuts at line boundaries so that accessibility tree elements are never
    split mid-line. The full snapshot is saved to cache/web (same pattern as
    web_extract's truncate-and-store) and the appended note tells the agent
    exactly where the complete text lives and how to page through it with
    read_file — element refs beyond the cut are in the file, not lost.

    Args:
        snapshot_text: The snapshot text to truncate
        max_chars: Maximum characters to keep

    Returns:
        Truncated text with a stored-full-text pointer if truncated
    """
    if len(snapshot_text) <= max_chars:
        return snapshot_text
    stored_path = _store_full_snapshot(snapshot_text)
    lines = snapshot_text.split('\n')
    result: list[str] = []
    chars = 0
    reserve = min(110 + len(stored_path or ''), max_chars // 2)
    for line in lines:
        if chars + len(line) + 1 > max_chars - reserve:
            break
        result.append(line)
        chars += len(line) + 1
    remaining = len(lines) - len(result)
    if remaining > 0:
        if stored_path:
            next_line = len(result) + 1
            result.append(f'\n[... {remaining} more lines truncated — full snapshot: read_file path="{stored_path}" offset={next_line} limit=200]')
        else:
            result.append(f'\n[... {remaining} more lines truncated, use browser_snapshot for full content]')
    return '\n'.join(result)

def _redact_browser_output(value: Any) -> Any:
    """Redact secrets from browser-originated data before returning to the model.

    Browser snapshots, console messages, JS exceptions, and eval results can
    contain page-rendered API keys, cookies, bearer tokens, or pasted secrets.
    Tool output is a model boundary, so force redaction here even if global log
    redaction is disabled for debugging.
    """
    from agent.redact import redact_sensitive_text
    if isinstance(value, str):
        return redact_sensitive_text(value, force=True)
    if isinstance(value, list):
        return [_redact_browser_output(item) for item in value]
    if isinstance(value, tuple):
        return tuple((_redact_browser_output(item) for item in value))
    if isinstance(value, dict):
        return {key: _redact_browser_output(item) for key, item in value.items()}
    return value

def browser_navigate(url: str, task_id: Optional[str]=None) -> str:
    """
    Navigate to a URL in the browser.

    Args:
        url: The URL to navigate to
        task_id: Task identifier for session isolation

    Returns:
        JSON string with navigation result (includes stealth features info on first nav)
    """
    import urllib.parse
    from agent.redact import _PREFIX_RE
    url_decoded = urllib.parse.unquote(url)
    if _PREFIX_RE.search(url) or _PREFIX_RE.search(url_decoded):
        return json.dumps({'success': False, 'error': 'Blocked: URL contains what appears to be an API key or token. Secrets must not be sent in URLs.'})
    url = _normalize_url_for_request(url)
    normalized_decoded = urllib.parse.unquote(url)
    if _PREFIX_RE.search(url) or _PREFIX_RE.search(normalized_decoded):
        return json.dumps({'success': False, 'error': 'Blocked: URL contains what appears to be an API key or token. Secrets must not be sent in URLs.'})
    effective_task_id = task_id or 'default'
    nav_session_key = _navigation_session_key(effective_task_id, url)
    auto_local_this_nav = _is_local_sidecar_key(nav_session_key)
    sensitive_query_key = _sensitive_query_param_name(url)
    if sensitive_query_key and (not _is_local_backend()) and (not auto_local_this_nav):
        return json.dumps({'success': False, 'error': f'Blocked: URL contains a credential-like query parameter ({sensitive_query_key}). Cloud browser backends are third-party readers; use a local browser/CDP session or remove the sensitive query parameter before navigating.'})
    if _is_always_blocked_url(url):
        return json.dumps({'success': False, 'error': 'Blocked: URL targets a cloud metadata endpoint'})
    if not _is_local_backend() and (not auto_local_this_nav) and (not _allow_private_urls()) and (not _is_safe_url(url)):
        return json.dumps({'success': False, 'error': 'Blocked: URL targets a private or internal address'})
    blocked = check_website_access(url)
    if blocked:
        return json.dumps({'success': False, 'error': blocked['message'], 'blocked_by_policy': {'host': blocked['host'], 'rule': blocked['rule'], 'source': blocked['source']}})
    if _is_camofox_mode():
        from tools.browser_camofox import camofox_navigate
        return camofox_navigate(url, task_id)
    if auto_local_this_nav:
        logger.info('browser_navigate: auto-routing %s to local Chromium sidecar (cloud provider %s stays on cloud for public URLs; set browser.auto_local_for_private_urls: false to disable)', url, type(_get_cloud_provider()).__name__ if _get_cloud_provider() else 'none')
    session_info = _get_session_info(nav_session_key)
    is_first_nav = session_info.get('_first_nav', True)
    if is_first_nav:
        session_info['_first_nav'] = False
        _maybe_start_recording(nav_session_key)
    result = _run_browser_command(nav_session_key, 'open', [url], timeout=_get_open_command_timeout(first_open=is_first_nav))
    if result.get('success'):
        data = result.get('data', {})
        title = data.get('title', '')
        final_url = data.get('url', url)
        if final_url and final_url != url and _is_always_blocked_url(final_url):
            _run_browser_command(nav_session_key, 'open', ['about:blank'], timeout=10)
            return json.dumps({'success': False, 'error': 'Blocked: redirect landed on a cloud metadata endpoint'})
        if not _is_local_backend() and (not auto_local_this_nav) and (not _allow_private_urls()) and final_url and (final_url != url) and (not _is_safe_url(final_url)):
            _run_browser_command(nav_session_key, 'open', ['about:blank'], timeout=10)
            return json.dumps({'success': False, 'error': 'Blocked: redirect landed on a private/internal address'})
        response = {'success': True, 'url': final_url, 'title': title}
        _last_active_session_key[effective_task_id] = nav_session_key
        _copy_fallback_warning(response, result)
        blocked_patterns = ['access denied', 'access to this page has been denied', 'blocked', 'bot detected', 'verification required', 'please verify', 'are you a robot', 'captcha', 'cloudflare', 'ddos protection', 'checking your browser', 'just a moment', 'attention required']
        title_lower = title.lower()
        if any((pattern in title_lower for pattern in blocked_patterns)):
            response['bot_detection_warning'] = f"Page title '{title}' suggests bot detection. The site may have blocked this request. Options: 1) Try adding delays between actions, 2) Access different pages first, 3) Enable advanced stealth (BROWSERBASE_ADVANCED_STEALTH=true, requires Scale plan), 4) Some sites have very aggressive bot detection that may be unavoidable."
        if is_first_nav and 'features' in session_info:
            features = session_info['features']
            active_features = [k for k, v in features.items() if v]
            if not features.get('proxies'):
                response['stealth_warning'] = 'Running WITHOUT residential proxies. Bot detection may be more aggressive. Consider upgrading Browserbase plan for proxy support.'
            response['stealth_features'] = active_features
        try:
            snap_result = _run_browser_command(nav_session_key, 'snapshot', ['-c'])
            if snap_result.get('success'):
                snap_data = snap_result.get('data', {})
                snapshot_text = snap_data.get('snapshot', '')
                refs = snap_data.get('refs', {})
                if len(snapshot_text) > SNAPSHOT_SUMMARIZE_THRESHOLD:
                    snapshot_text = _truncate_snapshot(snapshot_text)
                response['snapshot'] = _redact_browser_output(snapshot_text)
                response['element_count'] = len(refs) if refs else 0
                if snap_result.get('fallback_warning') and (not response.get('fallback_warning')):
                    _copy_fallback_warning(response, snap_result)
        except Exception as e:
            logger.debug('Auto-snapshot after navigate failed: %s', e)
        return json.dumps(response, ensure_ascii=False)
    else:
        return json.dumps({'success': False, 'error': result.get('error', 'Navigation failed')}, ensure_ascii=False)

def browser_snapshot(full: bool=False, task_id: Optional[str]=None, user_task: Optional[str]=None) -> str:
    """
    Get a text-based snapshot of the current page's accessibility tree.

    Args:
        full: If True, return complete snapshot. If False, return compact view.
        task_id: Task identifier for session isolation
        user_task: The user's current task (for task-aware extraction)

    Returns:
        JSON string with page snapshot
    """
    if _is_camofox_mode():
        from tools.browser_camofox import camofox_snapshot
        return camofox_snapshot(full, task_id, user_task)
    effective_task_id = _last_session_key(task_id or 'default')
    args = []
    if not full:
        args.extend(['-c'])
    result = _run_browser_command(effective_task_id, 'snapshot', args)
    if result.get('success'):
        data = result.get('data', {})
        snapshot_text = data.get('snapshot', '')
        refs = data.get('refs', {})
        if not _is_local_backend() and (not _is_local_sidecar_key(effective_task_id)) and (not _allow_private_urls()):
            try:
                _url_result = _run_browser_command(effective_task_id, 'eval', ['window.location.href'], timeout=5, _engine_override='auto')
                if _url_result.get('success'):
                    _current_url = _url_result.get('data', {}).get('result', '').strip().strip('"').strip("'")
                    if _current_url and (not _is_safe_url(_current_url)):
                        return json.dumps({'success': False, 'error': f'Blocked: page URL targets a private or internal address ({_current_url}). This may have been caused by a JavaScript navigation via browser_console.'}, ensure_ascii=False)
            except Exception as _url_exc:
                logger.debug('browser_snapshot: URL safety check failed (%s)', _url_exc)
        if len(snapshot_text) > SNAPSHOT_SUMMARIZE_THRESHOLD and user_task:
            snapshot_text = _extract_relevant_content(snapshot_text, user_task)
        elif len(snapshot_text) > SNAPSHOT_SUMMARIZE_THRESHOLD:
            snapshot_text = _truncate_snapshot(snapshot_text)
        response = {'success': True, 'snapshot': _redact_browser_output(snapshot_text), 'element_count': len(refs) if refs else 0}
        _copy_fallback_warning(response, result)
        try:
            from tools.browser_supervisor import SUPERVISOR_REGISTRY
            _supervisor = SUPERVISOR_REGISTRY.get(effective_task_id)
            if _supervisor is not None:
                _sv_snap = _supervisor.snapshot()
                if _sv_snap.active:
                    response.update(_redact_browser_output(_sv_snap.to_dict()))
        except Exception as _sv_exc:
            logger.debug('supervisor snapshot merge failed: %s', _sv_exc)
        return json.dumps(response, ensure_ascii=False)
    else:
        response = {'success': False, 'error': result.get('error', 'Failed to get snapshot')}
        return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)

def browser_click(ref: str, task_id: Optional[str]=None) -> str:
    """
    Click on an element.

    Args:
        ref: Element reference (e.g., "@e5")
        task_id: Task identifier for session isolation

    Returns:
        JSON string with click result
    """
    if _is_camofox_mode():
        from tools.browser_camofox import camofox_click
        return camofox_click(ref, task_id)
    effective_task_id = _last_session_key(task_id or 'default')
    blocked = _blocked_private_page_action(effective_task_id, 'click')
    if blocked is not None:
        return blocked
    if not ref.startswith('@'):
        ref = f'@{ref}'
    result = _run_browser_command(effective_task_id, 'click', [ref])
    if result.get('success'):
        response = {'success': True, 'clicked': ref}
        return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)
    else:
        response = {'success': False, 'error': result.get('error', f'Failed to click {ref}')}
        return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)

def browser_type(ref: str, text: str, task_id: Optional[str]=None) -> str:
    """
    Type text into an input field.

    Args:
        ref: Element reference (e.g., "@e3")
        text: Text to type
        task_id: Task identifier for session isolation

    Returns:
        JSON string with type result
    """
    if _is_camofox_mode():
        from tools.browser_camofox import camofox_type
        return camofox_type(ref, text, task_id)
    effective_task_id = _last_session_key(task_id or 'default')
    blocked = _blocked_private_page_action(effective_task_id, 'type')
    if blocked is not None:
        return blocked
    if not ref.startswith('@'):
        ref = f'@{ref}'
    result = _run_browser_command(effective_task_id, 'fill', [ref, text])
    from agent.display import redact_browser_typed_text_for_display, redact_tool_args_for_display
    display_text = (redact_tool_args_for_display('browser_type', {'text': text}) or {})['text']
    if result.get('success'):
        response = {'success': True, 'typed': display_text, 'element': ref}
        response = _copy_fallback_warning(response, result)
        response = redact_browser_typed_text_for_display(response, text)
        return json.dumps(response, ensure_ascii=False)
    else:
        response = {'success': False, 'error': result.get('error', f'Failed to type into {ref}')}
        response = _copy_fallback_warning(response, result)
        response = redact_browser_typed_text_for_display(response, text)
        return json.dumps(response, ensure_ascii=False)

def browser_scroll(direction: str, task_id: Optional[str]=None) -> str:
    """
    Scroll the page.

    Args:
        direction: "up" or "down"
        task_id: Task identifier for session isolation

    Returns:
        JSON string with scroll result
    """
    if direction not in {'up', 'down'}:
        return json.dumps({'success': False, 'error': f"Invalid direction '{direction}'. Use 'up' or 'down'."}, ensure_ascii=False)
    _SCROLL_PIXELS = 500
    if _is_camofox_mode():
        from tools.browser_camofox import camofox_scroll
        _SCROLL_REPEATS = 5
        result = None
        for _ in range(_SCROLL_REPEATS):
            result = camofox_scroll(direction, task_id)
        return result
    effective_task_id = _last_session_key(task_id or 'default')
    result = _run_browser_command(effective_task_id, 'scroll', [direction, str(_SCROLL_PIXELS)])
    if not result.get('success'):
        response = {'success': False, 'error': result.get('error', f'Failed to scroll {direction}')}
        return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)
    response = {'success': True, 'scrolled': direction}
    return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)

def browser_back(task_id: Optional[str]=None) -> str:
    """
    Navigate back in browser history.

    Args:
        task_id: Task identifier for session isolation

    Returns:
        JSON string with navigation result
    """
    if _is_camofox_mode():
        from tools.browser_camofox import camofox_back
        return camofox_back(task_id)
    effective_task_id = _last_session_key(task_id or 'default')
    result = _run_browser_command(effective_task_id, 'back', [])
    if result.get('success'):
        if _eval_ssrf_guard_active(effective_task_id):
            _blocked_url = _current_page_private_url(effective_task_id)
            if _blocked_url:
                return json.dumps({'success': False, 'error': f'Blocked: page URL targets a private or internal address ({_blocked_url}). Browser history navigation (back) landed on this address.'}, ensure_ascii=False)
        data = result.get('data', {})
        response = {'success': True, 'url': data.get('url', '')}
        return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)
    else:
        response = {'success': False, 'error': result.get('error', 'Failed to go back')}
        return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)

def browser_press(key: str, task_id: Optional[str]=None) -> str:
    """
    Press a keyboard key.

    Args:
        key: Key to press (e.g., "Enter", "Tab")
        task_id: Task identifier for session isolation

    Returns:
        JSON string with key press result
    """
    if _is_camofox_mode():
        from tools.browser_camofox import camofox_press
        return camofox_press(key, task_id)
    effective_task_id = _last_session_key(task_id or 'default')
    blocked = _blocked_private_page_action(effective_task_id, 'press')
    if blocked is not None:
        return blocked
    result = _run_browser_command(effective_task_id, 'press', [key])
    if result.get('success'):
        response = {'success': True, 'pressed': key}
        return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)
    else:
        response = {'success': False, 'error': result.get('error', f'Failed to press {key}')}
        return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)

def _blocked_private_page_action(effective_task_id: str, action: str) -> Optional[str]:
    """Return a blocked payload when an unsafe cloud page would receive input."""
    if not _eval_ssrf_guard_active(effective_task_id):
        return None
    blocked_url = _current_page_private_url(effective_task_id)
    if not blocked_url:
        return None
    return json.dumps({'success': False, 'error': f'Blocked: page URL targets a private or internal address ({blocked_url}). Refusing to {action} on this page in this browser mode.'}, ensure_ascii=False)

def browser_console(clear: bool=False, expression: Optional[str]=None, task_id: Optional[str]=None) -> str:
    """Get browser console messages and JavaScript errors, or evaluate JS in the page.

    When ``expression`` is provided, evaluates JavaScript in the page context
    (like the DevTools console) and returns the result.  Otherwise returns
    console output (log/warn/error/info) and uncaught exceptions.

    Args:
        clear: If True, clear the message/error buffers after reading
        expression: JavaScript expression to evaluate in the page context
        task_id: Task identifier for session isolation

    Returns:
        JSON string with console messages/errors, or eval result
    """
    if expression is not None:
        policy_error = _enforce_browser_eval_policy(expression)
        if policy_error:
            return json.dumps({'success': False, 'error': policy_error}, ensure_ascii=False)
        return _browser_eval(expression, task_id)
    if _is_camofox_mode():
        from tools.browser_camofox import camofox_console
        return camofox_console(clear, task_id)
    effective_task_id = _last_session_key(task_id or 'default')
    if _eval_ssrf_guard_active(effective_task_id):
        _blocked_url = _current_page_private_url(effective_task_id)
        if _blocked_url:
            return json.dumps({'success': False, 'error': f'Blocked: page URL targets a private or internal address ({_blocked_url}). This may have been caused by a JavaScript navigation via browser_console.'}, ensure_ascii=False)
    console_args = ['--clear'] if clear else []
    error_args = ['--clear'] if clear else []
    console_result = _run_browser_command(effective_task_id, 'console', console_args)
    errors_result = _run_browser_command(effective_task_id, 'errors', error_args)
    messages = []
    if console_result.get('success'):
        for msg in console_result.get('data', {}).get('messages', []):
            messages.append({'type': msg.get('type', 'log'), 'text': _redact_browser_output(msg.get('text', '')), 'source': 'console'})
    errors = []
    if errors_result.get('success'):
        for err in errors_result.get('data', {}).get('errors', []):
            errors.append({'message': _redact_browser_output(err.get('message', '')), 'source': 'exception'})
    response = {'success': True, 'console_messages': messages, 'js_errors': errors, 'total_messages': len(messages), 'total_errors': len(errors)}
    _copy_fallback_warning(response, console_result)
    if errors_result.get('fallback_warning') and (not response.get('fallback_warning')):
        _copy_fallback_warning(response, errors_result)
    return json.dumps(response, ensure_ascii=False)

def _eval_ssrf_guard_active(effective_task_id: str) -> bool:
    """Return True when eval-driven private-network access must be guarded.

    Matches the gating used by ``browser_navigate`` / ``browser_snapshot`` /
    ``browser_vision``: the SSRF guard is only meaningful for non-local
    backends (cloud browser, or a containerized terminal whose browser-on-host
    can reach internal networks the terminal can't), and is skipped for local
    sidecar sessions and when ``allow_private_urls`` is set.
    """
    return not _is_local_backend() and (not _is_local_sidecar_key(effective_task_id)) and (not _allow_private_urls())
_JS_URL_LITERAL_RE = re.compile('https?://[^\\s\'"`)\\]<>]+', re.IGNORECASE)

def _expression_targets_private_url(expression: str) -> Optional[str]:
    """Return the first private/always-blocked URL literal in a JS expression.

    Best-effort: scans for ``http(s)://...`` literals (fetch/XHR/navigation
    targets the agent may have embedded) and returns the first one that targets
    a private/internal address or the always-blocked cloud-metadata floor.
    Returns ``None`` when no such literal is found.
    """
    if not isinstance(expression, str):
        return None
    for match in _JS_URL_LITERAL_RE.findall(expression):
        candidate = match.rstrip('.,;')
        if _is_always_blocked_url(candidate) or not _is_safe_url(candidate):
            return candidate
    return None

def _current_page_private_url(effective_task_id: str) -> Optional[str]:
    """Return the current page URL when it targets a private/internal address.

    Reads ``window.location.href`` via a low-cost eval and returns it when the
    page has been navigated (e.g. via ``location.href = '...'`` in a prior
    eval) to an address the SSRF guard would reject.  Returns ``None`` when the
    page is public, the URL can't be determined, or the check errors (fail-open
    on probe failure, matching the snapshot/vision guards).
    """
    try:
        url_result = _run_browser_command(effective_task_id, 'eval', ['window.location.href'], timeout=5, _engine_override='auto')
        if url_result.get('success'):
            current_url = url_result.get('data', {}).get('result', '').strip().strip('"').strip("'")
            if current_url and (_is_always_blocked_url(current_url) or not _is_safe_url(current_url)):
                return current_url
    except Exception as exc:
        logger.debug('_current_page_private_url: probe failed (%s)', exc)
    return None
_RISKY_BROWSER_EVAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = ((re.compile('\\bdocument\\s*\\.\\s*cookie\\b', re.I), 'document.cookie'), (re.compile('\\b(?:localStorage|sessionStorage)\\b', re.I), 'web storage'), (re.compile('\\bindexedDB\\b', re.I), 'IndexedDB'), (re.compile('\\bcaches\\s*\\.\\s*(?:open|match|keys)\\b', re.I), 'Cache Storage'), (re.compile('\\bnavigator\\s*\\.\\s*(?:clipboard|credentials|serviceWorker)\\b', re.I), 'navigator sensitive API'), (re.compile('\\b(?:fetch|XMLHttpRequest|WebSocket|EventSource)\\s*\\(', re.I), 'network request'), (re.compile('\\bnavigator\\s*\\.\\s*sendBeacon\\s*\\(', re.I), 'network beacon'), (re.compile('\\bdocument\\s*\\.\\s*forms\\b.*\\bvalue\\b', re.I | re.S), 'form value extraction'), (re.compile('\\bquerySelector(?:All)?\\s*\\([^)]*(?:input|textarea|password)[^)]*\\).*\\bvalue\\b', re.I | re.S), 'form value extraction'))
_JS_STRING_LITERAL_RE = re.compile('\'(?:\\\\.|[^\'\\\\])*\'|\\"(?:\\\\.|[^\\"\\\\])*\\"|`(?:\\\\.|[^`\\\\])*`', re.S)
_SENSITIVE_BROWSER_EVAL_TOKENS: tuple[tuple[str, str], ...] = (('cookie', 'document.cookie'), ('localStorage', 'web storage'), ('sessionStorage', 'web storage'), ('indexedDB', 'IndexedDB'), ('caches', 'Cache Storage'), ('clipboard', 'navigator sensitive API'), ('credentials', 'navigator sensitive API'), ('serviceWorker', 'navigator sensitive API'), ('fetch', 'network request'), ('XMLHttpRequest', 'network request'), ('WebSocket', 'network request'), ('EventSource', 'network request'), ('sendBeacon', 'network beacon'))

def _allow_unsafe_browser_evaluate() -> bool:
    """Return whether sensitive browser JS evaluation is explicitly allowed.

    When true, ``browser_console(expression=...)`` runs without the
    sensitive-primitive denylist even if ``browser.restrict_evaluate`` is set.
    """
    try:
        from hermes_cli.config import read_raw_config
        cfg = read_raw_config()
        return is_truthy_value(cfg_get(cfg, 'browser', 'allow_unsafe_evaluate'), default=False)
    except Exception as e:
        logger.debug('Could not read browser.allow_unsafe_evaluate from config: %s', e)
        return False

def _restrict_browser_evaluate() -> bool:
    """Return whether the sensitive-primitive eval denylist is enabled.

    Off by default. ``browser_console(expression=...)`` is the agent's only
    programmatic page-inspection path, and the denylist blocks the *names* of
    common primitives (``fetch``, ``cookie``, ``querySelector(...input...)``)
    rather than any actual exfiltration — which also blocks a large class of
    legitimate DOM extraction (any selector or page script text containing
    those words). Egress itself is still gated by the SSRF/private-URL guards
    in ``_browser_eval`` regardless of this setting. Users who want the
    strict vocabulary denylist (e.g. when browsing hostile pages with a
    logged-in profile) opt in with ``browser.restrict_evaluate: true``;
    ``browser.allow_unsafe_evaluate: true`` overrides it back off.
    """
    try:
        from hermes_cli.config import read_raw_config
        cfg = read_raw_config()
        return is_truthy_value(cfg_get(cfg, 'browser', 'restrict_evaluate'), default=False)
    except Exception as e:
        logger.debug('Could not read browser.restrict_evaluate from config: %s', e)
        return False

def _decode_js_string_literal(literal: str) -> str:
    """Best-effort decode of a JavaScript string literal for policy checks.

    This is not a JS parser.  It only normalizes common escaped property names
    such as ``document["co\\x6fkie"]`` before the fail-closed sensitive-token
    check below.
    """
    if len(literal) < 2:
        return literal
    body = literal[1:-1]
    try:
        return bytes(body, 'utf-8').decode('unicode_escape')
    except Exception:
        return body

def _decoded_js_string_literals(expression: str) -> list[str]:
    return [_decode_js_string_literal(match.group(0)) for match in _JS_STRING_LITERAL_RE.finditer(expression)]

def _sensitive_browser_eval_token_reason(expression: str) -> Optional[str]:
    """Return a risk reason for direct or quoted sensitive browser primitives.

    ``browser_console(expression=...)`` executes in the page origin.  A denylist
    that only searches direct spellings like ``document.cookie`` and ``fetch(``
    misses equivalent JavaScript property access such as ``document["cookie"]``
    or ``globalThis["fetch"](...)``.  Treat sensitive primitive names as risky
    whether they appear as identifiers or decoded string-literal property names.
    Concatenating all string literals catches simple obfuscations like
    ``document["coo" + "kie"]`` while the config opt-in preserves the escape
    hatch for trusted pages.
    """
    string_literals = _decoded_js_string_literals(expression)
    concatenated_literals = ''.join(string_literals).lower()
    for token, reason in _SENSITIVE_BROWSER_EVAL_TOKENS:
        if re.search(f'\\b{re.escape(token)}\\b', expression, re.I):
            return reason
        token_lower = token.lower()
        if any((token_lower in literal.lower() for literal in string_literals)):
            return reason
        if token_lower in concatenated_literals:
            return reason
    return None

def _risky_browser_eval_reason(expression: str) -> Optional[str]:
    """Return a human-readable reason if a JS expression uses risky primitives."""
    if not expression:
        return None
    for pattern, reason in _RISKY_BROWSER_EVAL_PATTERNS:
        if pattern.search(expression):
            return reason
    return _sensitive_browser_eval_token_reason(expression)

def _enforce_browser_eval_policy(expression: str) -> Optional[str]:
    """Block sensitive browser JS evaluation when the opt-in denylist is on.

    The denylist is opt-in (``browser.restrict_evaluate: true``) because it
    gates on primitive *names*, which cripples legitimate DOM extraction —
    see ``_restrict_browser_evaluate``. Network egress to private/internal
    addresses is enforced separately in ``_browser_eval`` and does not depend
    on this policy.
    """
    if not _restrict_browser_evaluate():
        return None
    if _allow_unsafe_browser_evaluate():
        return None
    reason = _risky_browser_eval_reason(expression)
    if not reason:
        return None
    return f'Blocked: browser_console(expression=...) tried to use sensitive browser JavaScript primitive ({reason}) while browser.restrict_evaluate is enabled. Use browser_snapshot/browser_get_images/browser_console without expression for normal inspection, or set browser.restrict_evaluate: false in config.yaml to allow programmatic evaluation.'

def _browser_eval(expression: str, task_id: Optional[str]=None) -> str:
    """Evaluate a JavaScript expression in the page context and return the result."""
    effective_task_id = _last_session_key(task_id or 'default')
    if _eval_ssrf_guard_active(effective_task_id):
        blocked_literal = _expression_targets_private_url(expression)
        if blocked_literal:
            return json.dumps({'success': False, 'error': f'Blocked: JavaScript expression targets a private or internal address ({blocked_literal}). Reading internal endpoints via browser_console is not permitted in this browser mode.'}, ensure_ascii=False)
    if _is_camofox_mode():
        return _camofox_eval(expression, task_id)
    try:
        from tools.browser_supervisor import SUPERVISOR_REGISTRY
        supervisor = SUPERVISOR_REGISTRY.get(effective_task_id)
        if supervisor is not None:
            sup_result = supervisor.evaluate_runtime(expression)
            if sup_result.get('ok'):
                raw_result = sup_result.get('result')
                parsed = raw_result
                if isinstance(raw_result, str):
                    try:
                        parsed = json.loads(raw_result)
                    except (json.JSONDecodeError, ValueError):
                        pass
                if _eval_ssrf_guard_active(effective_task_id):
                    _blocked_url = _current_page_private_url(effective_task_id)
                    if _blocked_url:
                        return json.dumps({'success': False, 'error': f'Blocked: page URL targets a private or internal address ({_blocked_url}). This may have been caused by a JavaScript navigation via browser_console.'}, ensure_ascii=False)
                response = {'success': True, 'result': _redact_browser_output(parsed), 'result_type': type(parsed).__name__, 'method': 'cdp_supervisor'}
                return json.dumps(response, ensure_ascii=False, default=str)
            err = sup_result.get('error') or 'evaluate_runtime failed'
            if 'supervisor' not in err.lower():
                return json.dumps({'success': False, 'error': err}, ensure_ascii=False)
            logger.debug('browser_eval: supervisor path unavailable (%s), falling back to subprocess', err)
    except ImportError:
        pass
    except Exception as exc:
        logger.debug('browser_eval: supervisor path errored (%s), falling back', exc)
    result = _run_browser_command(effective_task_id, 'eval', [expression])
    if not result.get('success'):
        err = result.get('error', 'eval failed')
        if any((hint in err.lower() for hint in ('unknown command', 'not supported', 'not found', 'no such command'))):
            response = {'success': False, 'error': f'JavaScript evaluation is not supported by this browser backend. {err}'}
            return json.dumps(_copy_fallback_warning(response, result))
        if 'reference chain is too long' in err.lower():
            response = {'success': False, 'error': "Expression returned a live DOM node / NodeList / Window, which can't be serialized. Extract a primitive value (e.g. .innerText, .href, .src, .value) or use JSON.stringify() / a snapshot tool instead."}
            return json.dumps(_copy_fallback_warning(response, result))
        response = {'success': False, 'error': err}
        return json.dumps(_copy_fallback_warning(response, result))
    data = result.get('data', {})
    raw_result = data.get('result')
    parsed = raw_result
    if isinstance(raw_result, str):
        try:
            parsed = json.loads(raw_result)
        except (json.JSONDecodeError, ValueError):
            pass
    response = {'success': True, 'result': _redact_browser_output(parsed), 'result_type': type(parsed).__name__}
    if _eval_ssrf_guard_active(effective_task_id):
        _blocked_url = _current_page_private_url(effective_task_id)
        if _blocked_url:
            return json.dumps({'success': False, 'error': f'Blocked: page URL targets a private or internal address ({_blocked_url}). This may have been caused by a JavaScript navigation via browser_console.'}, ensure_ascii=False)
    return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False, default=str)

def _camofox_current_page_private_url(tab_id: str, user_id: str) -> Optional[str]:
    """Return the Camofox page URL when it targets a private/internal address.

    Camofox analogue of ``_current_page_private_url`` (evaluate endpoint instead
    of the agent-browser CLI).  Returns ``None`` when the page is public, the URL
    can't be determined, or the probe errors (fail-open on probe failure,
    matching the snapshot/vision guards — do not change to fail-closed without
    also changing the sibling).
    """
    try:
        from tools.browser_camofox import _post
        data = _post(f'/tabs/{tab_id}/evaluate', body={'expression': 'window.location.href', 'userId': user_id})
        current_url = str(data.get('result') if isinstance(data, dict) else data or '')
        current_url = current_url.strip().strip('"').strip("'")
        if current_url and (_is_always_blocked_url(current_url) or not _is_safe_url(current_url)):
            return current_url
    except Exception as exc:
        logger.debug('_camofox_current_page_private_url: probe failed (%s)', exc)
    return None

def _camofox_eval(expression: str, task_id: Optional[str]=None) -> str:
    """Evaluate JS via Camofox's /tabs/{tab_id}/evaluate endpoint (if available)."""
    from tools.browser_camofox import _ensure_tab, _post
    try:
        tab_info = _ensure_tab(task_id or 'default')
        tab_id = tab_info.get('tab_id') or tab_info.get('id')
        user_id = tab_info['user_id']
        resp = _post(f'/tabs/{tab_id}/evaluate', body={'expression': expression, 'userId': user_id})
        raw_result = resp.get('result') if isinstance(resp, dict) else resp
        parsed = raw_result
        if isinstance(raw_result, str):
            try:
                parsed = json.loads(raw_result)
            except (json.JSONDecodeError, ValueError):
                pass
        if _eval_ssrf_guard_active(task_id or 'default'):
            _blocked_url = _camofox_current_page_private_url(tab_id, user_id)
            if _blocked_url:
                return json.dumps({'success': False, 'error': f'Blocked: page URL targets a private or internal address ({_blocked_url}). This may have been caused by a JavaScript navigation via browser_console.'}, ensure_ascii=False)
        return json.dumps({'success': True, 'result': _redact_browser_output(parsed), 'result_type': type(parsed).__name__}, ensure_ascii=False, default=str)
    except Exception as e:
        error_msg = str(e)
        if any((code in error_msg for code in ('404', '405', '501'))):
            return json.dumps({'success': False, 'error': 'JavaScript evaluation is not supported by this Camofox server. Use browser_snapshot or browser_vision to inspect page state.'})
        return tool_error(error_msg, success=False)

def _maybe_start_recording(task_id: str):
    """Start recording if browser.record_sessions is enabled in config."""
    with _cleanup_lock:
        if task_id in _recording_sessions:
            return
    try:
        from hermes_cli.config import read_raw_config
        hermes_home = get_hermes_home()
        cfg = read_raw_config()
        record_enabled = cfg_get(cfg, 'browser', 'record_sessions', default=False)
        if not record_enabled:
            return
        recordings_dir = hermes_home / 'browser_recordings'
        recordings_dir.mkdir(parents=True, exist_ok=True)
        _cleanup_old_recordings(max_age_hours=72)
        timestamp = time.strftime('%Y%m%d_%H%M%S')
        recording_path = recordings_dir / f'session_{timestamp}_{task_id[:16]}.webm'
        result = _run_browser_command(task_id, 'record', ['start', str(recording_path)])
        if result.get('success'):
            with _cleanup_lock:
                _recording_sessions.add(task_id)
            logger.info('Auto-recording browser session %s to %s', task_id, recording_path)
        else:
            logger.debug('Could not start auto-recording: %s', result.get('error'))
    except Exception as e:
        logger.debug('Auto-recording setup failed: %s', e)

def _maybe_stop_recording(task_id: str):
    """Stop recording if one is active for this session."""
    with _cleanup_lock:
        if task_id not in _recording_sessions:
            return
    try:
        result = _run_browser_command(task_id, 'record', ['stop'])
        if result.get('success'):
            path = result.get('data', {}).get('path', '')
            logger.info('Saved browser recording for session %s: %s', task_id, path)
    except Exception as e:
        logger.debug('Could not stop recording for %s: %s', task_id, e)
    finally:
        with _cleanup_lock:
            _recording_sessions.discard(task_id)

def browser_get_images(task_id: Optional[str]=None) -> str:
    """
    Get all images on the current page.

    Args:
        task_id: Task identifier for session isolation

    Returns:
        JSON string with list of images (src and alt)
    """
    if _is_camofox_mode():
        from tools.browser_camofox import camofox_get_images
        return camofox_get_images(task_id)
    effective_task_id = _last_session_key(task_id or 'default')
    js_code = "JSON.stringify(\n        [...document.images].map(img => ({\n            src: img.src,\n            alt: img.alt || '',\n            width: img.naturalWidth,\n            height: img.naturalHeight\n        })).filter(img => img.src && !img.src.startsWith('data:'))\n    )"
    result = _run_browser_command(effective_task_id, 'eval', [js_code])
    if result.get('success'):
        if _eval_ssrf_guard_active(effective_task_id):
            _blocked_url = _current_page_private_url(effective_task_id)
            if _blocked_url:
                return json.dumps({'success': False, 'error': f'Blocked: page URL targets a private or internal address ({_blocked_url}). This may have been caused by a JavaScript navigation via browser_console.'}, ensure_ascii=False)
        data = result.get('data', {})
        raw_result = data.get('result', '[]')
        try:
            if isinstance(raw_result, str):
                images = json.loads(raw_result)
            else:
                images = raw_result
            response = {'success': True, 'images': _redact_browser_output(images), 'count': len(images)}
            return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)
        except json.JSONDecodeError:
            response = {'success': True, 'images': [], 'count': 0, 'warning': 'Could not parse image data'}
            return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)
    else:
        response = {'success': False, 'error': result.get('error', 'Failed to get images')}
        return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)

def browser_vision(question: str, annotate: bool=False, task_id: Optional[str]=None) -> Union[str, Dict[str, Any]]:
    """
    Take a screenshot of the current page for visual inspection.

    Captures what's visually displayed in the browser. When the active model
    supports native vision, the screenshot is attached directly to the
    conversation so the model can inspect it on the next turn; otherwise Duck Agent
    falls back to the auxiliary vision model and returns a text analysis. Useful
    for visual content the text-based snapshot may not capture (CAPTCHAs,
    verification challenges, images, complex layouts, etc.).

    The screenshot is saved persistently and its file path is returned so it
    can be shared with users via MEDIA:<path> in the response.

    Args:
        question: What you want to know about the page visually
        annotate: If True, overlay numbered [N] labels on interactive elements
        task_id: Task identifier for session isolation

    Returns:
        A JSON string with vision analysis results and screenshot_path, or a
        multimodal tool-result envelope carrying the screenshot and metadata.
    """
    if _is_camofox_mode():
        from tools.browser_camofox import camofox_vision
        return camofox_vision(question, annotate, task_id)
    import base64
    import uuid as uuid_mod
    from hermes_constants import get_hermes_dir
    screenshots_dir = get_hermes_dir('cache/screenshots', 'browser_screenshots')
    screenshot_path = screenshots_dir / f'browser_screenshot_{uuid_mod.uuid4().hex}.png'
    effective_task_id = _last_session_key(task_id or 'default')
    if not _is_local_backend() and (not _is_local_sidecar_key(effective_task_id)) and (not _allow_private_urls()):
        try:
            _url_result = _run_browser_command(effective_task_id, 'eval', ['window.location.href'], timeout=5, _engine_override='auto')
            if _url_result.get('success'):
                _current_url = _url_result.get('data', {}).get('result', '').strip().strip('"').strip("'")
                if _current_url and (not _is_safe_url(_current_url)):
                    return json.dumps({'success': False, 'error': f'Blocked: page URL targets a private or internal address ({_current_url}). This may have been caused by a JavaScript navigation via browser_console.'}, ensure_ascii=False)
        except Exception as _url_exc:
            logger.debug('browser_vision: URL safety check failed (%s)', _url_exc)
    engine = _get_browser_engine()
    _lp_prerouted = False
    _lp_fallback_warning = None
    if engine == 'lightpanda' and _should_inject_engine(engine):
        logger.debug('browser_vision: pre-routing screenshot to Chrome (engine=lightpanda)')
        screenshot_args = []
        if annotate:
            screenshot_args.append('--annotate')
        fb_result = _chrome_fallback_screenshot(effective_task_id, screenshot_args, _get_command_timeout())
        fb_reason = 'Lightpanda has no graphical renderer for screenshots; used Chrome for vision capture.'
        fb_result = _annotate_lightpanda_fallback(fb_result, fb_reason)
        if fb_result.get('success'):
            _lp_prerouted = True
            _lp_fallback_warning = fb_result.get('fallback_warning')
            fb_path = fb_result.get('data', {}).get('path', '')
            if fb_path and os.path.exists(fb_path):
                from hermes_constants import get_hermes_dir
                screenshots_dir = get_hermes_dir('cache/screenshots', 'browser_screenshots')
                screenshots_dir.mkdir(parents=True, exist_ok=True)
                import shutil as _shutil_vision
                persistent_path = screenshots_dir / f'browser_screenshot_{uuid_mod.uuid4().hex}.png'
                _shutil_vision.copy2(fb_path, persistent_path)
                screenshot_path = persistent_path
        else:
            logger.warning('Lightpanda Chrome fallback vision screenshot failed: %s', fb_result.get('error'))
            _lp_prerouted = False
    try:
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        _cleanup_old_screenshots(screenshots_dir, max_age_hours=24)
        if _lp_prerouted and screenshot_path.exists():
            result = {'success': True, 'data': {'path': str(screenshot_path), 'fallback_warning': _lp_fallback_warning, 'browser_engine': 'chrome', 'browser_engine_fallback': {'from': 'lightpanda', 'to': 'chrome', 'reason': 'Lightpanda has no graphical renderer for screenshots; used Chrome for vision capture.'}}, 'fallback_warning': _lp_fallback_warning, 'browser_engine': 'chrome', 'browser_engine_fallback': {'from': 'lightpanda', 'to': 'chrome', 'reason': 'Lightpanda has no graphical renderer for screenshots; used Chrome for vision capture.'}}
        else:
            screenshot_args = []
            if annotate:
                screenshot_args.append('--annotate')
            screenshot_args.append('--full')
            screenshot_args.append(str(screenshot_path))
            result = _run_browser_command(effective_task_id, 'screenshot', screenshot_args, _engine_override='auto' if _lp_prerouted else None)
        if not result.get('success'):
            error_detail = result.get('error', 'Unknown error')
            _cp = _get_cloud_provider()
            mode = 'local' if _cp is None else f'cloud ({_cp.provider_name()})'
            error_response = {'success': False, 'error': f'Failed to take screenshot ({mode} mode): {error_detail}'}
            return json.dumps(_copy_fallback_warning(error_response, result), ensure_ascii=False)
        actual_screenshot_path = result.get('data', {}).get('path')
        if actual_screenshot_path:
            screenshot_path = Path(actual_screenshot_path)
        if not screenshot_path.exists():
            _cp = _get_cloud_provider()
            mode = 'local' if _cp is None else f'cloud ({_cp.provider_name()})'
            return json.dumps({'success': False, 'error': f"Screenshot file was not created at {screenshot_path} ({mode} mode). This may indicate a socket path issue (macOS /var/folders/), a missing Chromium install ('agent-browser install'), or a stale daemon process."}, ensure_ascii=False)
        _screenshot_bytes = screenshot_path.read_bytes()
        _screenshot_b64 = base64.b64encode(_screenshot_bytes).decode('ascii')
        data_url = f'data:image/png;base64,{_screenshot_b64}'
        from tools.vision_tools import _build_native_vision_tool_result, _should_use_native_vision_fast_path
        if _should_use_native_vision_fast_path():
            native_result = _build_native_vision_tool_result(image_url=str(screenshot_path), question=question, image_data_url=data_url, image_size_bytes=len(_screenshot_bytes))
            meta = native_result.setdefault('meta', {})
            meta['screenshot_path'] = str(screenshot_path)
            if _lp_fallback_warning:
                meta['fallback_warning'] = _lp_fallback_warning
            if annotate and result.get('data', {}).get('annotations'):
                meta['annotations'] = result['data']['annotations']
            native_result['text_summary'] = f"{native_result.get('text_summary', '')} Screenshot path: {screenshot_path}".strip()
            return native_result
        vision_prompt = f"You are analyzing a screenshot of a web browser.\n\nUser's question: {question}\n\nProvide a detailed and helpful answer based on what you see in the screenshot. If there are interactive elements, describe them. If there are verification challenges or CAPTCHAs, describe what type they are and what action might be needed. Focus on answering the user's specific question."
        vision_model = _get_vision_model()
        logger.debug('browser_vision: analysing screenshot (%d bytes)', len(_screenshot_bytes))
        vision_timeout = 120.0
        vision_temperature = 0.1
        try:
            from hermes_cli.config import load_config
            _cfg = load_config()
            _vision_cfg = cfg_get(_cfg, 'auxiliary', 'vision', default={})
            _vt = _vision_cfg.get('timeout')
            if _vt is not None:
                vision_timeout = float(_vt)
            _vtemp = _vision_cfg.get('temperature')
            if _vtemp is not None:
                vision_temperature = float(_vtemp)
        except Exception:
            pass
        call_kwargs = {'task': 'vision', 'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': vision_prompt}, {'type': 'image_url', 'image_url': {'url': data_url}}]}], 'max_tokens': 2000, 'temperature': vision_temperature, 'timeout': vision_timeout}
        if vision_model:
            call_kwargs['model'] = vision_model
        try:
            response = _lazy_call_llm(**call_kwargs)
        except Exception as _api_err:
            from tools.vision_tools import _is_image_size_error, _resize_image_for_vision, _RESIZE_TARGET_BYTES
            if _is_image_size_error(_api_err) and len(data_url) > _RESIZE_TARGET_BYTES:
                logger.info('Vision API rejected screenshot (%.1f MB); auto-resizing to ~%.0f MB and retrying...', len(data_url) / (1024 * 1024), _RESIZE_TARGET_BYTES / (1024 * 1024))
                data_url = _resize_image_for_vision(screenshot_path, mime_type='image/png')
                call_kwargs['messages'][0]['content'][1]['image_url']['url'] = data_url
                response = _lazy_call_llm(**call_kwargs)
            else:
                raise
        analysis = (response.choices[0].message.content or '').strip()
        from agent.redact import redact_sensitive_text
        analysis = redact_sensitive_text(analysis)
        response_data = {'success': True, 'analysis': analysis or 'Vision analysis returned no content.', 'screenshot_path': str(screenshot_path)}
        _copy_fallback_warning(response_data, result)
        if annotate and result.get('data', {}).get('annotations'):
            response_data['annotations'] = result['data']['annotations']
        return json.dumps(response_data, ensure_ascii=False)
    except Exception as e:
        logger.warning('browser_vision failed: %s', e, exc_info=True)
        error_info = {'success': False, 'error': f'Error during vision analysis: {str(e)}'}
        if screenshot_path.exists():
            error_info['screenshot_path'] = str(screenshot_path)
            error_info['note'] = 'Screenshot was captured but vision analysis failed. You can still share it via MEDIA:<path>.'
        _copy_fallback_warning(error_info, result if 'result' in locals() else {})
        return json.dumps(error_info, ensure_ascii=False)

def _cleanup_old_screenshots(screenshots_dir, max_age_hours=24):
    """Remove browser screenshots older than max_age_hours to prevent disk bloat.

    Throttled to run at most once per hour per directory to avoid repeated
    scans on screenshot-heavy workflows.
    """
    key = str(screenshots_dir)
    now = time.time()
    if now - _last_screenshot_cleanup_by_dir.get(key, 0.0) < 3600:
        return
    _last_screenshot_cleanup_by_dir[key] = now
    try:
        cutoff = time.time() - max_age_hours * 3600
        for f in screenshots_dir.glob('browser_screenshot_*.png'):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except Exception as e:
                logger.debug('Failed to clean old screenshot %s: %s', f, e)
    except Exception as e:
        logger.debug('Screenshot cleanup error (non-critical): %s', e)

def _cleanup_old_recordings(max_age_hours=72):
    """Remove browser recordings older than max_age_hours to prevent disk bloat."""
    try:
        hermes_home = get_hermes_home()
        recordings_dir = hermes_home / 'browser_recordings'
        if not recordings_dir.exists():
            return
        cutoff = time.time() - max_age_hours * 3600
        for f in recordings_dir.glob('session_*.webm'):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except Exception as e:
                logger.debug('Failed to clean old recording %s: %s', f, e)
    except Exception as e:
        logger.debug('Recording cleanup error (non-critical): %s', e)

def cleanup_browser(task_id: Optional[str]=None) -> None:
    """
    Clean up browser session(s) for a task.

    Called automatically when a task completes or when inactivity timeout is reached.
    Closes both the agent-browser/Browserbase session and Camofox sessions.

    When ``task_id`` is a bare task identifier (no ``::local`` suffix), reaps
    BOTH the cloud/primary session AND any hybrid-routing local sidecar that
    may have been spawned for LAN/localhost URLs in the same task.  When
    ``task_id`` already carries a ``::local`` suffix (called from the inactivity
    cleanup loop against a specific session key), reaps only that one.

    Args:
        task_id: Task identifier (or explicit session key)
    """
    if task_id is None:
        task_id = 'default'
    if _is_local_sidecar_key(task_id):
        session_keys = [task_id]
        bare_task_id = task_id[:-len(_LOCAL_SUFFIX)]
    else:
        session_keys = [task_id]
        sidecar_key = f'{task_id}{_LOCAL_SUFFIX}'
        with _cleanup_lock:
            if sidecar_key in _active_sessions:
                session_keys.append(sidecar_key)
        bare_task_id = task_id
    for session_key in session_keys:
        _cleanup_single_browser_session(session_key)
    if _is_local_sidecar_key(task_id):
        if _last_active_session_key.get(bare_task_id) == task_id:
            _last_active_session_key.pop(bare_task_id, None)
    else:
        _last_active_session_key.pop(bare_task_id, None)

def _cleanup_single_browser_session(task_id: str) -> None:
    """Internal: reap a single browser session by its exact session key."""
    _stop_cdp_supervisor(task_id)
    if _is_camofox_mode():
        try:
            from tools.browser_camofox import camofox_close, camofox_soft_cleanup
            if not camofox_soft_cleanup(task_id):
                camofox_close(task_id)
        except Exception as e:
            logger.debug('Camofox cleanup for task %s: %s', task_id, e)
    logger.debug('cleanup_browser called for task_id: %s', task_id)
    logger.debug('Active sessions: %s', list(_active_sessions.keys()))
    with _cleanup_lock:
        session_info = _active_sessions.get(task_id)
    if session_info:
        bb_session_id = session_info.get('bb_session_id', 'unknown')
        logger.debug('Found session for task %s: bb_session_id=%s', task_id, bb_session_id)
        _maybe_stop_recording(task_id)
        if _session_has_expired(session_info):
            logger.debug('Skipping agent-browser close for expired session %s', task_id)
        else:
            try:
                _run_browser_command(task_id, 'close', [], timeout=10)
                logger.debug('agent-browser close command completed for task %s', task_id)
            except Exception as e:
                logger.warning('agent-browser close failed for task %s: %s', task_id, e)
        with _cleanup_lock:
            _active_sessions.pop(task_id, None)
            _session_last_activity.pop(task_id, None)
        if bb_session_id:
            provider = _get_cloud_provider()
            if provider is not None:
                try:
                    provider.close_session(bb_session_id)
                except Exception as e:
                    logger.warning('Could not close cloud browser session: %s', e)
        session_name = session_info.get('session_name', '')
        if session_name:
            socket_dir = os.path.join(_socket_safe_tmpdir(), f'agent-browser-{session_name}')
            if os.path.exists(socket_dir):
                pid_file = os.path.join(socket_dir, f'{session_name}.pid')
                if os.path.isfile(pid_file):
                    try:
                        from tools.process_registry import ProcessRegistry
                        daemon_pid = int(Path(pid_file).read_text(encoding='utf-8').strip())
                        ProcessRegistry._terminate_host_pid(daemon_pid)
                        logger.debug('Killed daemon pid %s for %s', daemon_pid, session_name)
                    except (ProcessLookupError, ValueError, PermissionError, OSError):
                        logger.debug('Could not kill daemon pid for %s (already dead or inaccessible)', session_name)
                shutil.rmtree(socket_dir, ignore_errors=True)
        logger.debug('Removed task %s from active sessions', task_id)
    else:
        logger.debug('No active session found for task_id: %s', task_id)

def cleanup_all_browsers() -> None:
    """
    Clean up all active browser sessions.

    Useful for cleanup on shutdown.
    """
    with _cleanup_lock:
        task_ids = list(_active_sessions.keys())
    for task_id in task_ids:
        cleanup_browser(task_id)
    try:
        from tools.browser_supervisor import SUPERVISOR_REGISTRY
        SUPERVISOR_REGISTRY.stop_all()
    except Exception:
        pass
    global _cached_agent_browser, _agent_browser_resolved
    global _cached_command_timeout, _command_timeout_resolved
    global _cached_chromium_installed
    global _cached_browser_engine, _browser_engine_resolved
    _cached_agent_browser = None
    _agent_browser_resolved = False
    _discover_homebrew_node_dirs.cache_clear()
    _command_timeout_resolved = False
    _cached_command_timeout = None
    _cached_chromium_installed = None
    global _chromium_autoinstall_attempted
    _chromium_autoinstall_attempted = False
    _cached_browser_engine = None
    _browser_engine_resolved = False
_cached_chromium_installed: Optional[bool] = None

def _chromium_search_roots() -> List[str]:
    """Directories to scan for a Chromium / headless-shell build.

    Order mirrors what agent-browser and Playwright actually probe:

    1. ``PLAYWRIGHT_BROWSERS_PATH`` when set (Docker image sets this to
       ``/opt/duck-agent/.playwright``).
    2. ``~/.cache/ms-playwright`` — Playwright's default on Linux/macOS.
    3. ``~/Library/Caches/ms-playwright`` — Playwright's default on macOS.
    4. ``%USERPROFILE%\\AppData\\Local\\ms-playwright`` — Playwright's default
       on Windows.
    """
    roots: List[str] = []
    env_path = os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '').strip()
    if env_path and env_path != '0':
        roots.append(env_path)
    home = os.path.expanduser('~')
    roots.append(os.path.join(home, '.cache', 'ms-playwright'))
    if sys.platform == 'darwin':
        roots.append(os.path.join(home, 'Library', 'Caches', 'ms-playwright'))
    if sys.platform == 'win32':
        local = os.environ.get('LOCALAPPDATA') or os.path.join(home, 'AppData', 'Local')
        roots.append(os.path.join(local, 'ms-playwright'))
    return roots

def _chromium_installed() -> bool:
    """Return True when a usable Chromium (or headless-shell) build is on disk.

    Checks, in order:

    1. ``AGENT_BROWSER_EXECUTABLE_PATH`` env var — the official way to point
       agent-browser at a pre-installed Chrome/Chromium.
    2. System Chrome/Chromium in PATH (``google-chrome``, ``chromium``,
       ``chromium-browser``, ``chrome``).
    3. Playwright's browser cache (current logic) — directories containing
       ``chromium-*`` or ``chromium_headless_shell-*``.

    agent-browser (0.26+) downloads Playwright's chromium / headless-shell
    builds into ``PLAYWRIGHT_BROWSERS_PATH`` and won't start without at least
    one of the three above being present.  Without a browser binary the CLI
    hangs on first use until the command timeout fires (often ~30s).  Guarding
    the tool behind this check prevents advertising a capability that will
    fail at runtime.
    """
    global _cached_chromium_installed
    if _cached_chromium_installed is not None:
        return _cached_chromium_installed
    ab_path = os.environ.get('AGENT_BROWSER_EXECUTABLE_PATH', '').strip()
    if ab_path:
        if os.path.isfile(ab_path) or shutil.which(ab_path):
            _cached_chromium_installed = True
            return True
    system_chrome = shutil.which('google-chrome') or shutil.which('chromium') or shutil.which('chromium-browser') or shutil.which('chrome')
    if system_chrome:
        _cached_chromium_installed = True
        return True
    for root in _chromium_search_roots():
        if not root or not os.path.isdir(root):
            continue
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for entry in entries:
            if entry.startswith('chromium-') or entry.startswith('chromium_headless_shell-'):
                _cached_chromium_installed = True
                return True
    _cached_chromium_installed = False
    return False
_chromium_autoinstall_attempted = False

def _maybe_autoinstall_chromium() -> bool:
    """Best-effort, gated download of the Chromium *binary* on local cold start.

    Closes the "the PR doesn't actually install the missing browser" gap for
    the common case — a Chromium binary that was simply never downloaded.
    Scope is deliberately narrow:

    - Binary only (``agent-browser install``), never ``--with-deps`` — that
      shells ``apt`` and needs root, so missing *system libraries* stay a user
      action (the timeout/blocked hints already point there).
    - Gated by ``security.allow_lazy_installs`` (same opt-out as every other
      lazy install) and skipped in Docker, where Chromium ships in the image.
    - Attempted once per process.

    Returns True only when Chromium is present afterwards.
    """
    global _chromium_autoinstall_attempted
    if _chromium_autoinstall_attempted:
        return _chromium_installed()
    _chromium_autoinstall_attempted = True
    if _running_in_docker():
        return False
    from tools.lazy_deps import _allow_lazy_installs
    if not _allow_lazy_installs():
        return False
    try:
        browser_cmd = _find_agent_browser()
    except FileNotFoundError:
        return False
    if browser_cmd == 'npx agent-browser':
        install_cmd = [shutil.which('npx') or 'npx', '-y', 'agent-browser', 'install']
    else:
        install_cmd = [browser_cmd, 'install']
    logger.info('browser: Chromium missing — auto-installing the browser binary (one-time ~170MB; disable via security.allow_lazy_installs)')
    try:
        proc = subprocess.run(install_cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=600, env=_build_browser_env())
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning('browser: Chromium auto-install failed to start: %s', e)
        return False
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or '').strip()[-300:]
        logger.warning('browser: Chromium auto-install exited %s: %s', proc.returncode, tail)
        return False
    global _cached_chromium_installed
    _cached_chromium_installed = None
    return _chromium_installed()

def _running_in_docker() -> bool:
    """Best-effort detection of whether we're inside a Docker container."""
    if os.path.exists('/.dockerenv'):
        return True
    try:
        with open('/proc/1/cgroup', 'rt', encoding='utf-8') as fp:
            return 'docker' in fp.read()
    except OSError:
        return False

def check_browser_requirements() -> bool:
    """
    Check if browser tool requirements are met.

    In **local mode** (no cloud provider configured): the ``agent-browser``
    CLI must be findable. Chrome/Chromium is required for the default Chrome
    engine and for fallback/screenshot paths, but not for Lightpanda-only text
    navigation/snapshot workflows.

    In **cloud mode** (Browserbase, Browser Use, or Firecrawl): the CLI
    and the provider's required credentials must be present. The cloud
    provider hosts its own Chromium, so no local browser binary is needed.

    Returns:
        True if all requirements are met, False otherwise
    """
    if _is_camofox_mode():
        return True
    if _get_cdp_override_raw():
        return True
    try:
        browser_cmd = _find_agent_browser(validate=False)
    except FileNotFoundError:
        return False
    if _requires_real_termux_browser_install(browser_cmd):
        return False
    provider = _get_cloud_provider()
    if provider is not None:
        return provider.is_configured()
    if _using_lightpanda_engine():
        return True
    if not _chromium_installed():
        return False
    return True

def check_browser_vision_requirements() -> bool:
    """Whether ``browser_vision`` should be advertised to the model.

    Requires BOTH a working browser (``check_browser_requirements``) AND a
    resolvable vision backend. Without the vision check, the tool stays in
    the model's tool list even when no vision provider is configured, then
    fails at call time with a cryptic provider-side error like
    ``unknown variant `image_url`, expected `text``` (issue #31179).
    """
    if not check_browser_requirements():
        return False
    try:
        from tools.vision_tools import check_vision_requirements
    except ImportError:
        return False
    return check_vision_requirements()
if __name__ == '__main__':
    '\n    Simple test/demo when run directly\n    '
    print('🌐 Browser Tool Module')
    print('=' * 40)
    _cp = _get_cloud_provider()
    mode = 'local' if _cp is None else f'cloud ({_cp.provider_name()})'
    print(f'   Mode: {mode}')
    if check_browser_requirements():
        print('✅ All requirements met')
    else:
        print('❌ Missing requirements:')
        try:
            browser_cmd = _find_agent_browser()
            if _requires_real_termux_browser_install(browser_cmd):
                print('   - bare npx fallback found (insufficient on Termux local mode)')
                print(f'     Install: {_browser_install_hint()}')
            elif _cp is None and (not _chromium_installed()):
                print('   - Chromium browser binary not found')
                searched = ', '.join(_chromium_search_roots()) or '(no candidate paths)'
                print(f'     Searched: {searched}')
                if _running_in_docker():
                    print('     Docker: pull the latest image — the current one predates the bundled Chromium install')
                    print('       docker pull ghcr.io/nousresearch/duck-agent:latest')
                else:
                    print('     Install it with:')
                    print('       npx agent-browser install --with-deps')
                    print('     Or:  npx playwright install --with-deps chromium')
        except FileNotFoundError:
            print('   - agent-browser CLI not found')
            print(f'     Install: {_browser_install_hint()}')
        if _cp is not None and (not _cp.is_configured()):
            print(f'   - {_cp.provider_name()} credentials not configured')
            print("   Tip: set browser.cloud_provider to 'local' to use free local mode instead")
    print('\n📋 Available Browser Tools:')
    for schema in BROWSER_TOOL_SCHEMAS:
        print(f"  🔹 {schema['name']}: {schema['description'][:60]}...")
    print('\n💡 Usage:')
    print('  from tools.browser_tool import browser_navigate, browser_snapshot')
    print("  result = browser_navigate('https://example.com', task_id='my_task')")
    print("  snapshot = browser_snapshot(task_id='my_task')")
from tools.registry import registry, tool_error
_BROWSER_SCHEMA_MAP = {s['name']: s for s in BROWSER_TOOL_SCHEMAS}
registry.register(name='browser_navigate', toolset='browser', schema=_BROWSER_SCHEMA_MAP['browser_navigate'], handler=lambda args, **kw: browser_navigate(url=args.get('url', ''), task_id=kw.get('task_id')), check_fn=check_browser_requirements, emoji='🌐')
registry.register(name='browser_snapshot', toolset='browser', schema=_BROWSER_SCHEMA_MAP['browser_snapshot'], handler=lambda args, **kw: browser_snapshot(full=args.get('full', False), task_id=kw.get('task_id'), user_task=kw.get('user_task')), check_fn=check_browser_requirements, emoji='📸')
registry.register(name='browser_click', toolset='browser', schema=_BROWSER_SCHEMA_MAP['browser_click'], handler=lambda args, **kw: browser_click(ref=args.get('ref', ''), task_id=kw.get('task_id')), check_fn=check_browser_requirements, emoji='👆')
registry.register(name='browser_type', toolset='browser', schema=_BROWSER_SCHEMA_MAP['browser_type'], handler=lambda args, **kw: browser_type(ref=args.get('ref', ''), text=args.get('text', ''), task_id=kw.get('task_id')), check_fn=check_browser_requirements, emoji='⌨️')
registry.register(name='browser_scroll', toolset='browser', schema=_BROWSER_SCHEMA_MAP['browser_scroll'], handler=lambda args, **kw: browser_scroll(direction=args.get('direction', 'down'), task_id=kw.get('task_id')), check_fn=check_browser_requirements, emoji='📜')
registry.register(name='browser_back', toolset='browser', schema=_BROWSER_SCHEMA_MAP['browser_back'], handler=lambda args, **kw: browser_back(task_id=kw.get('task_id')), check_fn=check_browser_requirements, emoji='◀️')
registry.register(name='browser_press', toolset='browser', schema=_BROWSER_SCHEMA_MAP['browser_press'], handler=lambda args, **kw: browser_press(key=args.get('key', ''), task_id=kw.get('task_id')), check_fn=check_browser_requirements, emoji='⌨️')
registry.register(name='browser_get_images', toolset='browser', schema=_BROWSER_SCHEMA_MAP['browser_get_images'], handler=lambda args, **kw: browser_get_images(task_id=kw.get('task_id')), check_fn=check_browser_requirements, emoji='🖼️')
registry.register(name='browser_vision', toolset='browser', schema=_BROWSER_SCHEMA_MAP['browser_vision'], handler=lambda args, **kw: browser_vision(question=args.get('question', ''), annotate=args.get('annotate', False), task_id=kw.get('task_id')), check_fn=check_browser_vision_requirements, emoji='👁️')
registry.register(name='browser_console', toolset='browser', schema=_BROWSER_SCHEMA_MAP['browser_console'], handler=lambda args, **kw: browser_console(clear=args.get('clear', False), expression=args.get('expression'), task_id=kw.get('task_id')), check_fn=check_browser_requirements, emoji='🖥️')