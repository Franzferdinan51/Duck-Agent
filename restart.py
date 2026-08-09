"""Shared gateway restart constants and supervisor detection helpers."""
import os
from collections.abc import Mapping
from hermes_cli.config import DEFAULT_CONFIG
GATEWAY_SERVICE_RESTART_EXIT_CODE = 75
GATEWAY_FATAL_CONFIG_EXIT_CODE = 78
EXTERNAL_GATEWAY_SUPERVISOR_ENV = 'HERMES_GATEWAY_EXTERNAL_SUPERVISOR'
DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT = float(DEFAULT_CONFIG['agent']['restart_drain_timeout'])
DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT = float(DEFAULT_CONFIG['agent']['restart_after_turn_timeout'])

def is_gateway_supervisor_process(environ: Mapping[str, str] | None=None) -> bool:
    """Return whether this gateway process is owned by a supervisor."""
    env = os.environ if environ is None else environ
    if env.get('INVOCATION_ID'):
        return True
    if env.get('HERMES_S6_SUPERVISED_CHILD'):
        return True
    xpc_service = env.get('XPC_SERVICE_NAME', '')
    if xpc_service and xpc_service != '0':
        return True
    return str(env.get(EXTERNAL_GATEWAY_SUPERVISOR_ENV, '')).strip().lower() in {'1', 'true', 'yes', 'on'}

def is_container_restart_context() -> bool:
    """Return whether the gateway is running inside a container for restart
    routing purposes (Docker/Podman ⇒ the detached setsid path dies with the
    cgroup; exit-75 service restart is the only viable path).

    Extracted from the inline probe in the /restart handler so tests can mock
    container detection hermetically — a real ``/.dockerenv`` on a
    containerized CI runner otherwise flips the routing under the test.
    """
    return os.path.exists('/.dockerenv') or os.path.exists('/run/.containerenv')

def parse_restart_drain_timeout(raw: object) -> float:
    """Parse a configured drain timeout, falling back to the shared default."""
    try:
        value = float(raw) if str(raw or '').strip() else DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
    except (TypeError, ValueError):
        return DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
    return max(0.0, value)

def parse_restart_after_turn_timeout(raw: object) -> float:
    """Parse the after-turn wait cap for in-band restart, falling back to default.

    ``0`` is a deliberate disable (legacy immediate drain) and must not fall
    through to the default — unlike empty/missing input.
    """
    if raw is None:
        return DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT
    if isinstance(raw, str) and (not raw.strip()):
        return DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT
    return max(0.0, value)

def resolve_restart_exit_wait_budget(drain_timeout: float, after_turn_timeout: float, *, headroom: float=15.0) -> float:
    """Seconds a CLI should wait for the gateway PID to exit after SIGUSR1.

    In-band restart may defer ``stop()`` until active turns finish
    (``after_turn_timeout``) and then spend up to ``drain_timeout`` inside
    ``stop()``. Callers that fall back to a hard kill on wait expiry must
    cover both phases or they reintroduce #77184.
    """
    try:
        drain = max(float(drain_timeout), 0.0)
    except (TypeError, ValueError):
        drain = 0.0
    try:
        after_turn = max(float(after_turn_timeout), 0.0)
    except (TypeError, ValueError):
        after_turn = 0.0
    try:
        margin = max(float(headroom), 0.0)
    except (TypeError, ValueError):
        margin = 0.0
    return drain + after_turn + margin