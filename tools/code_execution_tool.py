"""
Code Execution Tool -- Programmatic Tool Calling (PTC)

Lets the LLM write a Python script that calls Duck Agent tools via RPC,
collapsing multi-step tool chains into a single inference turn.

Architecture (two transports):

  **Local backend (UDS):**
  1. Parent generates a `hermes_tools.py` stub module with UDS RPC functions
  2. Parent opens a Unix domain socket and starts an RPC listener thread
  3. Parent spawns a child process that runs the LLM's script
  4. Tool calls travel over the UDS back to the parent for dispatch

  **Remote backends (file-based RPC):**
  1. Parent generates `hermes_tools.py` with file-based RPC stubs
  2. Parent ships both files to the remote environment
  3. Script runs inside the terminal backend (Docker/SSH/Modal/Daytona/etc.)
  4. Tool calls are written as request files; a polling thread on the parent
     reads them via env.execute(), dispatches, and writes response files
  5. The script polls for response files and continues

In both cases, only the script's stdout is returned to the LLM; intermediate
tool results never enter the context window.

Platform: Linux / macOS only (Unix domain sockets for local). Disabled on Windows.
Remote execution additionally requires Python 3 in the terminal backend.
"""
import base64
import functools
import json
import logging
import os
import platform
import re
import secrets
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
_IS_WINDOWS = platform.system() == 'Windows'
from typing import Any, Dict, List, Optional, Tuple
from tools.thread_context import propagate_context_to_thread
from agent.thread_scoped_output import thread_scoped_silence
logger = logging.getLogger(__name__)
SANDBOX_AVAILABLE = True
SANDBOX_ALLOWED_TOOLS = frozenset(['web_search', 'web_extract', 'read_file', 'write_file', 'search_files', 'patch', 'terminal'])
DEFAULT_TIMEOUT = 300
DEFAULT_MAX_TOOL_CALLS = 50
MAX_STDOUT_BYTES = 50000
MAX_STDERR_BYTES = 10000

def _assemble_stdout_result(head: bytes, tail: bytes=b'', *, total_bytes: Optional[int]=None) -> Tuple[str, Dict[str, Any]]:
    """Build display stdout plus explicit truncation metadata.

    The agent receives execute_code results as JSON. A textual truncation
    marker can be missed or later re-truncated by a client layer, so keep the
    marker for humans and also expose byte counts for deterministic handling.
    """
    captured = head + tail
    total = len(captured) if total_bytes is None else max(total_bytes, len(captured))
    truncated = total > len(captured)
    omitted = max(0, total - len(captured))
    if truncated:
        stdout_text = head.decode('utf-8', errors='replace') + f'\n\n... [OUTPUT TRUNCATED - {omitted:,} bytes omitted out of {total:,} total] ...\n\n' + tail.decode('utf-8', errors='replace')
    else:
        stdout_text = captured.decode('utf-8', errors='replace')
    metadata: Dict[str, Any] = {'stdout_truncated': truncated, 'stdout_bytes_captured': len(captured), 'stdout_bytes_total': total, 'stdout_bytes_omitted': omitted}
    if truncated:
        metadata['warning'] = 'execute_code stdout was truncated; the script did run, but only the captured head/tail output is included. Re-run only with narrower output if the omitted data is required.'
    return (stdout_text, metadata)

def _truncate_stdout_text(stdout_text: str) -> Tuple[str, Dict[str, Any]]:
    """Cap a complete stdout string by bytes using the same head/tail policy."""
    stdout_bytes = stdout_text.encode('utf-8', errors='replace')
    if len(stdout_bytes) <= MAX_STDOUT_BYTES:
        return _assemble_stdout_result(stdout_bytes)
    head_bytes = int(MAX_STDOUT_BYTES * 0.4)
    tail_bytes = MAX_STDOUT_BYTES - head_bytes
    return _assemble_stdout_result(stdout_bytes[:head_bytes], stdout_bytes[-tail_bytes:], total_bytes=len(stdout_bytes))
_SAFE_ENV_PREFIXES = ('PATH', 'HOME', 'USER', 'LANG', 'LC_', 'TERM', 'TMPDIR', 'TMP', 'TEMP', 'SHELL', 'LOGNAME', 'XDG_', 'PYTHONPATH', 'VIRTUAL_ENV', 'CONDA')
_SECRET_SUBSTRINGS = ('KEY', 'TOKEN', 'SECRET', 'PASSWORD', 'CREDENTIAL', 'PASSWD', 'AUTH', 'DSN', 'WEBHOOK', 'CREDS', 'BEARER', 'APIKEY')
_HERMES_CHILD_ALLOWED = frozenset({'DUCK_AGENT_HOME', 'HERMES_PROFILE', 'HERMES_CONFIG', 'HERMES_ENV', 'HERMES_DELEGATED_CHILD_CONTEXT'})
_WINDOWS_ESSENTIAL_ENV_VARS = frozenset({'SYSTEMROOT', 'SYSTEMDRIVE', 'WINDIR', 'COMSPEC', 'PATHEXT', 'OS', 'PROCESSOR_ARCHITECTURE', 'NUMBER_OF_PROCESSORS', 'PUBLIC', 'ALLUSERSPROFILE', 'PROGRAMDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMW6432', 'APPDATA', 'LOCALAPPDATA', 'USERPROFILE', 'USERDOMAIN', 'USERNAME', 'HOMEDRIVE', 'HOMEPATH', 'COMPUTERNAME'})

def _scrub_child_env(source_env, is_passthrough=None, is_windows=None):
    """Produce the scrubbed child-process env for execute_code.

    Rules (order matters):
      1. Passthrough vars (skill- or config-declared) pass through the active
         profile secret scope; an absent scoped value is omitted and an
         unscoped multiplex read fails closed.
      2. Secret-substring names (KEY/TOKEN/DSN/WEBHOOK/etc.) are blocked.
      3. Names matching a safe prefix pass.
      4. Operational HERMES_* vars (_HERMES_CHILD_ALLOWED) pass by exact name.
      5. On Windows, a small OS-essential allowlist passes by exact name
         — without these the child can't even create a socket or spawn a
         subprocess.

    Extracted into a helper so tests can exercise the logic without
    spawning a subprocess.
    """
    resolve_passthrough_value = None
    if is_passthrough is None:
        try:
            from tools.env_passthrough import is_env_passthrough as _ep, resolve_passthrough_value
        except Exception:
            _ep = lambda _: False
            resolve_passthrough_value = lambda _name, _fallback: None
        is_passthrough = _ep
    else:
        try:
            from tools.env_passthrough import resolve_passthrough_value
        except Exception:
            resolve_passthrough_value = lambda _name, _fallback: None
    if is_windows is None:
        is_windows = _IS_WINDOWS
    scrubbed = {}
    _dropped_hermes = []
    for k, v in source_env.items():
        if is_passthrough(k):
            resolved = resolve_passthrough_value(k, v)
            if resolved is not None:
                scrubbed[k] = resolved
            continue
        if any((s in k.upper() for s in _SECRET_SUBSTRINGS)):
            continue
        if any((k.startswith(p) for p in _SAFE_ENV_PREFIXES)):
            scrubbed[k] = v
            continue
        if k in _HERMES_CHILD_ALLOWED:
            scrubbed[k] = v
            continue
        if is_windows and k.upper() in _WINDOWS_ESSENTIAL_ENV_VARS:
            scrubbed[k] = v
            continue
        if k.startswith('HERMES_'):
            _dropped_hermes.append(k)
    if _dropped_hermes:
        logger.debug('execute_code: dropped %d non-allowlisted HERMES_* var(s) from the sandbox child env (%s). This is intentional hardening (#27303); if a sandbox script legitimately needs one, declare it via env_passthrough in the skill/config so it passes by explicit opt-in.', len(_dropped_hermes), ', '.join(sorted(_dropped_hermes)))
    try:
        from agent.delegation_context import is_delegated_child_process_context, scrub_kanban_env
        if is_delegated_child_process_context():
            scrubbed = scrub_kanban_env(scrubbed)
    except Exception:
        pass
    return scrubbed

def check_sandbox_requirements() -> bool:
    """Code execution sandbox requires a POSIX OS for Unix domain sockets."""
    if not SANDBOX_AVAILABLE:
        return False
    try:
        from tools.terminal_tool import _check_vercel_sandbox_requirements, _get_env_config
        config = _get_env_config()
    except Exception:
        logger.debug('Could not resolve terminal config for execute_code availability', exc_info=True)
        return False
    if config.get('env_type') == 'vercel_sandbox':
        return _check_vercel_sandbox_requirements(config)
    return True
_TOOL_STUBS = {'web_search': ('web_search', 'query: str, limit: int = 5', '"""Search the web. Returns dict with data.web list of {url, title, description}."""', '{"query": query, "limit": limit}'), 'web_extract': ('web_extract', 'urls: list, char_limit: int = None', '"""Extract content from URLs (no LLM summarization). Returns dict with results list of {url, title, content, error}. Pages over char_limit (default 15000) are head+tail truncated with the full text stored on disk; the content footer gives the path. content is markdown."""', '{"urls": urls, "char_limit": char_limit}'), 'read_file': ('read_file', 'path: str, offset: int = 1, limit: int = 2000', '"""Read a file (1-indexed lines). Returns dict with "content" and "total_lines"."""', '{"path": path, "offset": offset, "limit": limit}'), 'write_file': ('write_file', 'path: str, content: str, cross_profile: bool = False', '"""Write content to a file (always overwrites). Returns dict with status. cross_profile=True opts out of the cross-Duck Agent-profile soft guard."""', '{"path": path, "content": content, "cross_profile": cross_profile}'), 'search_files': ('search_files', 'pattern: str, target: str = "content", path: str = ".", file_glob: str = None, limit: int = 50, offset: int = 0, output_mode: str = "content", context: int = 0', '"""Search file contents (target="content") or find files by name (target="files"). Returns dict with "matches"."""', '{"pattern": pattern, "target": target, "path": path, "file_glob": file_glob, "limit": limit, "offset": offset, "output_mode": output_mode, "context": context}'), 'patch': ('patch', 'path: str = None, old_string: str = None, new_string: str = None, replace_all: bool = False, mode: str = "replace", patch: str = None, cross_profile: bool = False', '"""Targeted find-and-replace (mode="replace") or V4A multi-file patches (mode="patch"). Returns dict with status. cross_profile=True opts out of the cross-Duck Agent-profile soft guard."""', '{"path": path, "old_string": old_string, "new_string": new_string, "replace_all": replace_all, "mode": mode, "patch": patch, "cross_profile": cross_profile}'), 'terminal': ('terminal', 'command: str, timeout: int = None, workdir: str = None', '"""Run a shell command (foreground only). Returns dict with "output" and "exit_code"."""', '{"command": command, "timeout": timeout, "workdir": workdir}')}

def _sandbox_failure_hint(stderr_text: str, enabled_tools=None) -> Optional[str]:
    """Map well-known sandbox script failures to one actionable recovery hint.

    Production mining (state.db): the top execute_code failure classes are
    hermes_tools import misuse (importing tools that aren't in the sandbox,
    23x in one window), calling the built-in helpers via import, treating
    tool results as strings instead of dicts, and importing third-party
    packages that don't exist in the sandbox interpreter. Bounded scan,
    first match wins, never raises.
    """
    if not stderr_text:
        return None
    window = stderr_text[:4000]
    try:
        m = re.search("cannot import name '(\\w+)' from 'hermes_tools'", window)
        if m:
            missing = m.group(1)
            available = sorted(SANDBOX_ALLOWED_TOOLS & set(enabled_tools or SANDBOX_ALLOWED_TOOLS))
            builtin = {'json_parse', 'shell_quote', 'retry'}
            if missing in builtin:
                return f'{missing} is a BUILT-IN helper in the sandbox — no import needed. Remove it from the import line and call {missing}(...) directly.'
            return f"'{missing}' is not available inside the execute_code sandbox. Importable tools here: {', '.join(available)}. For anything else, use the normal tool call instead of execute_code."
        m = re.search("NameError: name '(json_parse|shell_quote|retry)' is not defined", window)
        if m:
            return f'{m.group(1)} is built into the generated sandbox module — call it directly at module scope without importing it.'
        m = re.search("ModuleNotFoundError: No module named '([\\w.]+)'", window)
        if m:
            return f"'{m.group(1)}' is not installed in the sandbox interpreter. Use Python stdlib inside execute_code, or run the code via terminal() with the project venv's python instead."
        if re.search("TypeError: string indices must be integers|AttributeError: 'str' object has no attribute 'get'", window):
            return "Tool functions in the sandbox return DICTS (already parsed) — do not json.loads() them or index them like strings. Example: read_file(path)['content']."
    except Exception:
        return None
    return None

def generate_hermes_tools_module(enabled_tools: List[str], transport: str='uds') -> str:
    """
    Build the source code for the hermes_tools.py stub module.

    Only tools in both SANDBOX_ALLOWED_TOOLS and enabled_tools get stubs.

    Args:
        enabled_tools: Tool names enabled in the current session.
        transport: ``"uds"`` for Unix domain socket (local backend) or
                   ``"file"`` for file-based RPC (remote backends).
    """
    tools_to_generate = sorted(SANDBOX_ALLOWED_TOOLS & set(enabled_tools))
    stub_functions = []
    export_names = []
    for tool_name in tools_to_generate:
        if tool_name not in _TOOL_STUBS:
            continue
        func_name, sig, doc, args_expr = _TOOL_STUBS[tool_name]
        stub_functions.append(f'def {func_name}({sig}):\n    {doc}\n    return _call({func_name!r}, {args_expr})\n')
        export_names.append(func_name)
    if transport == 'file':
        header = _FILE_TRANSPORT_HEADER
    else:
        header = _UDS_TRANSPORT_HEADER
    return header + '\n'.join(stub_functions)
_COMMON_HELPERS = '\n# ---------------------------------------------------------------------------\n# Convenience helpers (avoid common scripting pitfalls)\n# ---------------------------------------------------------------------------\n\ndef json_parse(text: str):\n    """Parse JSON tolerant of control characters (strict=False).\n    Use this instead of json.loads() when parsing output from terminal()\n    or web_extract() that may contain raw tabs/newlines in strings."""\n    return json.loads(text, strict=False)\n\n\ndef shell_quote(s: str) -> str:\n    """Shell-escape a string for safe interpolation into commands.\n    Use this when inserting dynamic content into terminal() commands:\n        terminal(f"echo {shell_quote(user_input)}")\n    """\n    return shlex.quote(s)\n\n\ndef retry(fn, max_attempts=3, delay=2):\n    """Retry a function up to max_attempts times with exponential backoff.\n    Use for transient failures (network errors, API rate limits):\n        result = retry(lambda: terminal("gh issue list ..."))\n    """\n    last_err = None\n    for attempt in range(max_attempts):\n        try:\n            return fn()\n        except Exception as e:\n            last_err = e\n            if attempt < max_attempts - 1:\n                time.sleep(delay * (2 ** attempt))\n    raise last_err\n\n'
_UDS_TRANSPORT_HEADER = '"""Auto-generated Duck Agent tools RPC stubs."""\nimport json, os, socket, shlex, threading, time\n\n_sock = None\n# The RPC server handles a single client connection serially and has no\n# request-id in the protocol, so concurrent _call() invocations from multiple\n# threads (e.g. ThreadPoolExecutor) would race on the shared socket and get\n# each other\'s responses. Serialize the entire send+recv round-trip.\n_call_lock = threading.Lock()\n' + _COMMON_HELPERS + '\ndef _connect():\n    """Connect to the parent\'s RPC server via the transport it picked.\n\n    HERMES_RPC_SOCKET can be either:\n      - a filesystem path (POSIX Unix domain socket — the default on\n        Linux and macOS)\n      - a string of the form ``tcp://127.0.0.1:<port>`` (Windows, where\n        AF_UNIX is unreliable — the parent falls back to loopback TCP)\n    """\n    global _sock\n    if _sock is None:\n        endpoint = os.environ["HERMES_RPC_SOCKET"]\n        if endpoint.startswith("tcp://"):\n            # tcp://host:port  (host is always 127.0.0.1 in practice — we\n            # only bind loopback server-side)\n            _host_port = endpoint[len("tcp://"):]\n            _host, _, _port = _host_port.rpartition(":")\n            _sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n            _sock.connect((_host or "127.0.0.1", int(_port)))\n        else:\n            _sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n            _sock.connect(endpoint)\n        _sock.settimeout(300)\n    return _sock\n\ndef _call(tool_name, args):\n    """Send a tool call to the parent process and return the parsed result."""\n    request = json.dumps({\n        "tool": tool_name,\n        "args": args,\n        "token": os.environ.get("HERMES_RPC_TOKEN", ""),\n    }) + "\\n"\n    with _call_lock:\n        conn = _connect()\n        conn.sendall(request.encode())\n        buf = b""\n        while True:\n            chunk = conn.recv(65536)\n            if not chunk:\n                raise RuntimeError("Agent process disconnected")\n            buf += chunk\n            if buf.endswith(b"\\n"):\n                break\n    raw = buf.decode().strip()\n    result = json.loads(raw)\n    if isinstance(result, str):\n        try:\n            return json.loads(result)\n        except (json.JSONDecodeError, TypeError):\n            return result\n    return result\n\n'
_FILE_TRANSPORT_HEADER = '"""Auto-generated Duck Agent tools RPC stubs (file-based transport)."""\nimport json, os, shlex, tempfile, threading, time\n\n_RPC_DIR = os.environ.get("HERMES_RPC_DIR") or os.path.join(tempfile.gettempdir(), "hermes_rpc")\n_seq = 0\n# `_seq += 1` is not atomic (read-modify-write), so concurrent _call()\n# invocations from multiple threads could allocate the same sequence number\n# and clobber each other\'s request files. Guard seq allocation with a lock.\n_seq_lock = threading.Lock()\n' + _COMMON_HELPERS + '\ndef _call(tool_name, args):\n    """Send a tool call request via file-based RPC and wait for response."""\n    global _seq\n    with _seq_lock:\n        _seq += 1\n        seq = _seq\n    seq_str = f"{seq:06d}"\n    req_file = os.path.join(_RPC_DIR, f"req_{seq_str}")\n    res_file = os.path.join(_RPC_DIR, f"res_{seq_str}")\n\n    # Write request atomically (write to .tmp, then rename).\n    # encoding="utf-8" is critical: on Windows-hosted remote backends\n    # (or any non-UTF-8 locale) the default open() mode would mangle\n    # non-ASCII chars in tool args when encoding them as JSON.\n    tmp = req_file + ".tmp"\n    with open(tmp, "w", encoding="utf-8") as f:\n        json.dump({\n            "tool": tool_name,\n            "args": args,\n            "seq": seq,\n            "token": os.environ.get("HERMES_RPC_TOKEN", ""),\n        }, f)\n    os.rename(tmp, req_file)\n\n    # Wait for response with adaptive polling\n    deadline = time.monotonic() + 300  # 5-minute timeout per tool call\n    poll_interval = 0.05  # Start at 50ms\n    while not os.path.exists(res_file):\n        if time.monotonic() > deadline:\n            raise RuntimeError(f"RPC timeout: no response for {tool_name} after 300s")\n        time.sleep(poll_interval)\n        poll_interval = min(poll_interval * 1.2, 0.25)  # Back off to 250ms\n\n    with open(res_file, encoding="utf-8") as f:\n        raw = f.read()\n\n    # Clean up response file\n    try:\n        os.unlink(res_file)\n    except OSError:\n        pass\n\n    result = json.loads(raw)\n    if isinstance(result, str):\n        try:\n            return json.loads(result)\n        except (json.JSONDecodeError, TypeError):\n            return result\n    return result\n\n'
_TERMINAL_BLOCKED_PARAMS = {'background', 'pty', 'notify_on_complete', 'watch_patterns'}

def _rpc_server_loop(server_sock: socket.socket, task_id: str, tool_call_log: list, tool_call_counter: list, max_tool_calls: int, allowed_tools: frozenset, stop_event: threading.Event, rpc_token: str):
    """
    Accept one client connection and dispatch tool-call requests until
    the client disconnects or the call limit is reached.
    """
    from model_tools import handle_function_call
    conn = None
    try:
        server_sock.settimeout(0.05)
        while not stop_event.is_set():
            try:
                conn, _ = server_sock.accept()
                break
            except socket.timeout:
                continue
        if conn is None:
            return
        conn.settimeout(300)
        buf = b''
        while True:
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            while b'\n' in buf:
                line, buf = buf.split(b'\n', 1)
                line = line.strip()
                if not line:
                    continue
                call_start = time.monotonic()
                try:
                    request = json.loads(line.decode())
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    resp = tool_error(f'Invalid RPC request: {exc}')
                    conn.sendall((resp + '\n').encode())
                    continue
                if not rpc_token or not secrets.compare_digest(str(request.get('token') or '').encode(), rpc_token.encode()):
                    resp = tool_error('Unauthorized RPC request')
                    conn.sendall((resp + '\n').encode())
                    continue
                tool_name = request.get('tool', '')
                tool_args = request.get('args', {})
                if tool_name not in allowed_tools:
                    available = ', '.join(sorted(allowed_tools))
                    resp = tool_error(f"Tool '{tool_name}' is not available in execute_code. Available: {available}")
                    conn.sendall((resp + '\n').encode())
                    continue
                if tool_call_counter[0] >= max_tool_calls:
                    resp = tool_error(f'Tool call limit reached ({max_tool_calls}). No more tool calls allowed in this execution.')
                    conn.sendall((resp + '\n').encode())
                    continue
                if tool_name == 'terminal' and isinstance(tool_args, dict):
                    for param in _TERMINAL_BLOCKED_PARAMS:
                        tool_args.pop(param, None)
                try:
                    with thread_scoped_silence():
                        result = handle_function_call(tool_name, tool_args, task_id=task_id)
                except Exception as exc:
                    logger.error('Tool call failed in sandbox: %s', exc, exc_info=True)
                    result = tool_error(str(exc))
                tool_call_counter[0] += 1
                call_duration = time.monotonic() - call_start
                args_preview = str(tool_args)[:80]
                tool_call_log.append({'tool': tool_name, 'args_preview': args_preview, 'duration': round(call_duration, 2)})
                conn.sendall((result + '\n').encode())
    except socket.timeout:
        logger.debug('RPC listener socket timeout')
    except OSError as e:
        logger.debug('RPC listener socket error: %s', e, exc_info=True)
    finally:
        if conn:
            try:
                conn.close()
            except OSError as e:
                logger.debug('RPC conn close error: %s', e)

def _get_or_create_env(task_id: str):
    """Get or create the terminal environment for *task_id*.

    Reuses the same environment (container/sandbox/SSH session) that the
    terminal and file tools use, creating one if it doesn't exist yet.
    Returns ``(env, env_type)`` tuple.
    """
    from tools.terminal_tool import _active_environments, _env_lock, _create_environment, _get_env_config, _last_activity, _start_cleanup_thread, _creation_locks, _creation_locks_lock, _task_env_overrides, _resolve_container_task_id
    effective_task_id = _resolve_container_task_id(task_id)
    with _env_lock:
        if effective_task_id in _active_environments:
            _last_activity[effective_task_id] = time.time()
            return (_active_environments[effective_task_id], _get_env_config()['env_type'])
    with _creation_locks_lock:
        if effective_task_id not in _creation_locks:
            _creation_locks[effective_task_id] = threading.Lock()
        task_lock = _creation_locks[effective_task_id]
    with task_lock:
        with _env_lock:
            if effective_task_id in _active_environments:
                _last_activity[effective_task_id] = time.time()
                return (_active_environments[effective_task_id], _get_env_config()['env_type'])
        config = _get_env_config()
        env_type = config['env_type']
        overrides = _task_env_overrides.get(effective_task_id, {})
        if env_type == 'docker':
            image = overrides.get('docker_image') or config['docker_image']
        elif env_type == 'singularity':
            image = overrides.get('singularity_image') or config['singularity_image']
        elif env_type == 'modal':
            image = overrides.get('modal_image') or config['modal_image']
        elif env_type == 'daytona':
            image = overrides.get('daytona_image') or config['daytona_image']
        else:
            image = ''
        cwd = overrides.get('cwd') or config['cwd']
        container_config = None
        if env_type in {'docker', 'singularity', 'modal', 'daytona', 'vercel_sandbox'}:
            container_config = {'container_cpu': config.get('container_cpu', 1), 'container_memory': config.get('container_memory', 5120), 'container_disk': config.get('container_disk', 51200), 'container_persistent': config.get('container_persistent', True), 'vercel_runtime': config.get('vercel_runtime', ''), 'docker_volumes': config.get('docker_volumes', []), 'docker_run_as_host_user': config.get('docker_run_as_host_user', False), 'docker_network': config.get('docker_network', True)}
        ssh_config = None
        if env_type == 'ssh':
            ssh_config = {'host': config.get('ssh_host', ''), 'user': config.get('ssh_user', ''), 'port': config.get('ssh_port', 22), 'key': config.get('ssh_key', ''), 'persistent': config.get('ssh_persistent', False)}
        local_config = None
        if env_type == 'local':
            local_config = {'persistent': config.get('local_persistent', False)}
        logger.info('Creating new %s environment for execute_code task %s...', env_type, effective_task_id[:8])
        env = _create_environment(env_type=env_type, image=image, cwd=cwd, timeout=config['timeout'], ssh_config=ssh_config, container_config=container_config, local_config=local_config, task_id=effective_task_id, host_cwd=config.get('host_cwd'))
        with _env_lock:
            _active_environments[effective_task_id] = env
            _last_activity[effective_task_id] = time.time()
        _start_cleanup_thread()
        logger.info('%s environment ready for execute_code task %s', env_type, effective_task_id[:8])
        return (env, env_type)

def _ship_file_to_remote(env, remote_path: str, content: str) -> None:
    """Write *content* to *remote_path* on the remote environment.

    Uses ``echo … | base64 -d`` rather than stdin piping because some
    backends (Modal) don't reliably deliver stdin_data to chained
    commands.  Base64 output is shell-safe ([A-Za-z0-9+/=]) so single
    quotes are fine.
    """
    encoded = base64.b64encode(content.encode('utf-8')).decode('ascii')
    quoted_remote_path = shlex.quote(remote_path)
    env.execute(f"echo '{encoded}' | base64 -d > {quoted_remote_path}", cwd='/', timeout=30)

def _env_temp_dir(env: Any) -> str:
    """Return a writable temp dir for env-backed execute_code sandboxes."""
    get_temp_dir = getattr(env, 'get_temp_dir', None)
    if callable(get_temp_dir):
        try:
            temp_dir = get_temp_dir()
            if isinstance(temp_dir, str) and temp_dir.startswith('/'):
                return temp_dir.rstrip('/') or '/'
        except Exception as exc:
            logger.debug('Could not resolve execute_code env temp dir: %s', exc)
    candidate = tempfile.gettempdir()
    if isinstance(candidate, str) and candidate.startswith('/'):
        return candidate.rstrip('/') or '/'
    return '/tmp'

def _rpc_poll_loop(env, rpc_dir: str, task_id: str, tool_call_log: list, tool_call_counter: list, max_tool_calls: int, allowed_tools: frozenset, stop_event: threading.Event, rpc_token: str):
    """Poll the remote filesystem for tool call requests and dispatch them.

    Runs in a background thread.  Each ``env.execute()`` spawns an
    independent process, so these calls run safely concurrent with the
    script-execution thread.
    """
    from model_tools import handle_function_call
    poll_interval = 0.1
    quoted_rpc_dir = shlex.quote(rpc_dir)
    while not stop_event.is_set():
        try:
            ls_result = env.execute(f'ls -1 {quoted_rpc_dir}/req_* 2>/dev/null || true', cwd='/', timeout=10)
            output = ls_result.get('output', '').strip()
            if not output:
                stop_event.wait(poll_interval)
                continue
            req_files = sorted([f.strip() for f in output.split('\n') if f.strip() and (not f.strip().endswith('.tmp')) and ('/req_' in f.strip())])
            for req_file in req_files:
                if stop_event.is_set():
                    break
                call_start = time.monotonic()
                quoted_req_file = shlex.quote(req_file)
                read_result = env.execute(f'cat {quoted_req_file}', cwd='/', timeout=10)
                try:
                    request = json.loads(read_result.get('output', ''))
                except (json.JSONDecodeError, ValueError):
                    logger.debug('Malformed RPC request in %s', req_file)
                    env.execute(f'rm -f {quoted_req_file}', cwd='/', timeout=5)
                    continue
                if not rpc_token or not secrets.compare_digest(str(request.get('token') or '').encode(), rpc_token.encode()):
                    logger.debug('Unauthorized RPC request in %s', req_file)
                    env.execute(f'rm -f {quoted_req_file}', cwd='/', timeout=5)
                    continue
                tool_name = request.get('tool', '')
                tool_args = request.get('args', {})
                seq = request.get('seq', 0)
                seq_str = f'{seq:06d}'
                res_file = f'{rpc_dir}/res_{seq_str}'
                quoted_res_file = shlex.quote(res_file)
                if tool_name not in allowed_tools:
                    available = ', '.join(sorted(allowed_tools))
                    tool_result = tool_error(f"Tool '{tool_name}' is not available in execute_code. Available: {available}")
                elif tool_call_counter[0] >= max_tool_calls:
                    tool_result = tool_error(f'Tool call limit reached ({max_tool_calls}). No more tool calls allowed in this execution.')
                else:
                    if tool_name == 'terminal' and isinstance(tool_args, dict):
                        for param in _TERMINAL_BLOCKED_PARAMS:
                            tool_args.pop(param, None)
                    try:
                        with thread_scoped_silence():
                            tool_result = handle_function_call(tool_name, tool_args, task_id=task_id)
                    except Exception as exc:
                        logger.error('Tool call failed in remote sandbox: %s', exc, exc_info=True)
                        tool_result = tool_error(str(exc))
                    tool_call_counter[0] += 1
                    call_duration = time.monotonic() - call_start
                    tool_call_log.append({'tool': tool_name, 'args_preview': str(tool_args)[:80], 'duration': round(call_duration, 2)})
                encoded_result = base64.b64encode(tool_result.encode('utf-8')).decode('ascii')
                env.execute(f"echo '{encoded_result}' | base64 -d > {quoted_res_file}.tmp && mv {quoted_res_file}.tmp {quoted_res_file}", cwd='/', timeout=60)
                env.execute(f'rm -f {quoted_req_file}', cwd='/', timeout=5)
        except Exception as e:
            if not stop_event.is_set():
                logger.debug('RPC poll error: %s', e, exc_info=True)
        if not stop_event.is_set():
            stop_event.wait(poll_interval)

def _execute_remote(code: str, task_id: Optional[str], enabled_tools: Optional[List[str]]) -> str:
    """Run a script on the remote terminal backend via file-based RPC.

    The script and the generated hermes_tools.py module are shipped to
    the remote environment, and tool calls are proxied through a polling
    thread that communicates via request/response files.
    """
    _cfg = _load_config()
    timeout = _cfg.get('timeout', DEFAULT_TIMEOUT)
    max_tool_calls = _cfg.get('max_tool_calls', DEFAULT_MAX_TOOL_CALLS)
    session_tools = set(enabled_tools) if enabled_tools else set()
    sandbox_tools = frozenset(SANDBOX_ALLOWED_TOOLS & session_tools)
    if not sandbox_tools:
        sandbox_tools = SANDBOX_ALLOWED_TOOLS
    effective_task_id = task_id or 'default'
    env, env_type = _get_or_create_env(effective_task_id)
    sandbox_id = uuid.uuid4().hex[:12]
    temp_dir = _env_temp_dir(env)
    sandbox_dir = f'{temp_dir}/hermes_exec_{sandbox_id}'
    quoted_sandbox_dir = shlex.quote(sandbox_dir)
    quoted_rpc_dir = shlex.quote(f'{sandbox_dir}/rpc')
    tool_call_log: list = []
    tool_call_counter = [0]
    exec_start = time.monotonic()
    stop_event = threading.Event()
    rpc_thread = None
    try:
        py_check = env.execute('command -v python3 >/dev/null 2>&1 && echo OK', cwd='/', timeout=15)
        if 'OK' not in py_check.get('output', ''):
            return json.dumps({'status': 'error', 'error': f'Python 3 is not available in the {env_type} terminal environment. Install Python to use execute_code with remote backends.', 'tool_calls_made': 0, 'duration_seconds': 0})
        env.execute(f'mkdir -p {quoted_rpc_dir}', cwd='/', timeout=10)
        rpc_token = secrets.token_urlsafe(32)
        tools_src = generate_hermes_tools_module(list(sandbox_tools), transport='file')
        _ship_file_to_remote(env, f'{sandbox_dir}/hermes_tools.py', tools_src)
        _ship_file_to_remote(env, f'{sandbox_dir}/script.py', code)
        rpc_thread = threading.Thread(target=propagate_context_to_thread(_rpc_poll_loop), args=(env, f'{sandbox_dir}/rpc', effective_task_id, tool_call_log, tool_call_counter, max_tool_calls, sandbox_tools, stop_event, rpc_token), daemon=True)
        rpc_thread.start()
        env_prefix = f"HERMES_RPC_DIR={shlex.quote(f'{sandbox_dir}/rpc')} HERMES_RPC_TOKEN={shlex.quote(rpc_token)} PYTHONDONTWRITEBYTECODE=1"
        tz = os.getenv('HERMES_TIMEZONE', '').strip()
        if tz:
            env_prefix += f' TZ={shlex.quote(tz)}'
        logger.info('Executing code on %s backend (task %s)...', env_type, effective_task_id[:8])
        script_result = env.execute(f'cd {quoted_sandbox_dir} && {env_prefix} python3 script.py', timeout=timeout)
        stdout_text = script_result.get('output', '') or ''
        exit_code = script_result.get('returncode', -1)
        status = 'success'
        if exit_code == 124:
            status = 'timeout'
        elif exit_code == 130:
            status = 'interrupted'
    except Exception as exc:
        duration = round(time.monotonic() - exec_start, 2)
        logger.error('execute_code remote failed after %ss with %d tool calls: %s: %s', duration, tool_call_counter[0], type(exc).__name__, exc, exc_info=True)
        return json.dumps({'status': 'error', 'error': str(exc), 'tool_calls_made': tool_call_counter[0], 'duration_seconds': duration}, ensure_ascii=False)
    finally:
        stop_event.set()
        if rpc_thread is not None:
            rpc_thread.join(timeout=5)
        try:
            env.execute(f'rm -rf {quoted_sandbox_dir}', cwd='/', timeout=15)
        except Exception:
            logger.debug('Failed to clean up remote sandbox %s', sandbox_dir)
    duration = round(time.monotonic() - exec_start, 2)
    stdout_text, stdout_metadata = _truncate_stdout_text(stdout_text)
    from tools.ansi_strip import strip_ansi
    stdout_text = strip_ansi(stdout_text)
    from agent.redact import redact_sensitive_text
    stdout_text = redact_sensitive_text(stdout_text, code_file=True)
    result: Dict[str, Any] = {'status': status, 'output': stdout_text, 'exit_code': exit_code, 'tool_calls_made': tool_call_counter[0], 'duration_seconds': duration}
    result.update(stdout_metadata)
    if status == 'timeout':
        timeout_msg = f'Script timed out after {timeout}s and was killed.'
        result['error'] = timeout_msg
        if stdout_text:
            result['output'] = stdout_text + f'\n\n⏰ {timeout_msg}'
        else:
            result['output'] = f'⏰ {timeout_msg}'
        logger.warning('execute_code (remote) timed out after %ss (limit %ss) with %d tool calls', duration, timeout, tool_call_counter[0])
    elif status == 'interrupted':
        result['output'] = stdout_text + '\n[execution interrupted — user sent a new message]'
    elif exit_code != 0:
        result['status'] = 'error'
        result['error'] = f'Script exited with code {exit_code}'
    return json.dumps(result, ensure_ascii=False)

def execute_code(code: str, task_id: Optional[str]=None, enabled_tools: Optional[List[str]]=None) -> str:
    """
    Run a Python script in a sandboxed child process with RPC access
    to a subset of Duck Agent tools.

    Dispatches to the local (UDS) or remote (file-based RPC) path
    depending on the configured terminal backend.

    Args:
        code:          Python source code to execute.
        task_id:       Session task ID for tool isolation (terminal env, etc.).
        enabled_tools: Tool names enabled in the current session. The sandbox
                       gets the intersection with SANDBOX_ALLOWED_TOOLS.

    Returns:
        JSON string with execution results.
    """
    if not SANDBOX_AVAILABLE:
        return tool_error('execute_code sandbox is unavailable in this environment. Use normal tool calls (terminal, read_file, write_file, ...) instead.')
    if not code or not code.strip():
        return tool_error('No code provided.')
    from tools.terminal_tool import _get_env_config, _docker_has_host_access
    _env_config = _get_env_config()
    env_type = _env_config['env_type']
    from tools.approval import check_execute_code_guard
    _guard = check_execute_code_guard(code, env_type, has_host_access=_docker_has_host_access(_env_config))
    if not _guard.get('approved', False):
        return json.dumps({'status': 'error', 'error': _guard.get('message') or 'execute_code blocked by approval guard.', 'tool_calls_made': 0, 'duration_seconds': 0}, ensure_ascii=False)
    if _guard.get('user_approved'):
        from tools.interrupt import clear_current_thread_interrupt
        clear_current_thread_interrupt()
    if env_type != 'local':
        return _execute_remote(code, task_id, enabled_tools)
    from tools.interrupt import is_interrupted as _is_interrupted
    _cfg = _load_config()
    timeout = _cfg.get('timeout', DEFAULT_TIMEOUT)
    max_tool_calls = _cfg.get('max_tool_calls', DEFAULT_MAX_TOOL_CALLS)
    session_tools = set(enabled_tools) if enabled_tools else set()
    sandbox_tools = frozenset(SANDBOX_ALLOWED_TOOLS & session_tools)
    if not sandbox_tools:
        sandbox_tools = SANDBOX_ALLOWED_TOOLS
    tmpdir = tempfile.mkdtemp(prefix='hermes_sandbox_')
    _sock_tmpdir = '/tmp' if sys.platform == 'darwin' else tempfile.gettempdir()
    _use_tcp_rpc = _IS_WINDOWS
    if _use_tcp_rpc:
        sock_path = None
        rpc_endpoint = None
    else:
        sock_path = os.path.join(_sock_tmpdir, f'hermes_rpc_{uuid.uuid4().hex}.sock')
        rpc_endpoint = sock_path
    tool_call_log: list = []
    tool_call_counter = [0]
    exec_start = time.monotonic()
    server_sock = None
    stop_event = threading.Event()
    try:
        tools_src = generate_hermes_tools_module(list(sandbox_tools))
        with open(os.path.join(tmpdir, 'hermes_tools.py'), 'w', encoding='utf-8') as f:
            f.write(tools_src)
        with open(os.path.join(tmpdir, 'script.py'), 'w', encoding='utf-8') as f:
            f.write(code)
        rpc_token = secrets.token_urlsafe(32)
        if _use_tcp_rpc:
            server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server_sock.bind(('127.0.0.1', 0))
            _host, _port = server_sock.getsockname()[:2]
            rpc_endpoint = f'tcp://{_host}:{_port}'
        else:
            server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server_sock.bind(sock_path)
            os.chmod(sock_path, 384)
        server_sock.listen(1)
        rpc_thread = threading.Thread(target=propagate_context_to_thread(_rpc_server_loop), args=(server_sock, task_id, tool_call_log, tool_call_counter, max_tool_calls, sandbox_tools, stop_event, rpc_token), daemon=True)
        rpc_thread.start()
        child_env = _scrub_child_env(os.environ)
        child_env['HERMES_RPC_SOCKET'] = rpc_endpoint
        child_env['HERMES_RPC_TOKEN'] = rpc_token
        child_env['PYTHONDONTWRITEBYTECODE'] = '1'
        child_env['PYTHONIOENCODING'] = 'utf-8'
        child_env['PYTHONUTF8'] = '1'
        _hermes_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _existing_pp = child_env.get('PYTHONPATH', '')
        _pp_parts = [tmpdir, _hermes_root]
        if _existing_pp:
            _pp_parts.append(_existing_pp)
        child_env['PYTHONPATH'] = os.pathsep.join(_pp_parts)
        _tz_name = os.getenv('HERMES_TIMEZONE', '').strip()
        if _tz_name:
            child_env['TZ'] = _tz_name
        child_env.pop('HERMES_TIMEZONE', None)
        from hermes_constants import apply_subprocess_home_env
        apply_subprocess_home_env(child_env)
        _mode = _get_execution_mode()
        _child_python = _resolve_child_python(_mode)
        _child_cwd = _resolve_child_cwd(_mode, tmpdir, task_id=task_id or '')
        _script_path = os.path.join(tmpdir, 'script.py')
        proc = subprocess.Popen([_child_python, _script_path], cwd=_child_cwd, env=child_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, start_new_session=True, creationflags=subprocess.CREATE_NO_WINDOW if _IS_WINDOWS else 0)
        deadline = time.monotonic() + timeout
        stderr_chunks: list = []
        _STDOUT_HEAD_BYTES = int(MAX_STDOUT_BYTES * 0.4)
        _STDOUT_TAIL_BYTES = MAX_STDOUT_BYTES - _STDOUT_HEAD_BYTES

        def _drain(pipe, chunks, max_bytes):
            """Simple head-only drain (used for stderr)."""
            total = 0
            try:
                while True:
                    data = pipe.read(4096)
                    if not data:
                        break
                    if total < max_bytes:
                        keep = max_bytes - total
                        chunks.append(data[:keep])
                    total += len(data)
            except (ValueError, OSError) as e:
                logger.debug('Error reading process output: %s', e, exc_info=True)
        stdout_total_bytes = [0]

        def _drain_head_tail(pipe, head_chunks, tail_chunks, head_bytes, tail_bytes, total_ref):
            """Drain stdout keeping both head and tail data."""
            head_collected = 0
            from collections import deque
            tail_buf = deque()
            tail_collected = 0
            try:
                while True:
                    data = pipe.read(4096)
                    if not data:
                        break
                    total_ref[0] += len(data)
                    if head_collected < head_bytes:
                        keep = min(len(data), head_bytes - head_collected)
                        head_chunks.append(data[:keep])
                        head_collected += keep
                        data = data[keep:]
                        if not data:
                            continue
                    tail_buf.append(data)
                    tail_collected += len(data)
                    while tail_collected > tail_bytes and tail_buf:
                        oldest = tail_buf.popleft()
                        tail_collected -= len(oldest)
            except (ValueError, OSError):
                pass
            tail_chunks.extend(tail_buf)
        stdout_head_chunks: list = []
        stdout_tail_chunks: list = []
        stdout_reader = threading.Thread(target=_drain_head_tail, args=(proc.stdout, stdout_head_chunks, stdout_tail_chunks, _STDOUT_HEAD_BYTES, _STDOUT_TAIL_BYTES, stdout_total_bytes), daemon=True)
        stderr_reader = threading.Thread(target=_drain, args=(proc.stderr, stderr_chunks, MAX_STDERR_BYTES), daemon=True)
        stdout_reader.start()
        stderr_reader.start()
        status = 'success'
        _activity_state = {'last_touch': time.monotonic(), 'start': exec_start}
        try:
            from tools.environments.base import touch_activity_if_due
        except Exception:
            touch_activity_if_due = None
        poll_interval = 0.005
        while proc.poll() is None:
            if _is_interrupted():
                _kill_process_group(proc)
                status = 'interrupted'
                break
            now = time.monotonic()
            if now > deadline:
                _kill_process_group(proc, escalate=True)
                status = 'timeout'
                break
            if touch_activity_if_due is not None:
                try:
                    touch_activity_if_due(_activity_state, 'execute_code running')
                except Exception:
                    pass
            try:
                proc.wait(timeout=min(poll_interval, max(0.0, deadline - now)))
            except subprocess.TimeoutExpired:
                pass
            poll_interval = min(0.2, poll_interval * 1.5)
        stdout_reader.join(timeout=3)
        stderr_reader.join(timeout=3)
        stderr_text = b''.join(stderr_chunks).decode('utf-8', errors='replace')
        stdout_text, stdout_metadata = _assemble_stdout_result(b''.join(stdout_head_chunks), b''.join(stdout_tail_chunks), total_bytes=stdout_total_bytes[0])
        exit_code = proc.returncode if proc.returncode is not None else -1
        duration = round(time.monotonic() - exec_start, 2)
        stop_event.set()
        server_sock.close()
        server_sock = None
        rpc_thread.join(timeout=3)
        from tools.ansi_strip import strip_ansi
        stdout_text = strip_ansi(stdout_text)
        stderr_text = strip_ansi(stderr_text)
        from agent.redact import redact_sensitive_text
        stdout_text = redact_sensitive_text(stdout_text, code_file=True)
        stderr_text = redact_sensitive_text(stderr_text, code_file=True)
        result: Dict[str, Any] = {'status': status, 'output': stdout_text, 'exit_code': exit_code, 'tool_calls_made': tool_call_counter[0], 'duration_seconds': duration}
        result.update(stdout_metadata)
        if status == 'timeout':
            timeout_msg = f'Script timed out after {timeout}s and was killed.'
            result['error'] = timeout_msg
            if stdout_text:
                result['output'] = stdout_text + f'\n\n⏰ {timeout_msg}'
            else:
                result['output'] = f'⏰ {timeout_msg}'
            logger.warning('execute_code timed out after %ss (limit %ss) with %d tool calls', duration, timeout, tool_call_counter[0])
        elif status == 'interrupted':
            result['output'] = stdout_text + '\n[execution interrupted — user sent a new message]'
        elif exit_code != 0:
            result['status'] = 'error'
            result['error'] = stderr_text or f'Script exited with code {exit_code}'
            if stderr_text:
                result['output'] = stdout_text + '\n--- stderr ---\n' + stderr_text
            hint = _sandbox_failure_hint(stderr_text, enabled_tools=sandbox_tools)
            if hint:
                result['hint'] = hint
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        duration = round(time.monotonic() - exec_start, 2)
        logger.error('execute_code failed after %ss with %d tool calls: %s: %s', duration, tool_call_counter[0], type(exc).__name__, exc, exc_info=True)
        return json.dumps({'status': 'error', 'error': str(exc), 'tool_calls_made': tool_call_counter[0], 'duration_seconds': duration}, ensure_ascii=False)
    finally:
        if server_sock is not None:
            try:
                server_sock.close()
            except OSError as e:
                logger.debug('Server socket close error: %s', e)
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
        try:
            if sock_path:
                os.unlink(sock_path)
        except OSError:
            pass

def _kill_process_group(proc, escalate: bool=False):
    """Kill the child and its entire process tree (cross-platform via psutil)."""
    import psutil
    try:
        parent = psutil.Process(proc.pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        try:
            parent.terminate()
        except psutil.NoSuchProcess:
            pass
    except psutil.NoSuchProcess:
        pass
    except (PermissionError, OSError) as e:
        logger.debug('Could not terminate process tree: %s', e, exc_info=True)
        try:
            proc.kill()
        except Exception as e2:
            logger.debug('Could not kill process: %s', e2, exc_info=True)
    if escalate:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                parent = psutil.Process(proc.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                try:
                    parent.kill()
                except psutil.NoSuchProcess:
                    pass
            except psutil.NoSuchProcess:
                pass
            except (PermissionError, OSError) as e:
                logger.debug('Could not kill process tree: %s', e, exc_info=True)
                try:
                    proc.kill()
                except Exception as e2:
                    logger.debug('Could not kill process: %s', e2, exc_info=True)

def _load_config() -> dict:
    """Load code_execution config without importing the interactive CLI.

    This helper is called while building the module-level execute_code schema
    during tool discovery.  Importing ``cli`` here pulls prompt_toolkit/Rich and
    a large chunk of the classic REPL onto every agent startup path, including
    ``duck-agent --tui`` where it is never used.  Read the lightweight raw config
    instead; the config layer already caches by (mtime, size), and an absent
    key cleanly falls back to DEFAULT_EXECUTION_MODE.
    """
    try:
        from hermes_cli.config import read_raw_config
        cfg = read_raw_config().get('code_execution', {})
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}
EXECUTION_MODES = ('project', 'strict')
DEFAULT_EXECUTION_MODE = 'project'

def _get_execution_mode() -> str:
    """Return the active execute_code mode — 'project' or 'strict'.

    Reads ``code_execution.mode`` from config.yaml; invalid values fall back
    to ``DEFAULT_EXECUTION_MODE`` ('project') with a log warning.

    Mode semantics:
      - ``project`` (default): scripts run in the session's working directory
        with the active virtual environment's python, so project dependencies
        (pandas, torch, project packages) and files resolve naturally.
      - ``strict``: scripts run in an isolated temp directory with
        ``sys.executable`` (duck-agent's python). Reproducible and the
        interpreter is guaranteed to work, but project deps and relative paths
        won't resolve.

    Env scrubbing and tool whitelist apply identically in both modes.
    """
    cfg_value = str(_load_config().get('mode', DEFAULT_EXECUTION_MODE)).strip().lower()
    if cfg_value in EXECUTION_MODES:
        return cfg_value
    logger.warning('Ignoring code_execution.mode=%r (expected one of %s), falling back to %r', cfg_value, EXECUTION_MODES, DEFAULT_EXECUTION_MODE)
    return DEFAULT_EXECUTION_MODE

@functools.lru_cache(maxsize=32)
def _is_usable_python(python_path: str) -> bool:
    """Check whether a candidate Python interpreter is usable for execute_code.

    Requires Python 3.8+ (f-strings and stdlib modules the RPC stubs need).
    Cached so we don't fork a subprocess on every execute_code call.
    """
    try:
        from agent.delegation_context import delegated_child_subprocess_env
        result = subprocess.run([python_path, '-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'], timeout=5, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW if _IS_WINDOWS else 0, stdin=subprocess.DEVNULL, env=delegated_child_subprocess_env())
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        return False

def _resolve_child_python(mode: str) -> str:
    """Pick the Python interpreter for the execute_code subprocess.

    In ``strict`` mode, always ``sys.executable`` — guaranteed to work and
    keeps behavior fully reproducible across sessions.

    In ``project`` mode, prefer the user's active virtualenv/conda env's
    python so ``import pandas`` etc. work. Falls back to ``sys.executable``
    if no venv is detected, the candidate binary is missing/not executable,
    or it fails a Python 3.8+ version check.
    """
    if mode != 'project':
        return sys.executable
    if _IS_WINDOWS:
        exe_names = ('python.exe', 'python3.exe')
        subdirs = ('Scripts',)
    else:
        exe_names = ('python', 'python3')
        subdirs = ('bin',)
    for var in ('VIRTUAL_ENV', 'CONDA_PREFIX'):
        root = os.environ.get(var, '').strip()
        if not root:
            continue
        for subdir in subdirs:
            for exe in exe_names:
                candidate = os.path.join(root, subdir, exe)
                if not (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
                    continue
                if _is_usable_python(candidate):
                    return candidate
                logger.info('execute_code: skipping %s=%s (Python version < 3.8 or broken). Using sys.executable instead.', var, candidate)
                return sys.executable
    return sys.executable

def _resolve_child_cwd(mode: str, staging_dir: str, task_id: str='') -> str:
    """Resolve the working directory for the execute_code subprocess.

    - ``strict``: the staging tmpdir (today's behavior).
    - ``project``: the session's own cwd — its per-session cwd record
      (written after every completed terminal command), then the raw
      per-session cwd override registered via ``session.cwd.set`` /
      ``register_task_env_overrides``, then the session's TERMINAL_CWD
      (same as the terminal tool), or ``os.getcwd()`` if none points at a
      real dir. Falls back to the staging tmpdir as a last resort so we
      never invoke Popen with a nonexistent cwd.

    This mirrors the resolution ladder file tools and the terminal use
    (record → registered override → TERMINAL_CWD), so all file-writing
    paths within a session agree on the working directory. (#56047)
    """
    if mode != 'project':
        return staging_dir
    if task_id:
        try:
            from tools.terminal_tool import get_session_cwd
            recorded = get_session_cwd(task_id)
        except Exception:
            recorded = None
        if recorded and os.path.isdir(recorded):
            return recorded
        try:
            from tools.file_tools import _registered_task_cwd_override
            session_cwd = _registered_task_cwd_override(task_id)
        except Exception:
            session_cwd = None
        if session_cwd and os.path.isdir(session_cwd):
            return session_cwd
    raw = os.environ.get('TERMINAL_CWD', '').strip()
    if raw:
        expanded = os.path.expanduser(raw)
        if os.path.isdir(expanded):
            return expanded
    here = os.getcwd()
    if os.path.isdir(here):
        return here
    return staging_dir
_TOOL_DOC_LINES = [('web_search', '  web_search(query: str, limit: int = 5) -> dict\n    Returns {"data": {"web": [{"url", "title", "description"}, ...]}}'), ('web_extract', '  web_extract(urls: list[str], char_limit: int = None) -> dict\n    Returns {"results": [{"url", "title", "content", "error"}, ...]} where content is markdown.\n    No LLM summarization. Pages over char_limit (default 15000) are head+tail truncated; full text stored on disk (path in the content footer).'), ('read_file', '  read_file(path: str, offset: int = 1, limit: int = 2000) -> dict\n    Lines are 1-indexed. Returns {"content": "...", "total_lines": N}'), ('write_file', '  write_file(path: str, content: str) -> dict\n    Always overwrites the entire file.'), ('search_files', '  search_files(pattern: str, target="content", path=".", file_glob=None, limit=50) -> dict\n    target: "content" (search inside files) or "files" (find files by name). Returns {"matches": [...]}'), ('patch', '  patch(path: str, old_string: str, new_string: str, replace_all: bool = False) -> dict\n    Replaces old_string with new_string in the file.'), ('terminal', '  terminal(command: str, timeout=None, workdir=None) -> dict\n    Foreground only (no background/pty). Returns {"output": "...", "exit_code": N}')]

def build_execute_code_schema(enabled_sandbox_tools: set=None, mode: str=None) -> dict:
    """Build the execute_code schema with description listing only enabled tools.

    When tools are disabled via ``duck-agent tools`` (e.g. web is turned off),
    the schema description should NOT mention web_search / web_extract —
    otherwise the model thinks they are available and keeps trying to use them.

    ``mode`` controls the working-directory sentence in the description:
      - ``'strict'``: scripts run in a temp dir (not the session's CWD)
      - ``'project'`` (default): scripts run in the session's CWD with the
        active venv's python
    If ``mode`` is None, the current ``code_execution.mode`` config is read.
    """
    if enabled_sandbox_tools is None:
        enabled_sandbox_tools = SANDBOX_ALLOWED_TOOLS
    if mode is None:
        mode = _get_execution_mode()
    tool_lines = '\n'.join((doc for name, doc in _TOOL_DOC_LINES if name in enabled_sandbox_tools))
    import_examples = [n for n in ('web_search', 'terminal') if n in enabled_sandbox_tools]
    if not import_examples:
        import_examples = sorted(enabled_sandbox_tools)[:2]
    if import_examples:
        import_str = ', '.join(import_examples) + ', ...'
    else:
        import_str = '...'
    if mode == 'strict':
        cwd_note = "Scripts run in their own temp dir, not the session's CWD — use absolute paths (os.path.expanduser('~/.duck-agent/.env')) or terminal()/read_file() for user files."
    else:
        cwd_note = "Scripts run in the session's working directory with the active venv's python, so project deps (pandas, etc.) and relative paths work like in terminal()."
    description = f'Run a Python script that calls Duck Agent tools programmatically. Use when you need 3+ tool calls with logic between them: filtering/reducing large outputs before they enter context, conditional branching, or loops (N pages/files, retry on failure). Use normal tool calls for single calls, results you must reason over in full, or anything needing user interaction.\n\nAvailable via `from hermes_tools import ...`:\n\n{tool_lines}\n\nLimits: 5-minute timeout, 50KB stdout cap, max 50 tool calls per script. terminal() is foreground-only (no background or pty).\n\n{cwd_note}\n\nPrint your final result to stdout; stdlib (json, re, csv, datetime, ...) is available for processing.\n\nBuilt-in helpers (no import): json_parse(text) — tolerant json.loads for terminal() output; shell_quote(s) — shlex.quote for dynamic shell args; retry(fn, max_attempts=3, delay=2) — exponential backoff for transient failures.'
    return {'name': 'execute_code', 'description': description, 'parameters': {'type': 'object', 'properties': {'code': {'type': 'string', 'description': f'Python code to execute. Import tools with `from hermes_tools import {import_str}` and print your final result to stdout.'}}, 'required': ['code']}}
EXECUTE_CODE_SCHEMA = build_execute_code_schema()
from tools.registry import registry, tool_error
registry.register(name='execute_code', toolset='code_execution', schema=EXECUTE_CODE_SCHEMA, handler=lambda args, **kw: execute_code(code=args.get('code', ''), task_id=kw.get('task_id'), enabled_tools=kw.get('enabled_tools')), check_fn=check_sandbox_requirements, emoji='🐍', max_result_size_chars=100000)