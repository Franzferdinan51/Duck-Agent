"""``duck-agent debug`` debug tools for Duck Agent.

Currently supports:
    duck-agent debug share    Upload debug report (system info + logs) to a
                          paste service and print a shareable URL.
                          By default, log content is run through
                          ``agent.redact.redact_sensitive_text`` with
                          ``force=True`` before upload so credentials in
                          ``~/.duck-agent/logs/*.log`` are not leaked into
                          the public paste service. Pass ``--no-redact``
                          to disable.
                          Pass ``--nous`` to upload instead to Nous-internal
                          storage (AWS S3) via a signed URL minted by the
                          Nous account service: the bundle is private
                          (viewable only by Nous staff / allowlisted mods via
                          a Google-login-gated viewer) and auto-deletes after
                          14 days, rather than going to a public paste.
"""
import datetime
import gzip
import io
import json
import logging
import re
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from hermes_constants import get_hermes_home
from utils import atomic_replace
logger = logging.getLogger(__name__)
_REDACTION_BANNER = '[duck-agent debug share: log content redacted at upload time. run with --no-redact to disable]\n'
_EMAIL_ADDRESS_RE = re.compile('(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}(?![A-Za-z0-9._%+-])')
_PASTE_RS_URL = 'https://paste.rs/'
_DPASTE_COM_URL = 'https://dpaste.com/api/'
_MAX_LOG_BYTES = 512000
_AUTO_DELETE_SECONDS = 21600

def _pending_file() -> Path:
    """Path to ``~/.duck-agent/pastes/pending.json``.

    Each entry: ``{"url": "...", "expire_at": <unix_ts>}``.  Scheduled
    DELETEs used to be handled by spawning a detached Python process per
    paste that slept for 6 hours; those accumulated forever if the user
    ran ``duck-agent debug share`` repeatedly.

    Deletion is now driven by the gateway's cron ticker
    (``gateway/run.py::_start_cron_ticker``) which calls
    ``_sweep_expired_pastes`` once per hour.  ``duck-agent debug share`` also
    runs an opportunistic sweep on entry as a fallback for CLI-only users
    who never start the gateway.
    """
    return get_hermes_home() / 'pastes' / 'pending.json'

def _load_pending() -> list[dict]:
    path = _pending_file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(data, list):
            return [e for e in data if isinstance(e, dict) and 'url' in e and ('expire_at' in e)]
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return []

def _save_pending(entries: list[dict]) -> None:
    path = _pending_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(entries, indent=2), encoding='utf-8')
        atomic_replace(tmp, path)
    except OSError:
        pass

def _record_pending(urls: list[str], delay_seconds: int=_AUTO_DELETE_SECONDS) -> None:
    """Record *urls* for deletion at ``now + delay_seconds``.

    Only paste.rs URLs are recorded (dpaste.com auto-expires).  Entries
    are merged into any existing pending.json.
    """
    paste_rs_urls = [u for u in urls if _extract_paste_id(u)]
    if not paste_rs_urls:
        return
    entries = _load_pending()
    by_url: dict[str, float] = {e['url']: float(e['expire_at']) for e in entries}
    expire_at = time.time() + delay_seconds
    for u in paste_rs_urls:
        by_url[u] = max(expire_at, by_url.get(u, 0.0))
    merged = [{'url': u, 'expire_at': ts} for u, ts in by_url.items()]
    _save_pending(merged)

def _sweep_expired_pastes(now: Optional[float]=None) -> tuple[int, int]:
    """Synchronously DELETE any pending pastes whose ``expire_at`` has passed.

    Returns ``(deleted, remaining)``.  Best-effort: failed deletes stay in
    the pending file and will be retried on the next sweep.  Silent —
    intended to be called from every ``duck-agent debug`` invocation with
    minimal noise.
    """
    entries = _load_pending()
    if not entries:
        return (0, 0)
    current = time.time() if now is None else now
    deleted = 0
    remaining: list[dict] = []
    for entry in entries:
        try:
            expire_at = float(entry.get('expire_at', 0))
        except (TypeError, ValueError):
            continue
        if expire_at > current:
            remaining.append(entry)
            continue
        url = entry.get('url', '')
        try:
            if delete_paste(url):
                deleted += 1
                continue
        except Exception:
            pass
        if expire_at + 86400 > current:
            remaining.append(entry)
        else:
            deleted += 1
    if deleted:
        _save_pending(remaining)
    return (deleted, len(remaining))

def _best_effort_sweep_expired_pastes() -> None:
    """Attempt pending-paste cleanup without letting /debug fail offline."""
    try:
        _sweep_expired_pastes()
    except Exception:
        pass
_PRIVACY_NOTICE = '⚠️  This will upload system info + logs to a PUBLIC paste service.\n\nCryptographic secrets (API keys, tokens, passwords) are redacted before\nupload, but the following personal data is NOT redacted and will be public:\n  • Your display name and persistent platform user ID\n  • Verbatim content of your recent messages (prompts, responses, tool output)\n  • Local filesystem paths\n  • Any other PII present in the logs\n\nThe resulting URL is public to anyone who has the link. Pastes auto-delete\nafter 6 hours, but may be archived by third parties in the meantime.\n\nUse --local to view the report without uploading.\n'
_GATEWAY_PRIVACY_NOTICE = '⚠️ **Privacy notice:** This uploads system info + recent log tails (may contain conversation fragments) to a public paste service. Full logs are NOT included from the gateway — use `duck-agent debug share` from the CLI for full log uploads.\nPastes auto-delete after 6 hours.'

def _extract_paste_id(url: str) -> Optional[str]:
    """Extract the paste ID from a paste.rs or dpaste.com URL.

    Returns the ID string, or None if the URL doesn't match a known service.
    """
    url = url.strip().rstrip('/')
    for prefix in ('https://paste.rs/', 'http://paste.rs/'):
        if url.startswith(prefix):
            return url[len(prefix):]
    return None

def delete_paste(url: str) -> bool:
    """Delete a paste from paste.rs.  Returns True on success.

    Only paste.rs supports unauthenticated DELETE.  dpaste.com pastes
    expire automatically but cannot be deleted via API.
    """
    paste_id = _extract_paste_id(url)
    if not paste_id:
        raise ValueError(f'Cannot delete: only paste.rs URLs are supported.  Got: {url}')
    target = f'{_PASTE_RS_URL}{paste_id}'
    req = urllib.request.Request(target, method='DELETE', headers={'User-Agent': 'duck-agent/debug-share'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return 200 <= resp.status < 300

def _schedule_auto_delete(urls: list[str], delay_seconds: int=_AUTO_DELETE_SECONDS):
    """Record *urls* for deletion ``delay_seconds`` from now.

    Previously this spawned a detached Python subprocess per call that slept
    for 6 hours and then issued DELETE requests.  Those subprocesses leaked —
    every ``duck-agent debug share`` invocation added ~20 MB of resident Python
    interpreters that never exited until the sleep completed.

    The replacement is stateless: we append to ``~/.duck-agent/pastes/pending.json``
    and the gateway's cron ticker sweeps expired entries once per hour.
    ``duck-agent debug share`` also runs an opportunistic sweep as a fallback
    for CLI-only users.  If neither runs again, paste.rs's own retention
    policy handles cleanup.
    """
    _record_pending(urls, delay_seconds=delay_seconds)

def _upload_paste_rs(content: str) -> str:
    """Upload to paste.rs.  Returns the paste URL.

    paste.rs accepts a plain POST body and returns the URL directly.
    """
    data = content.encode('utf-8')
    req = urllib.request.Request(_PASTE_RS_URL, data=data, method='POST', headers={'Content-Type': 'text/plain; charset=utf-8', 'User-Agent': 'duck-agent/debug-share'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        url = resp.read().decode('utf-8').strip()
    if not url.startswith('http'):
        raise ValueError(f'Unexpected response from paste.rs: {url[:200]}')
    return url

def _upload_dpaste_com(content: str, expiry_days: int=7) -> str:
    """Upload to dpaste.com.  Returns the paste URL.

    dpaste.com uses multipart form data.
    """
    boundary = '----HermesDebugBoundary9f3c'

    def _field(name: str, value: str) -> str:
        return f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
    body = (_field('content', content) + _field('syntax', 'text') + _field('expiry_days', str(expiry_days)) + f'--{boundary}--\r\n').encode('utf-8')
    req = urllib.request.Request(_DPASTE_COM_URL, data=body, method='POST', headers={'Content-Type': f'multipart/form-data; boundary={boundary}', 'User-Agent': 'duck-agent/debug-share'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        url = resp.read().decode('utf-8').strip()
    if not url.startswith('http'):
        raise ValueError(f'Unexpected response from dpaste.com: {url[:200]}')
    return url

def upload_to_pastebin(content: str, expiry_days: int=7) -> str:
    """Upload *content* to a paste service, trying paste.rs then dpaste.com.

    Returns the paste URL on success, raises on total failure.
    """
    errors: list[str] = []
    try:
        return _upload_paste_rs(content)
    except Exception as exc:
        errors.append(f'paste.rs: {exc}')
    try:
        return _upload_dpaste_com(content, expiry_days=expiry_days)
    except Exception as exc:
        errors.append(f'dpaste.com: {exc}')
    raise RuntimeError('Failed to upload to any paste service:\n  ' + '\n  '.join(errors))

@dataclass
class LogSnapshot:
    """Single-read snapshot of a log file used by debug-share."""
    path: Optional[Path]
    tail_text: str
    full_text: Optional[str]

def _primary_log_path(log_name: str) -> Optional[Path]:
    """Where *log_name* would live if present. Doesn't check existence."""
    from hermes_cli.logs import LOG_FILES
    filename = LOG_FILES.get(log_name)
    return get_hermes_home() / 'logs' / filename if filename else None
_CLIENT_SIDE_LOGS = {'desktop': 'written by Duck Agent Desktop on the machine running the app, not by this backend. If the desktop connects to a remote/docker/SSH backend, collect it on that client machine'}

def _missing_log_note(log_name: str) -> str:
    """Explain a missing log instead of stating a bare absence.

    For a client-side log the absence is expected on a remote backend, so the
    note names the writer and the path to collect by hand.
    """
    reason = _CLIENT_SIDE_LOGS.get(log_name)
    if reason is None:
        return '(file not found)'
    primary = _primary_log_path(log_name)
    where = f' — expected at {primary}' if primary else ''
    return f'(not on this host: {reason}{where})'

def _resolve_log_path(log_name: str) -> Optional[Path]:
    """Find the log file for *log_name*, falling back to the .1 rotation.

    Returns the first non-empty candidate (primary, then .1), or None.
    Callers distinguish 'empty primary' from 'truly missing' via
    :func:`_primary_log_path`.
    """
    primary = _primary_log_path(log_name)
    if primary is None:
        return None
    if primary.exists() and primary.stat().st_size > 0:
        return primary
    rotated = primary.parent / f'{primary.name}.1'
    if rotated.exists() and rotated.stat().st_size > 0:
        return rotated
    return None

def _redact_log_text(text: str) -> str:
    """Run ``redact_sensitive_text`` with ``force=True`` over upload-bound text.

    Uses ``force=True`` so redaction fires regardless of the operator's
    ``security.redact_secrets`` setting. The local on-disk log file is
    not modified; only the in-memory copy headed for the public paste
    service is sanitized. Returns the redacted text (or the original
    when empty / non-string).
    """
    if not text:
        return text
    from agent.redact import redact_sensitive_text
    text = redact_sensitive_text(text, force=True)
    return _EMAIL_ADDRESS_RE.sub('[REDACTED_EMAIL]', text)

def _capture_log_snapshot(log_name: str, *, tail_lines: int, max_bytes: int=_MAX_LOG_BYTES, redact: bool=True) -> LogSnapshot:
    """Capture a log once and derive summary/full-log views from it.

    The report tail and standalone log upload must come from the same file
    snapshot. Otherwise a rotation/truncate between reads can make the report
    look newer than the uploaded ``agent.log`` paste.

    When ``redact`` is True (the default), both ``tail_text`` and
    ``full_text`` are run through ``_redact_log_text`` so the snapshot
    returned is upload-safe. The on-disk log file is never modified.
    Pass ``redact=False`` to capture original log content (used by
    ``duck-agent debug share --no-redact``).
    """
    log_path = _resolve_log_path(log_name)
    if log_path is None:
        primary = _primary_log_path(log_name)
        tail = '(file empty)' if primary and primary.exists() else _missing_log_note(log_name)
        return LogSnapshot(path=None, tail_text=tail, full_text=None)
    try:
        size = log_path.stat().st_size
        if size == 0:
            return LogSnapshot(path=log_path, tail_text='(file empty)', full_text=None)
        with open(log_path, 'rb') as f:
            if size <= max_bytes:
                raw = f.read()
                truncated = False
            else:
                chunk_size = 8192
                pos = size
                chunks: list[bytes] = []
                total = 0
                newline_count = 0
                while pos > 0 and (total < max_bytes or newline_count <= tail_lines + 1) and (total < max_bytes * 2):
                    read_size = min(chunk_size, pos)
                    pos -= read_size
                    f.seek(pos)
                    chunk = f.read(read_size)
                    chunks.insert(0, chunk)
                    total += len(chunk)
                    newline_count += chunk.count(b'\n')
                    chunk_size = min(chunk_size * 2, 65536)
                raw = b''.join(chunks)
                truncated = pos > 0
        full_raw = raw
        if truncated and len(full_raw) > max_bytes:
            cut = len(full_raw) - max_bytes
            on_boundary = cut > 0 and full_raw[cut - 1:cut] == b'\n'
            full_raw = full_raw[cut:]
            if not on_boundary and b'\n' in full_raw:
                full_raw = full_raw.split(b'\n', 1)[1]
        all_text = raw.decode('utf-8', errors='replace')
        tail_text = ''.join(all_text.splitlines(keepends=True)[-tail_lines:]).rstrip('\n')
        full_text = full_raw.decode('utf-8', errors='replace')
        if truncated:
            full_text = f'[... truncated — showing last ~{max_bytes // 1024}KB ...]\n{full_text}'
        if redact:
            tail_text = _redact_log_text(tail_text)
            full_text = _redact_log_text(full_text)
        return LogSnapshot(path=log_path, tail_text=tail_text, full_text=full_text)
    except Exception as exc:
        return LogSnapshot(path=log_path, tail_text=f'(error reading: {exc})', full_text=None)

def _capture_default_log_snapshots(log_lines: int, *, redact: bool=True) -> dict[str, LogSnapshot]:
    """Capture all logs used by debug-share exactly once.

    ``redact`` is forwarded to each ``_capture_log_snapshot`` call so all
    captured logs share the same redaction policy for a given run.
    """
    errors_lines = min(log_lines, 100)
    return {'agent': _capture_log_snapshot('agent', tail_lines=log_lines, redact=redact), 'errors': _capture_log_snapshot('errors', tail_lines=errors_lines, redact=redact), 'gateway': _capture_log_snapshot('gateway', tail_lines=errors_lines, redact=redact), 'gui': _capture_log_snapshot('gui', tail_lines=errors_lines, redact=redact), 'desktop': _capture_log_snapshot('desktop', tail_lines=errors_lines, redact=redact)}

def _capture_dump() -> str:
    """Run ``duck-agent dump`` and return its stdout as a string."""
    from hermes_cli.dump import run_dump

    class _FakeArgs:
        show_keys = False
    old_stdout = sys.stdout
    sys.stdout = capture = io.StringIO()
    try:
        run_dump(_FakeArgs())
    except SystemExit:
        pass
    finally:
        sys.stdout = old_stdout
    return capture.getvalue()

def collect_debug_report(*, log_lines: int=200, dump_text: str='', log_snapshots: Optional[dict[str, LogSnapshot]]=None) -> str:
    """Build the summary debug report: system dump + log tails.

    Parameters
    ----------
    log_lines
        Number of recent lines to include per log file.
    dump_text
        Pre-captured dump output.  If empty, ``duck-agent dump`` is run
        internally.

    Returns the report as a plain-text string ready for upload.
    """
    buf = io.StringIO()
    if not dump_text:
        dump_text = _capture_dump()
    buf.write(dump_text)
    if log_snapshots is None:
        log_snapshots = _capture_default_log_snapshots(log_lines)
    buf.write('\n\n')
    buf.write(f'--- agent.log (last {log_lines} lines) ---\n')
    buf.write(log_snapshots['agent'].tail_text)
    buf.write('\n\n')
    errors_lines = min(log_lines, 100)
    buf.write(f'--- errors.log (last {errors_lines} lines) ---\n')
    buf.write(log_snapshots['errors'].tail_text)
    buf.write('\n\n')
    buf.write(f'--- gateway.log (last {errors_lines} lines) ---\n')
    buf.write(log_snapshots['gateway'].tail_text)
    buf.write('\n\n')
    buf.write(f'--- gui.log (last {errors_lines} lines) ---\n')
    buf.write(log_snapshots['gui'].tail_text)
    buf.write('\n\n')
    buf.write(f'--- desktop.log (last {errors_lines} lines) ---\n')
    buf.write(log_snapshots['desktop'].tail_text)
    buf.write('\n')
    return buf.getvalue()
_NOUS_BUNDLE_FORMAT = 'duck-agent-debug-share/1'

def collect_share_bundle(log_lines: int=200, redact: bool=True) -> dict[str, str]:
    """Collect the debug report + full logs as a label→text mapping.

    Returns ``{"report": ..., "agent.log": ..., "gateway.log": ...,
    "desktop.log": ...}`` where each value is the already-redacted (when
    ``redact`` is True) text that would be uploaded.  Keys for logs that are
    absent/empty are simply omitted.

    This is the single source of collection + redaction shared by both
    destinations: the paste.rs path (:func:`build_debug_share`) and the
    Nous-S3 path (``--nous``).  Centralising it guarantees the Nous bundle is
    built from the *same* force-redacted snapshots as the public paste path —
    redaction is the safety boundary, so the Nous path must never see raw
    logs.

    The dump header is prepended to each full log (mirroring the historical
    paste behaviour) so every file is self-contained, and the redaction
    banner is prepended when ``redact`` is True.
    """
    dump_text = _capture_dump()
    log_snapshots = _capture_default_log_snapshots(log_lines, redact=redact)
    report = collect_debug_report(log_lines=log_lines, dump_text=dump_text, log_snapshots=log_snapshots)
    agent_log = log_snapshots['agent'].full_text
    gateway_log = log_snapshots['gateway'].full_text
    gui_log = log_snapshots['gui'].full_text
    desktop_log = log_snapshots['desktop'].full_text
    if agent_log:
        agent_log = dump_text + '\n\n--- full agent.log ---\n' + agent_log
    if gateway_log:
        gateway_log = dump_text + '\n\n--- full gateway.log ---\n' + gateway_log
    if gui_log:
        gui_log = dump_text + '\n\n--- full gui.log ---\n' + gui_log
    if desktop_log:
        desktop_log = dump_text + '\n\n--- full desktop.log ---\n' + desktop_log
    if redact:
        report = _REDACTION_BANNER + report
        if agent_log:
            agent_log = _REDACTION_BANNER + agent_log
        if gateway_log:
            gateway_log = _REDACTION_BANNER + gateway_log
        if gui_log:
            gui_log = _REDACTION_BANNER + gui_log
        if desktop_log:
            desktop_log = _REDACTION_BANNER + desktop_log
    bundle: dict[str, str] = {'report': report}
    if agent_log:
        bundle['agent.log'] = agent_log
    if gateway_log:
        bundle['gateway.log'] = gateway_log
    if gui_log:
        bundle['gui.log'] = gui_log
    if desktop_log:
        bundle['desktop.log'] = desktop_log
    return bundle

def build_nous_bundle(bundle: dict[str, str], redact: bool=True) -> bytes:
    """Gzip-compress a :func:`collect_share_bundle` mapping into the Nous envelope.

    The JSON shape is what the discord-support viewer (Repo 3) parses::

        {"format": "duck-agent-debug-share/1",
         "redacted": <bool>,
         "created": <iso8601>,
         "files": {"report": ..., "agent.log": ..., ...}}
    """
    created = datetime.datetime.now(datetime.timezone.utc).isoformat()
    envelope = {'format': _NOUS_BUNDLE_FORMAT, 'redacted': bool(redact), 'created': created, 'files': bundle}
    return gzip.compress(json.dumps(envelope).encode('utf-8'))

@dataclass
class DebugShareResult:
    """Structured outcome of a ``debug share`` upload.

    Returned by :func:`build_debug_share` so non-CLI callers (the dashboard
    web server, gateway) can render the uploaded paste URLs as real links
    instead of scraping printed text.
    """
    urls: dict
    failures: list
    redacted: bool
    auto_delete_seconds: int
    report: str = ''

def build_debug_share(*, log_lines: int=200, expiry: int=7, redact: bool=True) -> DebugShareResult:
    """Collect the debug report + full logs, upload each, return the URLs.

    This is the shared core behind ``duck-agent debug share`` (CLI) and the
    dashboard ``POST /api/ops/debug-share`` endpoint. It performs blocking
    network I/O (paste uploads) — callers inside an event loop must run it in
    a worker thread.

    The summary report upload is required: on failure this raises
    ``RuntimeError``. Full-log uploads are best-effort; their errors are
    collected into ``failures`` rather than raised.
    """
    _best_effort_sweep_expired_pastes()
    bundle = collect_share_bundle(log_lines=log_lines, redact=redact)
    if redact:
        logger.info('duck-agent debug share: applied force-mode redaction to log snapshots before upload')
    report = bundle['report']
    urls: dict[str, str] = {}
    failures: list[str] = []
    urls['Report'] = upload_to_pastebin(report, expiry_days=expiry)
    for label in ('agent.log', 'gateway.log', 'gui.log', 'desktop.log'):
        content = bundle.get(label)
        if not content:
            continue
        try:
            urls[label] = upload_to_pastebin(content, expiry_days=expiry)
        except Exception as exc:
            failures.append(f'{label}: {exc}')
    _schedule_auto_delete(list(urls.values()))
    return DebugShareResult(urls=urls, failures=failures, redacted=redact, auto_delete_seconds=_AUTO_DELETE_SECONDS, report=report)

def _confirm_upload(args) -> bool:
    """Require explicit consent before any debug-share upload.

    The privacy notice is printed by the caller. This gates the actual
    upload: with ``--yes`` (or ``-y``) we proceed unprompted; otherwise we
    ask an interactive ``[y/N]`` question. In a non-interactive context
    (no TTY on stdin — scripts, CI, piped input) we refuse rather than
    hang or upload silently, so debug data can't be exposed without a
    deliberate ``--yes``.

    Returns True to proceed with the upload, False to abort.
    """
    if bool(getattr(args, 'yes', False)):
        return True
    if not sys.stdin.isatty():
        print('ERROR: Non-interactive mode requires --yes to confirm upload.\n       This prevents accidental exposure of personal data.\n       Use --local to view the report without uploading.', file=sys.stderr)
        sys.exit(1)
    try:
        answer = input('Upload debug report? [y/N] ').strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ''
    if answer not in ('y', 'yes'):
        print('Aborted.')
        return False
    return True

def run_debug_share(args):
    """Collect debug report + full logs, upload each, print URLs."""
    log_lines = getattr(args, 'lines', 200)
    expiry = getattr(args, 'expire', 7)
    local_only = getattr(args, 'local', False)
    nous = getattr(args, 'nous', False)
    redact = not getattr(args, 'no_redact', False)
    if local_only:
        _best_effort_sweep_expired_pastes()
        print('Collecting debug report...')
        bundle = collect_share_bundle(log_lines=log_lines, redact=redact)
        print(bundle['report'])
        for title, label in (('FULL agent.log', 'agent.log'), ('FULL gateway.log', 'gateway.log'), ('FULL gui.log', 'gui.log'), ('FULL desktop.log', 'desktop.log')):
            body = bundle.get(label)
            if body:
                print(f"\n\n{'=' * 60}")
                print(title)
                print(f"{'=' * 60}\n")
                print(body)
        return
    if nous:
        _run_debug_share_nous(args, log_lines=log_lines, redact=redact)
        return
    print(_PRIVACY_NOTICE)
    if not _confirm_upload(args):
        return
    print('Collecting debug report...')
    print('Uploading...')
    try:
        result = build_debug_share(log_lines=log_lines, expiry=expiry, redact=redact)
    except RuntimeError as exc:
        print(f'\nUpload failed: {exc}', file=sys.stderr)
        print('\nRun `duck-agent debug share --local` to print the report instead.\n')
        sys.exit(1)
    label_width = max((len(k) for k in result.urls))
    print('\nDebug report uploaded:')
    for label, url in result.urls.items():
        print(f'  {label:<{label_width}}  {url}')
    if result.failures:
        print(f"\n  (failed to upload: {', '.join(result.failures)})")
    hours = result.auto_delete_seconds // 3600
    print(f'\n⏱  Pastes will auto-delete in {hours} hours.')
    print('To delete now:  duck-agent debug delete <url>')
    print('\nShare these links with the Duck Agent team for support.')
_NOUS_PRIVACY_NOTICE = '⚠️  --nous: This uploads your debug bundle to Nous-INTERNAL storage (AWS S3),\n    NOT a public paste service. The following is included:\n  • System info (OS, Python/Duck Agent version, provider, which API keys are\n    configured — NOT the actual keys)\n  • Full agent.log, gateway.log, and desktop.log (up to 512 KB each — likely\n    contains conversation content, tool outputs, and file paths)\n\n  • The bundle is viewable only by Nous staff (and allowlisted Discord mods)\n    via a Google-login-gated viewer.\n  • It is NOT a public paste — there is no public URL to the contents.\n  • It auto-deletes after 14 days.\n'

def _run_debug_share_nous(args, *, log_lines: int, redact: bool) -> None:
    """Handle ``duck-agent debug share --nous``: upload the bundle to Nous-S3.

    Collects the same force-redacted bundle as the paste path, gzips it into
    the Nous envelope, requests a signed URL from NAS, uploads, and prints the
    private viewer link. On any failure falls back to a clear error that
    suggests ``--local``.
    """
    from hermes_cli.diagnostics_upload import share_to_nous
    print(_NOUS_PRIVACY_NOTICE)
    if not _confirm_upload(args):
        return
    if not redact:
        print('⚠️  --no-redact is set: secrets in your logs will NOT be redacted before upload.\n')
    print('Collecting debug report...')
    _best_effort_sweep_expired_pastes()
    bundle = collect_share_bundle(log_lines=log_lines, redact=redact)
    if redact:
        logger.info('duck-agent debug share --nous: applied force-mode redaction before upload')
    blob = build_nous_bundle(bundle, redact=redact)
    print('Uploading to Nous diagnostics storage...')
    try:
        res = share_to_nous(blob)
    except Exception as exc:
        print(f'\nNous upload failed: {exc}\n\nThe Nous diagnostics service may be unavailable or not yet provisioned.\nRun `duck-agent debug share --local` to print the report instead, or `duck-agent debug share` to upload to a public paste service.\n', file=sys.stderr)
        sys.exit(1)
    view_url = res.get('viewUrl') or res.get('view_url')
    print('\nDebug bundle uploaded to Nous (private):')
    if view_url:
        print(f'  View URL  {view_url}')
    else:
        print(f"  (no view URL returned; upload id: {res.get('id', '?')})")
    expires_at = res.get('expiresAt') or res.get('expires_at')
    if expires_at:
        print(f'\n⏱  Auto-deletes at {expires_at} (14-day retention).')
    else:
        print('\n⏱  Auto-deletes after 14 days.')
    print('\nShare this private link with the Nous team — only Nous staff (via Google login) can open it.')

def run_debug_delete(args):
    """Delete one or more paste URLs uploaded by /debug."""
    urls = getattr(args, 'urls', [])
    if not urls:
        print('Usage: duck-agent debug delete <url> [<url> ...]')
        print("  Deletes paste.rs pastes uploaded by 'duck-agent debug share'.")
        return
    for url in urls:
        try:
            ok = delete_paste(url)
            if ok:
                print(f'  ✓ Deleted: {url}')
            else:
                print(f'  ✗ Failed to delete: {url} (unexpected response)')
        except ValueError as exc:
            print(f'  ✗ {exc}')
        except Exception as exc:
            print(f'  ✗ Could not delete {url}: {exc}')

def run_debug(args):
    """Route debug subcommands."""
    try:
        _sweep_expired_pastes()
    except Exception:
        pass
    subcmd = getattr(args, 'debug_command', None)
    if subcmd == 'share':
        run_debug_share(args)
    elif subcmd == 'delete':
        run_debug_delete(args)
    else:
        print('Usage: duck-agent debug <command>')
        print()
        print('Commands:')
        print('  share    Upload debug report to a paste service and print URL')
        print('  delete   Delete a previously uploaded paste')
        print()
        print('Options (share):')
        print('  --lines N    Number of log lines to include (default: 200)')
        print('  --expire N   Paste expiry in days (default: 7)')
        print('  --local      Print report locally instead of uploading')
        print('  --nous       Upload to Nous-internal storage (private, staff-only,')
        print('               auto-deletes in 14 days) instead of a public paste')
        print('  --no-redact  Disable upload-time secret redaction (default: redact)')
        print()
        print('Options (delete):')
        print('  <url> ...    One or more paste URLs to delete')