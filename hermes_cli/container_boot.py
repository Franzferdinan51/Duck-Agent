"""Container-boot reconciliation of per-profile gateway s6 services.

Service directories under /run/service/ live on **tmpfs** and are wiped
on every container restart. Profile directories under
``$DUCK_AGENT_HOME/profiles/<name>/`` live on the persistent VOLUME, and
each one records its gateway's last state in ``gateway_state.json``.
This module bridges the two: on every container boot, walk the
persistent profiles, recreate the s6 service slots, and auto-start
only those whose last recorded state was ``running``.

Wired into the image as /etc/cont-init.d/02-reconcile-profiles by the
Dockerfile (Phase 4 Task 4.0). Runs as root after 01-duck-agent-setup
(the stage2 hook) has chowned the volume and seeded $DUCK_AGENT_HOME, but
before s6-rc starts user services.

Without this module, every ``docker restart`` would silently wipe
every per-profile gateway, even though the user's profiles still
exist on disk.
"""
from __future__ import annotations
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence
log = logging.getLogger(__name__)
_AUTOSTART_STATES = frozenset({'running'})
_TRANSIENT_RUNNING_STATES = frozenset({'draining', 'degraded'})
_STALE_RUNTIME_FILES = ('gateway.pid', 'processes.json')
ReconcileActionLabel = Literal['started', 'registered', 'skipped']

@dataclass(frozen=True)
class ReconcileAction:
    """One profile's outcome from a single reconciliation pass."""
    profile: str
    prior_state: str | None
    action: ReconcileActionLabel
    prior_exit: str = 'unknown'

def reconcile_profile_gateways(*, hermes_home: Path, scandir: Path, dry_run: bool=False, container_argv: Sequence[str] | None=None) -> list[ReconcileAction]:
    """Recreate s6 service registrations for every persistent profile.

    Always registers a ``gateway-default`` slot for the root profile
    (the implicit profile that lives at the top of ``$DUCK_AGENT_HOME``,
    not under ``profiles/``). The dispatcher in ``hermes_cli.gateway``
    maps an empty profile suffix to ``gateway-default``, so this slot
    is what ``duck-agent gateway start`` (no ``-p``) targets. Without it,
    bare ``duck-agent gateway start`` inside the container would land on
    ``s6-svc -u /run/service/gateway-default`` → uncaught
    ``CalledProcessError`` → traceback to the user (PR #30136 review).

    The default slot's prior state is read from
    ``$DUCK_AGENT_HOME/gateway_state.json`` (sibling to the profile root,
    not under ``profiles/``); stale runtime files there are swept the
    same way as for named profiles.

    Args:
        duck_agent_home: The container's DUCK_AGENT_HOME (typically /opt/data).
            Profiles live under ``<duck_agent_home>/profiles/<name>/``;
            the default profile lives at ``<duck_agent_home>`` itself.
        scandir: The s6 dynamic scandir (typically /run/service). Service
            directories are created at ``<scandir>/gateway-<profile>/``.
        dry_run: When True, walk and return the action list without
            touching the filesystem. For tests and `--dry-run` debug.
        container_argv: Optional container PID 1 argv override. Production
            reads ``/proc/1/cmdline``; tests inject it directly.

    Returns:
        One :class:`ReconcileAction` per profile, in this order:
        ``default`` first, then named profiles in directory order.
    """
    actions: list[ReconcileAction] = []
    from utils import is_truthy_value
    multiplex_profiles = is_truthy_value(os.environ.get('GATEWAY_MULTIPLEX_PROFILES'))
    legacy_default_state = _maybe_migrate_legacy_gateway_run_state(hermes_home, container_argv=container_argv, dry_run=dry_run)
    default_prior_state = legacy_default_state or _read_desired_state(hermes_home)
    default_should_start = default_prior_state in _AUTOSTART_STATES
    if not dry_run:
        _cleanup_stale_runtime_files(hermes_home)
        _register_service(scandir, 'default', start=default_should_start)
    actions.append(ReconcileAction(profile='default', prior_state=default_prior_state, action='started' if default_should_start else 'registered', prior_exit=_read_prior_exit_label(hermes_home)))
    profiles_root = hermes_home / 'profiles'
    if profiles_root.is_dir():
        for entry in sorted(profiles_root.iterdir()):
            if not entry.is_dir():
                continue
            if not (entry / 'SOUL.md').exists():
                continue
            if entry.name == 'default':
                log.warning('profiles/default/ exists — skipping to avoid colliding with the reserved root-profile s6 slot')
                continue
            prior_state = _read_desired_state(entry)
            should_start = not multiplex_profiles and prior_state in _AUTOSTART_STATES
            if not dry_run:
                _cleanup_stale_runtime_files(entry)
                _register_service(scandir, entry.name, start=should_start)
            actions.append(ReconcileAction(profile=entry.name, prior_state=prior_state, action='started' if should_start else 'registered', prior_exit=_read_prior_exit_label(entry)))
    if not dry_run:
        _write_reconcile_log(hermes_home, actions)
    return actions

def _maybe_migrate_legacy_gateway_run_state(hermes_home: Path, *, container_argv: Sequence[str] | None, dry_run: bool) -> str | None:
    """Seed root gateway_state for pre-s6 `gateway run` containers.

    The tini image let Docker users run the gateway as the container
    command (`docker run ... gateway run`). After the s6 migration,
    profile gateways are restored from persisted gateway_state.json; a
    legacy container with no state file would therefore register the
    default service down and never start. Only synthesize state when no
    root gateway_state.json exists so explicit stopped/failed states keep
    winning across restarts.
    """
    state_file = hermes_home / 'gateway_state.json'
    if state_file.exists():
        return None
    if os.environ.get('HERMES_GATEWAY_NO_SUPERVISE', '').lower() in ('1', 'true', 'yes'):
        return None
    argv = tuple(container_argv) if container_argv is not None else _read_container_argv()
    if not _is_legacy_gateway_run_request(argv):
        return None
    if not dry_run:
        import time
        state_file.write_text(json.dumps({'gateway_state': 'running', 'desired_state': 'running', 'timestamp': int(time.time()), 'migrated_from': 'legacy-container-cmd'}) + '\n', encoding='utf-8')
    return 'running'

def _read_container_argv() -> tuple[str, ...]:
    """Best-effort read of the container's main program argv.

    Under s6-overlay v2, PID 1 is ``/init`` and its argv contains the
    ``main-wrapper.sh`` path.  Under s6-overlay v3, PID 1 is
    ``s6-svscan`` and the actual command (``rc.init top main-wrapper.sh
    ...``) lives on a different PID.  We try PID 1 first (fast path,
    covers v2 and pre-s6 images), then fall back to scanning
    ``/proc/*/cmdline`` for a process whose argv contains
    ``main-wrapper.sh`` (the rc.init-launched PID in v3).
    """
    try:
        raw = Path('/proc/1/cmdline').read_bytes()
        argv = tuple((part.decode('utf-8', 'replace') for part in raw.split(b'\x00') if part))
        if any(('main-wrapper.sh' in part for part in argv)):
            return argv
    except OSError:
        pass
    try:
        proc_dir = Path('/proc')
        for entry in proc_dir.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                raw = (entry / 'cmdline').read_bytes()
            except OSError:
                continue
            argv = tuple((part.decode('utf-8', 'replace') for part in raw.split(b'\x00') if part))
            if any(('main-wrapper.sh' in part for part in argv)):
                return argv
    except OSError:
        pass
    return ()

def _strip_container_argv_prefix(argv: Sequence[str]) -> list[str]:
    """Strip the s6/wrapper prefix off the container argv, leaving the duck-agent args.

    Two container-command argv shapes are handled:

    * **s6-overlay v2 / tini:** PID 1 argv is
      ``/init /opt/duck-agent/docker/main-wrapper.sh <subcommand> [args...]``.
    * **s6-overlay v3:** PID 1 is ``s6-svscan`` and the command lives on the
      rc.init-launched process as ``/bin/sh -e
      /run/s6/basedir/scripts/rc.init top /opt/duck-agent/docker/main-wrapper.sh
      <subcommand> [args...]`` (see :func:`_read_container_argv`).

    Rather than peel each leading token positionally (which silently breaks
    the moment s6 changes its launcher shape again — exactly what happened
    in the v2→v3 bump), drop everything up to and including the
    ``main-wrapper.sh`` token: that wrapper path is the stable boundary the
    image owns, and the subcommand always follows it. Pre-s6 / direct
    ``duck-agent`` invocations carry no wrapper, so fall back to peeling a bare
    ``init`` prefix. The wrapper re-execs ``duck-agent <subcommand>``, so an
    explicit leading ``duck-agent`` is peeled too. Shared by the legacy-gateway
    and dashboard role detectors.
    """
    args = list(argv)
    wrapper_idx = next((i for i, a in enumerate(args) if a.endswith('main-wrapper.sh')), None)
    if wrapper_idx is not None:
        args = args[wrapper_idx + 1:]
    elif args and Path(args[0]).name == 'init':
        args = args[1:]
    if args and args[0].endswith('entrypoint-dispatch.sh'):
        args = args[1:]
    if args and Path(args[0]).name == 'duck-agent':
        args = args[1:]
    return args

def _is_legacy_gateway_run_request(argv: Sequence[str]) -> bool:
    """Return True for Docker commands equivalent to `gateway run`."""
    args = _strip_container_argv_prefix(argv)
    if '--no-supervise' in args:
        return False
    return len(args) >= 2 and args[0] == 'gateway' and (args[1] == 'run')

def _is_dashboard_container(argv: Sequence[str]) -> bool:
    """Return True when the container's command is the dashboard.

    A dashboard-only container (``duck-agent dashboard ...``) never spawns or
    supervises per-profile gateways — that is the gateway container's job.
    Reconciling profile gateway s6 slots there is not just wasted work: when
    the gateway and dashboard containers share a bind-mounted DUCK_AGENT_HOME,
    both race to ``flock()`` the same ``logs/gateways/<profile>/lock`` files,
    producing "Resource busy" failures and an s6-log restart storm. So the
    dashboard container skips reconciliation entirely.

    Detected from PID 1 argv (``/proc/1/cmdline``) rather than an operator
    flag: the role is a fact about the container's command, not a tunable,
    and a flag can be forgotten in a hand-written compose/k8s manifest —
    reintroducing the exact storm this prevents. Mirrors the argv handling
    in :func:`_is_legacy_gateway_run_request`.
    """
    args = _strip_container_argv_prefix(argv)
    return bool(args) and args[0] == 'dashboard'

def _read_desired_state(profile_dir: Path) -> str | None:
    """Read the persisted gateway desired state for reconciliation.

    Newer state files carry ``desired_state``: operator intent written by
    s6 lifecycle commands. Older files only carry ``gateway_state``; keep
    that as a compatibility fallback so existing running/stopped profiles
    preserve their behavior until the next explicit start/stop.

    When falling back to ``gateway_state`` (no explicit ``desired_state``),
    a transient running sub-state (``draining``) is normalised to ``running``
    — see ``_TRANSIENT_RUNNING_STATES``. A gateway hard-killed mid-drain
    leaves ``draining`` as its last persisted value; without this it would be
    treated as a non-autostart state and the gateway would stay DOWN forever.
    An explicit ``desired_state`` is always honoured verbatim (it is the
    operator's durable intent), so this normalisation only affects the
    legacy/transient fallback path.

    Missing or unparseable files count as "no desired state" so we don't
    bork the whole reconciliation on a corrupt file.
    """
    state_file = profile_dir / 'gateway_state.json'
    if not state_file.exists():
        return None
    try:
        data = json.loads(state_file.read_text(encoding='utf-8'))
        desired_state = data.get('desired_state')
        if desired_state is not None:
            return desired_state
        gateway_state = data.get('gateway_state')
        if gateway_state in _TRANSIENT_RUNNING_STATES:
            return 'running'
        return gateway_state
    except (OSError, json.JSONDecodeError):
        log.warning('could not read %s; treating as no prior state', state_file)
        return None

def _cleanup_stale_runtime_files(profile_dir: Path) -> None:
    """Remove gateway.pid and processes.json — they reference PIDs in
    the dead container's process namespace and would otherwise confuse
    the newly-started gateway's process-mismatch checks."""
    for name in _STALE_RUNTIME_FILES:
        (profile_dir / name).unlink(missing_ok=True)

def _read_prior_exit_label(profile_dir: Path) -> str:
    """How the profile's previous gateway life ended (clean/unclean/unknown).

    Thin, exception-free wrapper over
    :func:`gateway.lifecycle_ledger.read_prior_exit_label` — cont-init runs
    in a minimal environment and forensics must never block reconciliation
    (NS-608)."""
    try:
        from gateway.lifecycle_ledger import read_prior_exit_label
        return read_prior_exit_label(profile_dir)
    except Exception:
        return 'unknown'

def _register_service(scandir: Path, profile: str, *, start: bool) -> None:
    """Recreate the s6 service slot for one profile.

    Mirrors the rendering in :func:`S6ServiceManager.register_profile_gateway`,
    but here we control the start state directly via the ``down`` marker
    file (s6-svscan honors it on rescan). Cannot use the manager
    directly because the cont-init.d phase runs as root before
    s6-svscan starts scanning the dynamic scandir — the manager's
    ``s6-svscanctl -a`` call would fail with no control socket.

    Atomicity: build the new layout in a sibling temp directory and
    rename it into place via :meth:`Path.replace`. This matches
    :meth:`S6ServiceManager.register_profile_gateway` (PR #30136
    review item O4) — even though cont-init.d runs before s6-svscan
    starts scanning, an atomic publication keeps the contract uniform
    between the two registration paths and protects against a
    half-populated dir if the script is interrupted mid-write.
    """
    import shutil
    from hermes_cli.service_manager import S6ServiceManager, _seed_supervise_skeleton, validate_profile_name
    validate_profile_name(profile)
    service_dir = scandir / f'gateway-{profile}'
    tmp_dir = service_dir.with_name('.' + service_dir.name + '.tmp')
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True)
    try:
        (tmp_dir / 'type').write_text('longrun\n', encoding='utf-8')
        run = tmp_dir / 'run'
        run.write_text(S6ServiceManager._render_run_script(profile, extra_env={}), encoding='utf-8')
        run.chmod(493)
        finish = tmp_dir / 'finish'
        finish.write_text(S6ServiceManager._render_finish_script(), encoding='utf-8')
        finish.chmod(493)
        log_subdir = tmp_dir / 'log'
        log_subdir.mkdir()
        log_run = log_subdir / 'run'
        log_run.write_text(S6ServiceManager._render_log_run(profile), encoding='utf-8')
        log_run.chmod(493)
        if not start:
            (tmp_dir / 'down').touch()
        _seed_supervise_skeleton(tmp_dir)
        if service_dir.exists():
            shutil.rmtree(service_dir)
        tmp_dir.replace(service_dir)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

def _write_reconcile_log(hermes_home: Path, actions: list[ReconcileAction]) -> None:
    """Append one line per profile to $DUCK_AGENT_HOME/logs/container-boot.log.

    Operators inspect this to debug "why didn't my profile come back
    up". Keeping a separate log file (vs. mixing into agent.log) lets
    troubleshooters grep for "profile=foo" without wading through
    unrelated activity.

    Size-bounded: when the file exceeds ``_LOG_ROTATE_BYTES``
    (defaults to 256 KiB ≈ 3000 reconcile lines), the current file
    is renamed to ``container-boot.log.1`` (replacing any previous
    rotation) before the new entries are appended. This gives long-
    lived containers a soft cap of ~512 KiB across the two files
    without pulling in logrotate or s6-log machinery just for this
    one append-only file (PR #30136 review item O3).
    """
    import time
    log_dir = hermes_home / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / 'container-boot.log'
    try:
        if log_path.exists() and log_path.stat().st_size >= _LOG_ROTATE_BYTES:
            log_path.replace(log_dir / 'container-boot.log.1')
    except OSError as exc:
        log.warning('could not rotate %s: %s', log_path, exc)
    ts = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    with log_path.open('a', encoding='utf-8') as f:
        for a in actions:
            f.write(f'{ts} profile={a.profile} prior_state={a.prior_state} action={a.action} prior_exit={a.prior_exit}\n')
_LOG_ROTATE_BYTES = 256 * 1024

def main() -> int:
    """Entry point invoked from /etc/cont-init.d/02-reconcile-profiles."""
    if _is_dashboard_container(_read_container_argv()):
        print('reconcile: skipping (dashboard container — does not need per-profile gateways)')
        return 0
    hermes_home = Path(os.environ.get('DUCK_AGENT_HOME', '/opt/data'))
    scandir = Path(os.environ.get('S6_PROFILE_GATEWAY_SCANDIR', '/run/service'))
    actions = reconcile_profile_gateways(hermes_home=hermes_home, scandir=scandir)
    for a in actions:
        print(f'reconcile: profile={a.profile} prior_state={a.prior_state} action={a.action}')
    return 0
if __name__ == '__main__':
    raise SystemExit(main())