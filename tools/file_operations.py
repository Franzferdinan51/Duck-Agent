"""
File Operations Module

Provides file manipulation capabilities (read, write, patch, search) that work
across all terminal backends (local, docker, ssh, singularity, modal, daytona, vercel_sandbox).

The key insight is that all file operations can be expressed as shell commands,
so we wrap the terminal backend's execute() interface to provide a unified file API.

Usage:
    from tools.file_operations import ShellFileOperations
    from tools.terminal_tool import _active_environments
    
    # Get file operations for a terminal environment
    file_ops = ShellFileOperations(terminal_env)
    
    # Read a file
    result = file_ops.read_file("/path/to/file.py")
    
    # Write a file
    result = file_ops.write_file("/path/to/new.py", "print('hello')")
    
    # Search for content
    result = file_ops.search("TODO", path=".", file_glob="*.py")
"""
import os
import re
import difflib
import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, ClassVar
from pathlib import Path
from tools.binary_extensions import BINARY_EXTENSIONS
from agent.file_safety import build_write_denied_paths, build_write_denied_prefixes, get_write_denied_error, is_write_denied as _shared_is_write_denied
_HOME = str(Path.home())
WRITE_DENIED_PATHS = build_write_denied_paths(_HOME)
WRITE_DENIED_PREFIXES = build_write_denied_prefixes(_HOME)
_OSC_SEQUENCE_RE = re.compile('\\x1b\\][^\\x07\\x1b]*(?:\\x07|\\x1b\\\\)')
_FENCE_MARKER_RE = re.compile("'?\\x07?__HERMES_FENCE_[A-Za-z0-9]+__\\x07?'?")

def _strip_terminal_fence_leaks(text: str) -> str:
    """Strip leaked terminal fence wrappers from file read output."""
    if not text:
        return text
    cleaned_lines: List[str] = []
    for line in text.splitlines(keepends=True):
        had_terminal_wrapper = '__HERMES_FENCE_' in line or '\x1b]' in line
        cleaned = _OSC_SEQUENCE_RE.sub('', line)
        cleaned = _FENCE_MARKER_RE.sub('', cleaned)
        cleaned = cleaned.replace('\x07', '')
        if had_terminal_wrapper and cleaned.strip("'\r\n\t ") == '':
            continue
        cleaned_lines.append(cleaned)
    return ''.join(cleaned_lines)

def _detect_line_ending(sample: str) -> Optional[str]:
    """Return the dominant line ending in ``sample`` or None if undetermined.

    Looks at the first few line breaks and picks ``\\r\\n`` if any are
    present (Windows / DOS), otherwise ``\\n`` (Unix).  Returns ``None``
    for empty / single-line content where we can't tell.  Used to
    preserve the file's original line endings across write_file and
    patch operations — without this the agent's bare-LF tool args
    silently normalize Windows-line-ending files, and patch produces
    mixed endings when only a substituted region changes.
    """
    if not sample:
        return None
    head = sample[:4096]
    if '\r\n' in head:
        return '\r\n'
    if '\n' in head:
        return '\n'
    return None

def _normalize_line_endings(text: str, target: str) -> str:
    """Convert all line endings in ``text`` to ``target`` (``\\n`` or ``\\r\\n``).

    Idempotent: ``_normalize_line_endings(_normalize_line_endings(x, "\\r\\n"), "\\r\\n") == _normalize_line_endings(x, "\\r\\n")``.
    Strips lone ``\\r`` characters as well, so mixed-ending content is
    homogenized in a single pass.
    """
    lf_normalized = text.replace('\r\n', '\n').replace('\r', '\n')
    if target == '\n':
        return lf_normalized
    if target == '\r\n':
        return lf_normalized.replace('\n', '\r\n')
    return text
_UTF8_BOM = '\ufeff'

def _strip_bom(text: str) -> tuple[str, bool]:
    """Return (text-without-leading-BOM, had_bom).

    Only a single leading BOM is stripped; a BOM appearing mid-content is
    left alone (it's legitimate data there, not a file marker).
    """
    if text and text.startswith(_UTF8_BOM):
        return (text[len(_UTF8_BOM):], True)
    return (text, False)

def _has_bom(text: Optional[str]) -> bool:
    """True if ``text`` begins with a UTF-8 BOM."""
    return bool(text) and text.startswith(_UTF8_BOM)

def _is_write_denied(path: str) -> bool:
    """Return True if path is on the write deny list."""
    return _shared_is_write_denied(path)

@dataclass
class ReadResult:
    """Result from reading a file."""
    content: str = ''
    total_lines: int = 0
    file_size: int = 0
    truncated: bool = False
    hint: Optional[str] = None
    is_binary: bool = False
    is_image: bool = False
    base64_content: Optional[str] = None
    mime_type: Optional[str] = None
    dimensions: Optional[str] = None
    error: Optional[str] = None
    similar_files: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None and v != []}

@dataclass
class WriteResult:
    """Result from writing a file."""
    bytes_written: int = 0
    dirs_created: bool = False
    verified: Optional[bool] = None
    lint: Optional[Dict[str, Any]] = None
    lsp_diagnostics: Optional[str] = None
    error: Optional[str] = None
    warning: Optional[str] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}

@dataclass
class PatchResult:
    """Result from patching a file."""
    success: bool = False
    diff: str = ''
    files_modified: List[str] = field(default_factory=list)
    files_created: List[str] = field(default_factory=list)
    files_deleted: List[str] = field(default_factory=list)
    lint: Optional[Dict[str, Any]] = None
    lsp_diagnostics: Optional[str] = None
    error: Optional[str] = None
    no_change: bool = False
    note: Optional[str] = None

    def to_dict(self) -> dict:
        result: Dict[str, Any] = {'success': self.success}
        if self.no_change:
            result['no_change'] = True
        if self.note:
            result['note'] = self.note
        if self.diff:
            result['diff'] = self.diff
        if self.files_modified:
            result['files_modified'] = self.files_modified
        if self.files_created:
            result['files_created'] = self.files_created
        if self.files_deleted:
            result['files_deleted'] = self.files_deleted
        if self.lint:
            result['lint'] = self.lint
        if self.lsp_diagnostics:
            result['lsp_diagnostics'] = self.lsp_diagnostics
        if self.error:
            result['error'] = self.error
        return result

@dataclass
class SearchMatch:
    """A single search match."""
    path: str
    line_number: int
    content: str
    mtime: float = 0.0

@dataclass
class SearchResult:
    """Result from searching."""
    matches: List[SearchMatch] = field(default_factory=list)
    files: List[str] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)
    total_count: int = 0
    truncated: bool = False
    limit_reason: Optional[str] = None
    warning: Optional[str] = None
    error: Optional[str] = None
    _DENSIFY_MIN_MATCHES: ClassVar[int] = 5

    def _densify_matches(self) -> Optional[str]:
        """Render content-mode matches as a compact, path-grouped text block.

        The verbose form repeats the ``{"path","line","content"}`` keys and the
        full path string for every match. This groups consecutive matches by
        path (path printed once, then ``  <line>: <content>`` rows), which is
        lossless — every path, line number, and content byte is preserved — and
        readable by the model without any decode step.

        Returns ``None`` when densification is not worthwhile (too few matches),
        so the caller falls back to the verbose array.
        """
        if len(self.matches) < self._DENSIFY_MIN_MATCHES:
            return None
        lines: list[str] = []
        current_path: Optional[str] = None
        for m in self.matches:
            if m.path != current_path:
                lines.append(m.path)
                current_path = m.path
            lines.append(f'  {m.line_number}: {m.content.rstrip()}')
        return '\n'.join(lines)

    def to_dict(self, densify: bool=False) -> dict:
        result: dict[str, object] = {'total_count': self.total_count}
        if self.matches:
            dense = self._densify_matches() if densify else None
            if dense is not None:
                result['matches_format'] = "path-grouped: each file path on its own line, followed by indented '<line>: <content>' rows for matches in that file"
                result['matches_text'] = dense
            else:
                result['matches'] = [{'path': m.path, 'line': m.line_number, 'content': m.content} for m in self.matches]
        if self.files:
            result['files'] = self.files
        if self.counts:
            result['counts'] = self.counts
        if self.truncated:
            result['truncated'] = True
        if self.limit_reason:
            result['limit_reason'] = self.limit_reason
        if self.warning:
            result['warning'] = self.warning
        if self.error:
            result['error'] = self.error
        return result

@dataclass
class LintResult:
    """Result from linting a file."""
    success: bool = True
    skipped: bool = False
    output: str = ''
    message: str = ''

    def to_dict(self) -> dict:
        if self.skipped:
            return {'status': 'skipped', 'message': self.message}
        result = {'status': 'ok' if self.success else 'error', 'output': self.output}
        if self.message:
            result['message'] = self.message
        return result

@dataclass
class ExecuteResult:
    """Result from executing a shell command."""
    stdout: str = ''
    exit_code: int = 0
_SEARCH_TIMEOUT_MARKER_RE = re.compile('\\n?\\[Command timed out after \\d+s\\]\\s*$')

def _search_stdout_and_limit(result: ExecuteResult) -> tuple[str, Optional[str]]:
    """Return stdout cleaned for parsing and a limit reason for search timeouts."""
    if result.exit_code == 124:
        return (_SEARCH_TIMEOUT_MARKER_RE.sub('', result.stdout), 'search_timeout')
    return (result.stdout, None)

def _split_tool_diagnostics(output: str) -> tuple[str, str]:
    """Separate rg/grep diagnostic lines from real match output.

    ``_exec`` runs commands with ``stderr=subprocess.STDOUT``, so error and
    warning text from ``rg``/``grep`` is interleaved with match lines in a
    single stream. Diagnostics must not be parsed as matches, and on a hard
    failure they are the error message to surface.

    Returns ``(diagnostics, payload)`` where ``payload`` contains only lines
    that look like real search output — a match line (``file:line:content``),
    a files-only path, a count line, or a context line/separator. Everything
    else (tool-prefixed errors, rg's multi-line ``regex parse error`` block
    with its indented carets, blank lines) is folded into ``diagnostics``.

    Classifying by *shape* rather than by error prefix is what lets the
    exit-2 guard distinguish a pure failure (no usable payload → surface the
    error) from a partial failure (some files matched, one was unreadable →
    keep the matches). It also means error text can never be mis-parsed as a
    match, a latent bug that predates the exit-code fix.
    """
    diagnostics: list[str] = []
    payload: list[str] = []
    for line in output.split('\n'):
        if not line.strip():
            continue
        stripped = line.lstrip()
        if stripped.startswith('rg: ') or stripped.startswith('grep: '):
            diagnostics.append(line)
            continue
        if line == '--' or _SEARCH_OUTPUT_RE.match(line):
            payload.append(line)
        else:
            diagnostics.append(line)
    return ('\n'.join(diagnostics), '\n'.join(payload))
_SEARCH_OUTPUT_RE = re.compile('^([A-Za-z]:)?[^\\s:][^\\n]*?[:\\-]\\d|^[^\\s:][^\\s]*$')

def _parse_search_context_line(line: str) -> tuple[str, int, str] | None:
    """Parse grep/rg context output in ``path-line-content`` format.

    Context lines are ambiguous because filenames may legitimately contain
    ``-<digits>-`` segments. Prefer the rightmost numeric separator so a path
    like ``dir/file-12-name.py-8-context`` resolves to
    ``dir/file-12-name.py`` line ``8`` instead of truncating at ``file``.
    """
    if not line or line == '--':
        return None
    match = None
    for candidate in re.finditer('-(\\d+)-', line):
        match = candidate
    if match is None:
        return None
    path = line[:match.start()]
    if not path:
        return None
    return (path, int(match.group(1)), line[match.end():])

class FileOperations(ABC):
    """Abstract interface for file operations across terminal backends."""

    @abstractmethod
    def read_file(self, path: str, offset: int=1, limit: int=2000) -> ReadResult:
        """Read a file with pagination support."""
        ...

    @abstractmethod
    def read_file_raw(self, path: str) -> ReadResult:
        """Read the complete file content as a plain string.

        No pagination, no line-number prefixes, no per-line truncation.
        Returns ReadResult with .content = full file text, .error set on
        failure. Always reads to EOF regardless of file size.
        """
        ...

    @abstractmethod
    def write_file(self, path: str, content: str, pre_content: Optional[str]=None) -> WriteResult:
        """Write content to a file, creating directories as needed."""
        ...

    @abstractmethod
    def patch_replace(self, path: str, old_string: str, new_string: str, replace_all: bool=False) -> PatchResult:
        """Replace text in a file using fuzzy matching."""
        ...

    @abstractmethod
    def patch_v4a(self, patch_content: str) -> PatchResult:
        """Apply a V4A format patch."""
        ...

    @abstractmethod
    def delete_file(self, path: str) -> WriteResult:
        """Delete a file. Returns WriteResult with .error set on failure."""
        ...

    def delete_path(self, path: str, recursive: bool=False) -> WriteResult:
        """Cross-platform delete that handles files and (with recursive=True)
        directory trees. Default implementation delegates to ``delete_file``
        for the non-recursive case; backends with native recursive support
        should override.
        """
        if recursive:
            return WriteResult(error='Recursive delete not implemented for this backend')
        return self.delete_file(path)

    @abstractmethod
    def move_file(self, src: str, dst: str) -> WriteResult:
        """Move/rename a file from src to dst. Returns WriteResult with .error set on failure."""
        ...

    @abstractmethod
    def search(self, pattern: str, path: str='.', target: str='content', file_glob: Optional[str]=None, limit: int=50, offset: int=0, output_mode: str='content', context: int=0) -> SearchResult:
        """Search for content or files."""
        ...
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.ico'}
LINTERS = {'.py': 'python -m py_compile {file} 2>&1', '.js': 'node --check {file} 2>&1', '.ts': 'npx tsc --noEmit {file} 2>&1', '.go': 'go vet {file} 2>&1', '.rs': 'rustfmt --check {file} 2>&1'}
_SHELL_LINTER_LSP_REDUNDANT = frozenset({'.ts', '.go', '.rs'})
_LINTER_UNUSABLE_PATTERNS = {'npx': ('this is not the tsc command you are looking for', 'could not determine executable to run', 'not found in npm registry'), 'rustfmt': ('no input filename given', 'error: not a workspace'), 'go': ('cannot find package', 'go: cannot find main module')}

def _looks_like_linter_unusable(base_cmd: str, output: str) -> bool:
    """Return True iff ``output`` from ``base_cmd`` indicates the linter
    itself couldn't run (a tooling gap), as opposed to a real lint error
    in the file being checked.

    ``base_cmd`` is the first word of the linter command line (``npx``,
    ``rustfmt``, ``go``, ...).  ``output`` is the stdout/stderr captured
    from running it.
    """
    patterns = _LINTER_UNUSABLE_PATTERNS.get(base_cmd)
    if not patterns:
        return False
    lower = output.lower()
    return any((p in lower for p in patterns))

def _lint_json_inproc(content: str) -> tuple[bool, str]:
    """In-process JSON syntax check.  Returns (ok, error_message)."""
    import json as _json
    try:
        _json.loads(content)
        return (True, '')
    except _json.JSONDecodeError as e:
        return (False, f'JSONDecodeError: {e.msg} (line {e.lineno}, column {e.colno})')
    except Exception as e:
        return (False, f'{type(e).__name__}: {e}')

def _lint_yaml_inproc(content: str) -> tuple[bool, str]:
    """In-process YAML syntax check.  Returns (ok, error_message).

    Skipped gracefully if PyYAML isn't installed — YAML parsing is optional.

    Deliberately a *syntax-only* scan (``yaml.parse``), not ``safe_load``:
    loading rejects perfectly valid YAML that merely isn't a single plain
    document — multi-document streams (``---``-separated Kubernetes
    manifests raise ``ComposerError``) and application-defined tags
    (CloudFormation ``!Sub``/``!Ref``, Ansible ``!vault`` raise
    ``ConstructorError``).  Those are content conventions for whatever
    consumes the file, not syntax errors, and this linter's verdict is
    used as a fail-closed WRITE gate in ``write_file`` — a false positive
    here refuses a legitimate write outright.  ``yaml.parse`` still
    catches real scanner/parser failures (unclosed quotes, bad
    indentation, tab-mangled block maps).
    """
    try:
        import yaml as _yaml
    except ImportError:
        return (True, '__SKIP__')
    try:
        for _event in _yaml.parse(content):
            pass
        return (True, '')
    except _yaml.YAMLError as e:
        return (False, f'YAMLError: {e}')
    except Exception as e:
        return (False, f'{type(e).__name__}: {e}')

def _lint_toml_inproc(content: str) -> tuple[bool, str]:
    """In-process TOML syntax check (stdlib tomllib, Python 3.11+)."""
    import tomllib as _toml
    try:
        _toml.loads(content)
        return (True, '')
    except Exception as e:
        return (False, f'{type(e).__name__}: {e}')

def _lint_python_inproc(content: str) -> tuple[bool, str]:
    """In-process Python syntax check via ast.parse.

    Catches SyntaxError, IndentationError, and everything else the
    ast module rejects — matching py_compile's scope but with no
    subprocess overhead and no dependency on a ``python`` in PATH.
    """
    import ast as _ast
    try:
        _ast.parse(content)
        return (True, '')
    except SyntaxError as e:
        loc = f' (line {e.lineno}, column {e.offset})' if e.lineno else ''
        return (False, f'{type(e).__name__}: {e.msg}{loc}')
    except Exception as e:
        return (False, f'{type(e).__name__}: {e}')
LINTERS_INPROC = {'.py': _lint_python_inproc, '.json': _lint_json_inproc, '.yaml': _lint_yaml_inproc, '.yml': _lint_yaml_inproc, '.toml': _lint_toml_inproc}
_FAIL_CLOSED_INPROC_EXTS = frozenset({'.json', '.yaml', '.yml', '.toml'})
MAX_LINES = 2000
MAX_LINE_LENGTH = 2000
MAX_FILE_SIZE = 50 * 1024
DEFAULT_READ_OFFSET = 1
DEFAULT_READ_LIMIT = 2000
DEFAULT_SEARCH_OFFSET = 0
DEFAULT_SEARCH_LIMIT = 50

def _coerce_int(value: Any, default: int) -> int:
    """Best-effort integer coercion for tool pagination inputs."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

def normalize_read_pagination(offset: Any=DEFAULT_READ_OFFSET, limit: Any=DEFAULT_READ_LIMIT) -> tuple[int, int]:
    """Return safe read_file pagination bounds.

    Tool schemas declare minimum/maximum values, but not every caller or
    provider enforces schemas before dispatch. Clamp here so invalid values
    cannot leak into sed ranges like ``0,-1p``.

    The upper bound on ``limit`` comes from ``tool_output.max_lines`` in
    config.yaml (defaults to the module-level ``MAX_LINES`` constant).
    """
    from tools.tool_output_limits import get_max_lines
    max_lines = get_max_lines()
    normalized_offset = max(1, _coerce_int(offset, DEFAULT_READ_OFFSET))
    normalized_limit = _coerce_int(limit, DEFAULT_READ_LIMIT)
    normalized_limit = max(1, min(normalized_limit, max_lines))
    return (normalized_offset, normalized_limit)

def normalize_search_pagination(offset: Any=DEFAULT_SEARCH_OFFSET, limit: Any=DEFAULT_SEARCH_LIMIT) -> tuple[int, int]:
    """Return safe search pagination bounds for shell head/tail pipelines."""
    normalized_offset = max(0, _coerce_int(offset, DEFAULT_SEARCH_OFFSET))
    normalized_limit = max(1, _coerce_int(limit, DEFAULT_SEARCH_LIMIT))
    return (normalized_offset, normalized_limit)
_REGEX_NEWLINE_ESCAPE_RE = re.compile('(?<!\\\\)(?:\\\\\\\\)*\\\\n')

def _pattern_has_regex_newline(pattern: str) -> bool:
    """Return True when a content-search regex tries to match a newline.

    ``search_files`` runs rg/grep in line-oriented mode, not rg
    ``-U``/``--multiline`` mode, so newline regexes cannot match across
    lines.  Detect both a literal newline already decoded into the tool
    argument and a regex ``
`` escape (odd number of backslashes before
    ``n``).  Even backslashes, e.g. ``\\n``, mean a literal backslash+n
    search and should not warn.
    """
    return '\n' in pattern or bool(_REGEX_NEWLINE_ESCAPE_RE.search(pattern))

def _is_line_oriented_newline_error(error: Optional[str]) -> bool:
    """Return True for rg's hard error when multiline mode is required."""
    if not error:
        return False
    return 'literal "\\n" is not allowed' in error and '--multiline' in error

def _maybe_warn_line_oriented_newline_pattern(result: SearchResult, pattern: str) -> SearchResult:
    """Attach a newline-regex warning only when search found no usable results."""
    if result.total_count != 0 or not _pattern_has_regex_newline(pattern):
        return result
    if result.error and (not _is_line_oriented_newline_error(result.error)):
        return result
    result.error = None
    result.warning = '0 results found. Note: search_files content search is line-oriented and does not run ripgrep with -U/--multiline, so `\\n` in the regex does not match line breaks. Use context=N to inspect neighboring lines, or escape as `\\\\n` when searching for a literal backslash+n.'
    return result

class ShellFileOperations(FileOperations):
    """
    File operations implemented via shell commands.
    
    Works with ANY terminal backend that has execute(command, cwd) method.
    This includes local, docker, singularity, ssh, modal, and daytona environments.
    """

    def __init__(self, terminal_env, cwd: str=None):
        """
        Initialize file operations with a terminal environment.

        Args:
            terminal_env: Any object with execute(command, cwd) method.
                         Returns {"output": str, "returncode": int}
            cwd: Optional explicit fallback cwd when the terminal env has
                 no cwd attribute (rare — most backends track cwd live).

        Note:
            Every _exec() call prefers the LIVE ``terminal_env.cwd`` over
            ``self.cwd`` so ``cd`` commands run via the terminal tool are
            picked up immediately.  ``self.cwd`` is only used as a fallback
            when the env has no cwd at all — it is NOT the authoritative
            cwd, despite being settable at init time.

            Historical bug (fixed): prior versions of this class used the
            init-time cwd for every _exec() call, which caused relative
            paths passed to patch/read/write to target the wrong directory
            after the user ran ``cd`` in the terminal.  Patches would
            claim success and return a plausible diff but land in the
            original directory, producing apparent silent failures.
        """
        self.env = terminal_env
        self.cwd = cwd or getattr(terminal_env, 'cwd', None) or getattr(getattr(terminal_env, 'config', None), 'cwd', None) or '/'
        self._command_cache: Dict[str, bool] = {}

    def _exec(self, command: str, cwd: str=None, timeout: int=None, stdin_data: str=None) -> ExecuteResult:
        """Execute command via terminal backend.

        Args:
            stdin_data: If provided, piped to the process's stdin instead of
                        embedding in the command string. Bypasses ARG_MAX.

        Cwd resolution order (critical — see class docstring):
          1. Explicit ``cwd`` arg (if provided)
          2. Live ``self.env.cwd`` (tracks ``cd`` commands run via terminal)
          3. Init-time ``self.cwd`` (fallback when env has no cwd attribute)

        This ordering ensures relative paths in file operations follow the
        terminal's current directory — not the directory this file_ops was
        originally created in.  See test_file_ops_cwd_tracking.py.
        """
        kwargs = {}
        if timeout:
            kwargs['timeout'] = timeout
        if stdin_data is not None:
            kwargs['stdin_data'] = stdin_data
        effective_cwd = cwd or getattr(self.env, 'cwd', None) or self.cwd
        result = self.env.execute(command, cwd=effective_cwd, **kwargs)
        return ExecuteResult(stdout=result.get('output', ''), exit_code=result.get('returncode', 0))

    def _has_command(self, cmd: str) -> bool:
        """Check if a command exists in the environment (cached)."""
        if cmd not in self._command_cache:
            result = self._exec(f"command -v {cmd} >/dev/null 2>&1 && echo 'yes'")
            self._command_cache[cmd] = result.stdout.strip() == 'yes'
        return self._command_cache[cmd]

    def _is_likely_binary(self, path: str, content_sample: str=None) -> bool:
        """
        Check if a file is likely binary.
        
        Uses extension check (fast) + content analysis (fallback).
        """
        ext = os.path.splitext(path)[1].lower()
        if ext in BINARY_EXTENSIONS:
            return True
        if content_sample:
            if '�' in content_sample[:1000]:
                return True
            non_printable = sum((1 for c in content_sample[:1000] if ord(c) < 32 and c not in '\n\r\t'))
            return non_printable / min(len(content_sample), 1000) > 0.3
        return False

    def _is_image(self, path: str) -> bool:
        """Check if file is an image we can return as base64."""
        ext = os.path.splitext(path)[1].lower()
        return ext in IMAGE_EXTENSIONS

    def _add_line_numbers(self, content: str, start_line: int=1) -> str:
        """Add line numbers to content in ``LINE_NUM|CONTENT`` format.

        The gutter uses a compact ``<n>|`` prefix (e.g. ``34|foo``) rather
        than a fixed-width zero/space-padded one (``    34|foo``). The
        padding was pure token overhead: on dense source the padded gutter
        cost ~48% more tokens than the bare content and ~16% more than the
        compact form, because the leading spaces + zero-padding tokenize
        into extra tokens on every single line. An A/B (Sonnet 4.6, 2
        passes) showed the compact gutter matches the padded gutter on
        line-reference / patch / value-lookup / structure tasks (4/4 both),
        while dropping line numbers entirely regressed line-referencing
        (the model hand-counted and was off-by-one, 3/4) — so we keep the
        numbers, just not the padding.
        """
        from tools.tool_output_limits import get_max_line_length
        max_line_length = get_max_line_length()
        lines = content.split('\n')
        numbered = []
        for i, line in enumerate(lines, start=start_line):
            if len(line) > max_line_length:
                line = line[:max_line_length] + '... [truncated]'
            numbered.append(f'{i}|{line}')
        return '\n'.join(numbered)

    def _expand_path(self, path: str) -> str:
        """
        Expand shell-style paths like ~ and ~user to absolute paths.
        
        This must be done BEFORE shell escaping, since ~ doesn't expand
        inside single quotes.
        """
        if not path:
            return path
        if path.startswith('~'):
            result = self._exec('echo $HOME')
            if result.exit_code == 0 and result.stdout.strip():
                home = result.stdout.strip()
                if path == '~':
                    return home
                elif path.startswith('~/'):
                    return home + path[1:]
                rest = path[1:]
                slash_idx = rest.find('/')
                username = rest[:slash_idx] if slash_idx >= 0 else rest
                if username and re.fullmatch('[a-zA-Z0-9._-]+', username):
                    expand_result = self._exec(f'echo ~{username}')
                    if expand_result.exit_code == 0 and expand_result.stdout.strip():
                        user_home = expand_result.stdout.strip()
                        suffix = path[1 + len(username):]
                        return user_home + suffix
        return path

    def _escape_shell_arg(self, arg: str) -> str:
        """Escape a string for safe use in shell commands.

        On Windows native drive paths (``C:\\Users\\x`` / ``C:/Users/x``)
        and mixed MSYS leftovers (``/c/Users\\x``) are rewritten to the
        Git Bash ``/c/Users/x`` form via ``_bash_safe_path``: bash eats
        backslashes and MSYS otherwise mangles drive paths into the
        ``Directory \\drivers\\etc does not exist`` failure class. Reuses
        the env-layer translator so shell file ops and the terminal ``cd``
        agree on the path form. No-op off Windows and for plain POSIX paths.
        """
        from tools.environments.local import _bash_safe_path
        arg = _bash_safe_path(arg)
        return "'" + arg.replace("'", '\'"\'"\'') + "'"

    def _atomic_write(self, path: str, content: str) -> 'ExecuteResult':
        """Write ``content`` to ``path`` atomically via temp-file + rename.

        Streams ``content`` over stdin into a temp file in the SAME
        directory as ``path`` (so the final ``mv`` is a real rename on the
        same filesystem, not a non-atomic cross-device copy), preserves the
        existing file's mode if it exists, then renames over the target.
        On any failure the temp file is removed so we never leak a partial
        ``.duck-agent-tmp`` file next to the user's data, and the original file
        is left untouched. Content rides stdin so there is no ARG_MAX limit.

        ``mkdir -p`` for the parent directory is folded into this script
        (one fewer subprocess vs. a separate ``mkdir -p`` call).

        Returns an :class:`ExecuteResult`; ``exit_code == 0`` means the file
        was swapped into place atomically. A non-zero exit means nothing was
        renamed and the original (if any) is intact.
        """
        q_path = self._escape_shell_arg(path)
        parent = os.path.dirname(path) or '.'
        q_parent = self._escape_shell_arg(parent)
        tmpl = self._escape_shell_arg('.duck-agent-tmp.XXXXXX')
        script = f'set -e; d={q_parent}; t={q_path}; if [ -L "$t" ]; then rt="$(readlink -f "$t" 2>/dev/null || realpath "$t" 2>/dev/null || true)"; [ -n "$rt" ] && {{ t="$rt"; d="$(dirname "$t")"; }}; fi; mkdir -p "$d"; tmp="$(mktemp -p "$d" ' + tmpl + ' 2>/dev/null || mktemp "$d/.duck-agent-tmp.$$.XXXXXX" 2>/dev/null || { tmp="$d/.duck-agent-tmp.$$"; : > "$tmp" && echo "$tmp"; })"; [ -n "$tmp" ] || { echo "atomic write: could not create temp file" >&2; exit 1; }; trap \'rm -f \\"$tmp\\"\' EXIT; if [ -e "$t" ]; then m="$(stat -c%a "$t" 2>/dev/null || stat -f%Lp "$t" 2>/dev/null || true)"; [ -n "$m" ] && chmod "$m" "$tmp" 2>/dev/null || true; fi; cat > "$tmp"; if [ ! -e "$t" ]; then chmod "=rw" "$tmp" 2>/dev/null || true; fi; mv -f "$tmp" "$t"; trap - EXIT'
        return self._exec(script, stdin_data=content)

    def _detect_file_line_ending(self, path: str, pre_content: Optional[str]=None) -> Optional[str]:
        """Detect the dominant line ending of a file on disk.

        If ``pre_content`` is already available (we just read the file
        for lint/LSP purposes), inspect that — zero extra exec calls.
        Otherwise issue a tiny ``head -c 4096`` to sample the first 4KB.

        Returns ``"\\r\\n"`` for CRLF (Windows), ``"\\n"`` for LF (Unix),
        or ``None`` if undetermined (new file, empty file, single-line
        file with no line break in the first chunk).
        """
        if pre_content:
            return _detect_line_ending(pre_content)
        head_cmd = f'head -c 4096 {self._escape_shell_arg(path)} 2>/dev/null'
        head_result = self._exec(head_cmd)
        if head_result.exit_code != 0 or not head_result.stdout:
            return None
        return _detect_line_ending(head_result.stdout)

    def _file_has_bom(self, path: str, pre_content: Optional[str]=None) -> bool:
        """Whether the file on disk starts with a UTF-8 BOM.

        Always probes the first 3 bytes on disk — do NOT trust
        ``pre_content`` for BOM detection because the most common
        provider (``read_file_raw``) deliberately strips BOMs so the
        agent never sees U+FEFF glyphs.  Passing BOM-stripped content
        through ``pre_content`` would cause a false-negative and
        silently remove the marker on rewrite.

        A missing/empty file returns False (new writes get no BOM
        unless the caller explicitly includes one).
        """
        head_cmd = f'head -c 3 {self._escape_shell_arg(path)} 2>/dev/null'
        head_result = self._exec(head_cmd)
        if head_result.exit_code != 0 or not head_result.stdout:
            return False
        return _has_bom(head_result.stdout)

    def _unified_diff(self, old_content: str, new_content: str, filename: str) -> str:
        """Generate unified diff between old and new content."""
        old_lines = old_content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        diff = difflib.unified_diff(old_lines, new_lines, fromfile=f'a/{filename}', tofile=f'b/{filename}')
        return ''.join(diff)

    def read_file(self, path: str, offset: int=1, limit: int=2000) -> ReadResult:
        """
        Read a file with pagination, binary detection, and line numbers.
        
        Args:
            path: File path (absolute or relative to cwd)
            offset: Line number to start from (1-indexed, default 1)
            limit: Maximum lines to return (default 500, max 2000)
        
        Returns:
            ReadResult with content, metadata, or error info
        """
        path = self._expand_path(path)
        offset, limit = normalize_read_pagination(offset, limit)
        stat_cmd = f'wc -c < {self._escape_shell_arg(path)} 2>/dev/null'
        stat_result = self._exec(stat_cmd)
        if stat_result.exit_code != 0:
            return self._suggest_similar_files(path)
        stat_output = _strip_terminal_fence_leaks(stat_result.stdout)
        try:
            file_size = int(stat_output.strip())
        except ValueError:
            file_size = 0
        if file_size > MAX_FILE_SIZE:
            pass
        if self._is_image(path):
            return ReadResult(is_image=True, is_binary=True, file_size=file_size, hint='Image file detected. Automatically redirected to vision_analyze tool. Use vision_analyze with this file path to inspect the image contents.')
        sample_cmd = f'head -c 1000 {self._escape_shell_arg(path)} 2>/dev/null'
        sample_result = self._exec(sample_cmd)
        sample_output = _strip_terminal_fence_leaks(sample_result.stdout)
        if self._is_likely_binary(path, sample_output):
            return ReadResult(is_binary=True, file_size=file_size, error='Binary file - cannot display as text. Use appropriate tools to handle this file type.')
        end_line = offset + limit - 1
        read_cmd = f"sed -n '{offset},{end_line}p' {self._escape_shell_arg(path)}"
        read_result = self._exec(read_cmd)
        if read_result.exit_code != 0:
            return ReadResult(error=f'Failed to read file: {read_result.stdout}')
        read_output = _strip_terminal_fence_leaks(read_result.stdout)
        if offset == 1:
            read_output, _ = _strip_bom(read_output)
        wc_cmd = f'wc -l < {self._escape_shell_arg(path)}'
        wc_result = self._exec(wc_cmd)
        wc_output = _strip_terminal_fence_leaks(wc_result.stdout)
        try:
            total_lines = int(wc_output.strip())
        except ValueError:
            total_lines = 0
        truncated = total_lines > end_line
        hint = None
        if truncated:
            hint = f'Use offset={end_line + 1} to continue reading (showing {offset}-{end_line} of {total_lines} lines)'
        return ReadResult(content=self._add_line_numbers(read_output, offset), total_lines=total_lines, file_size=file_size, truncated=truncated, hint=hint)

    def _suggest_similar_files(self, path: str) -> ReadResult:
        """Suggest similar files when the requested file is not found."""
        dir_path = os.path.dirname(path) or '.'
        filename = os.path.basename(path)
        basename_no_ext = os.path.splitext(filename)[0]
        ext = os.path.splitext(filename)[1].lower()
        lower_name = filename.lower()
        ls_cmd = f'ls -1 {self._escape_shell_arg(dir_path)} 2>/dev/null | head -50'
        ls_result = self._exec(ls_cmd)
        scored: list = []
        if ls_result.exit_code == 0 and ls_result.stdout.strip():
            for f in ls_result.stdout.strip().split('\n'):
                if not f:
                    continue
                lf = f.lower()
                score = 0
                if lf == lower_name:
                    score = 100
                elif os.path.splitext(f)[0].lower() == basename_no_ext.lower():
                    score = 90
                elif lf.startswith(lower_name) or lower_name.startswith(lf):
                    score = 70
                elif lower_name in lf:
                    score = 60
                elif lf in lower_name and len(lf) > 2:
                    score = 40
                elif ext and os.path.splitext(f)[1].lower() == ext:
                    common = set(lower_name) & set(lf)
                    if len(common) >= max(len(lower_name), len(lf)) * 0.4:
                        score = 30
                if score > 0:
                    scored.append((score, os.path.join(dir_path, f)))
        scored.sort(key=lambda x: -x[0])
        similar = [fp for _, fp in scored[:5]]
        return ReadResult(error=f'File not found: {path}', similar_files=similar)

    def read_file_raw(self, path: str) -> ReadResult:
        """Read the complete file content as a plain string.

        No pagination, no line-number prefixes, no per-line truncation.
        Uses cat so the full file is returned regardless of size.
        """
        path = self._expand_path(path)
        stat_cmd = f'wc -c < {self._escape_shell_arg(path)} 2>/dev/null'
        stat_result = self._exec(stat_cmd)
        if stat_result.exit_code != 0:
            return self._suggest_similar_files(path)
        stat_output = _strip_terminal_fence_leaks(stat_result.stdout)
        try:
            file_size = int(stat_output.strip())
        except ValueError:
            file_size = 0
        if self._is_image(path):
            return ReadResult(is_image=True, is_binary=True, file_size=file_size)
        sample_result = self._exec(f'head -c 1000 {self._escape_shell_arg(path)} 2>/dev/null')
        sample_output = _strip_terminal_fence_leaks(sample_result.stdout)
        if self._is_likely_binary(path, sample_output):
            return ReadResult(is_binary=True, file_size=file_size, error='Binary file — cannot display as text.')
        cat_result = self._exec(f'cat {self._escape_shell_arg(path)}')
        if cat_result.exit_code != 0:
            return ReadResult(error=f'Failed to read file: {cat_result.stdout}')
        raw_content, _ = _strip_bom(_strip_terminal_fence_leaks(cat_result.stdout))
        return ReadResult(content=raw_content, file_size=file_size)

    def delete_file(self, path: str) -> WriteResult:
        """Delete a single file.

        Cross-platform: runs via ``python -c`` against the terminal env's
        Python so it works on Windows shells (``cmd.exe``/PowerShell) that
        don't ship ``rm``. Directories are rejected here — use
        ``delete_path(recursive=True)`` for trees.
        """
        return self._python_delete(path, recursive=False)

    def delete_path(self, path: str, recursive: bool=False) -> WriteResult:
        """Cross-platform delete that handles files and (with recursive=True)
        directory trees. Always preferred over emitting ``rm -rf`` /
        ``Remove-Item -Recurse`` directly so the same tool call works on
        every backend (local / docker / ssh / Windows).
        """
        return self._python_delete(path, recursive=recursive)

    def _python_delete(self, path: str, recursive: bool) -> WriteResult:
        path = self._expand_path(path)
        denied = get_write_denied_error(path, verb='Delete')
        if denied:
            return WriteResult(error=denied)
        snippet = f"import shutil, pathlib, sys\np = pathlib.Path({path!r})\nrecursive = {bool(recursive)!r}\ntry:\n    if p.is_dir() and not p.is_symlink():\n        if recursive:\n            shutil.rmtree(p)\n        else:\n            print('is a directory: ' + str(p), file=sys.stderr); sys.exit(2)\n    else:\n        p.unlink()\nexcept FileNotFoundError:\n    pass\nexcept Exception as exc:\n    print(str(exc), file=sys.stderr); sys.exit(1)\n"
        result = self._exec(f'python3 -c {self._escape_shell_arg(snippet)}')
        if result.exit_code != 0 and 'python3' in (result.stdout or ''):
            result = self._exec(f'python -c {self._escape_shell_arg(snippet)}')
        if result.exit_code != 0:
            return WriteResult(error=f"Failed to delete {path}: {(result.stdout or '').strip() or 'unknown error'}")
        return WriteResult()

    def move_file(self, src: str, dst: str) -> WriteResult:
        """Move a file via mv."""
        src = self._expand_path(src)
        dst = self._expand_path(dst)
        for p in (src, dst):
            denied = get_write_denied_error(p, verb='Move')
            if denied:
                return WriteResult(error=denied)
        result = self._exec(f'mv {self._escape_shell_arg(src)} {self._escape_shell_arg(dst)}')
        if result.exit_code != 0:
            return WriteResult(error=f'Failed to move {src} -> {dst}: {result.stdout}')
        return WriteResult()

    def write_file(self, path: str, content: str, pre_content: Optional[str]=None) -> WriteResult:
        """
        Write content to a file, creating parent directories as needed.

        Pipes content through stdin to avoid OS ARG_MAX limits on large
        files. The content never appears in the shell command string —
        only the file path does.

        Before anything touches disk, a fail-closed syntax gate runs
        against the CANDIDATE content: if ``path``'s extension is in
        ``_FAIL_CLOSED_INPROC_EXTS`` (JSON/YAML/TOML — structured data
        formats where a parse failure always means corruption) and the
        candidate content doesn't parse, the write is refused outright.
        No temp file, no rename, nothing on disk changes.

        After a write that clears the gate, runs a post-first / pre-lazy
        lint check via ``_check_lint_delta()``.  If the new content is
        clean, the lint call is O(one parse).  If the new content has
        errors the gate didn't already catch (i.e. errors from a linter
        outside ``_FAIL_CLOSED_INPROC_EXTS``, such as Python), the
        pre-write content is linted too and only errors newly introduced
        by this write are surfaced — pre-existing problems are filtered
        out so the agent isn't distracted chasing them.

        Args:
            path: File path to write
            content: Content to write
            pre_content: Pre-edit file content if the caller already has it
                (e.g. patch_replace read the file for fuzzy matching).
                When provided, skips a redundant ``cat`` subprocess to
                re-read the file for lint baseline / line-ending
                detection. BOM detection always probes disk (the most
                common provider — ``read_file_raw`` — strips BOMs, so
                trusting ``pre_content`` for BOM would cause false
                negatives and silent marker loss on rewrite). When
                None, reads from disk as before.

        Returns:
            WriteResult with bytes written, lint summary, or error.
        """
        path = self._expand_path(path)
        denied = get_write_denied_error(path)
        if denied:
            return WriteResult(error=denied)
        ext = os.path.splitext(path)[1].lower()
        inproc_linter = LINTERS_INPROC.get(ext) if ext in _FAIL_CLOSED_INPROC_EXTS else None
        if inproc_linter is not None:
            _ok, _lint_err = inproc_linter(content)
            if not _ok and _lint_err != '__SKIP__':
                return WriteResult(error=f"Refusing to write '{path}': candidate content fails {ext} syntax validation ({_lint_err}). The file was NOT created or modified. Fix the content and retry.")
        want_pre = ext in LINTERS_INPROC or self._lsp_handles_extension(ext)
        if want_pre:
            if pre_content is not None:
                pass
            else:
                read_cmd = f'cat {self._escape_shell_arg(path)} 2>/dev/null'
                read_result = self._exec(read_cmd)
                if read_result.exit_code == 0 and read_result.stdout:
                    pre_content = read_result.stdout
        original_ending = self._detect_file_line_ending(path, pre_content)
        if original_ending == '\r\n':
            content = _normalize_line_endings(content, '\r\n')
        if self._file_has_bom(path, pre_content) and (not _has_bom(content)):
            content = _UTF8_BOM + content
        self._snapshot_lsp_baseline(path)
        parent = os.path.dirname(path)
        dirs_created = bool(parent)
        write_result = self._atomic_write(path, content)
        if write_result.exit_code != 0:
            return WriteResult(error=f'Failed to write file: {write_result.stdout}')
        content_bytes = content.encode('utf-8', 'surrogatepass')
        bytes_written = len(content_bytes)
        content_verified: Optional[bool] = None
        try:
            hash_cmd = f'sha256sum {self._escape_shell_arg(path)} 2>/dev/null'
            hash_result = self._exec(hash_cmd)
            if hash_result.exit_code == 0 and hash_result.stdout.strip():
                disk_sha = hash_result.stdout.strip().split()[0]
                expected_sha = hashlib.sha256(content_bytes).hexdigest()
                content_verified = disk_sha == expected_sha
                if not content_verified:
                    return WriteResult(error=f'Post-write verification failed for {path}: on-disk content hash differs from the intended write. The write did not persist correctly — re-read the file and retry.')
        except Exception:
            content_verified = None
        lint_result = self._check_lint_delta(path, pre_content=pre_content, post_content=content)
        lsp_diagnostics: Optional[str] = None
        if lint_result.success or lint_result.skipped:
            block = self._maybe_lsp_diagnostics(path, pre_content=pre_content, post_content=content)
            if block:
                lsp_diagnostics = block
        return WriteResult(bytes_written=bytes_written, dirs_created=dirs_created, verified=content_verified, lint=lint_result.to_dict() if lint_result else None, lsp_diagnostics=lsp_diagnostics)

    def patch_replace(self, path: str, old_string: str, new_string: str, replace_all: bool=False) -> PatchResult:
        """
        Replace text in a file using fuzzy matching.

        Args:
            path: File path to modify
            old_string: Text to find (must be unique unless replace_all=True)
            new_string: Replacement text
            replace_all: If True, replace all occurrences

        Returns:
            PatchResult with diff and lint results
        """
        path = self._expand_path(path)
        denied = get_write_denied_error(path)
        if denied:
            return PatchResult(error=denied)
        read_cmd = f'cat {self._escape_shell_arg(path)} 2>/dev/null'
        read_result = self._exec(read_cmd)
        if read_result.exit_code != 0:
            return PatchResult(error=f'Failed to read file: {path}')
        content = read_result.stdout
        raw_content = content
        content, _ = _strip_bom(content)
        from tools.fuzzy_match import fuzzy_find_and_replace
        new_content, match_count, _strategy, error = fuzzy_find_and_replace(content, old_string, new_string, replace_all)
        if error or match_count == 0:
            from tools.fuzzy_match import is_already_applied
            if is_already_applied(content, old_string, new_string):
                return PatchResult(success=True, no_change=True, note=f'File already contains the target text — the edit appears to be already applied to {path}. No write performed; do not re-send this patch.')
            err_msg = error or f'Could not find match for old_string in {path}'
            try:
                from tools.fuzzy_match import format_no_match_hint
                err_msg += format_no_match_hint(err_msg, match_count, old_string, content)
            except Exception:
                pass
            return PatchResult(error=err_msg)
        file_ending = _detect_line_ending(content)
        if file_ending:
            new_content = _normalize_line_endings(new_content, file_ending)
        write_result = self.write_file(path, new_content, pre_content=raw_content)
        if write_result.error:
            return PatchResult(error=f'Failed to write changes: {write_result.error}')
        verify_cmd = f'cat {self._escape_shell_arg(path)} 2>/dev/null'
        verify_result = self._exec(verify_cmd)
        if verify_result.exit_code != 0:
            return PatchResult(error=f'Post-write verification failed: could not re-read {path}')
        _verify_bomless, _ = _strip_bom(verify_result.stdout)
        _verify_stdout_normalized = _verify_bomless.replace('\r\n', '\n').replace('\r', '\n')
        _new_content_normalized = new_content.replace('\r\n', '\n').replace('\r', '\n')
        if _verify_stdout_normalized != _new_content_normalized:
            return PatchResult(error=f'Post-write verification failed for {path}: on-disk content differs from intended write (wrote {len(_new_content_normalized)} chars, read back {len(_verify_stdout_normalized)} chars after normalizing line endings). The patch did not persist. Re-read the file and try again.')
        diff = self._unified_diff(content, new_content, path)
        lint_result = self._check_lint_delta(path, pre_content=content, post_content=new_content)
        return PatchResult(success=True, diff=diff, files_modified=[path], lint=lint_result.to_dict() if lint_result else None, lsp_diagnostics=write_result.lsp_diagnostics)

    def patch_v4a(self, patch_content: str) -> PatchResult:
        """
        Apply a V4A format patch.
        
        V4A format:
            *** Begin Patch
            *** Update File: path/to/file.py
            @@ context hint @@
             context line
            -removed line
            +added line
            *** End Patch
        
        Args:
            patch_content: V4A format patch string
        
        Returns:
            PatchResult with changes made
        """
        from tools.patch_parser import parse_v4a_patch, apply_v4a_operations
        operations, parse_error = parse_v4a_patch(patch_content)
        if parse_error:
            return PatchResult(error=f'Failed to parse patch: {parse_error}')
        result = apply_v4a_operations(operations, self)
        return result

    def _check_lint(self, path: str, content: Optional[str]=None) -> LintResult:
        """
        Run syntax check on a file after editing.

        Prefers the in-process linter for structured formats (JSON, YAML,
        TOML) when possible — those parse via the Python stdlib in
        microseconds and don't require a subprocess.  Falls back to the
        shell linter table for compiled/type-checked languages
        (py_compile, node --check, tsc, go vet, rustfmt).

        Args:
            path: File path (used to select the linter + for shell invocation).
            content: Optional file content.  If provided AND an in-process
                     linter matches the extension, we lint the content
                     directly without re-reading the file from disk.  Ignored
                     for shell linters.

        Returns:
            LintResult with status and any errors.
        """
        ext = os.path.splitext(path)[1].lower()
        inproc = LINTERS_INPROC.get(ext)
        if inproc is not None:
            if content is None:
                read_cmd = f'cat {self._escape_shell_arg(path)} 2>/dev/null'
                read_result = self._exec(read_cmd)
                if read_result.exit_code != 0:
                    return LintResult(skipped=True, message=f'Failed to read {path} for lint')
                content = read_result.stdout
            ok, err = inproc(content)
            if err == '__SKIP__':
                return LintResult(skipped=True, message=f'No linter available for {ext} (missing dependency)')
            return LintResult(success=ok, output='' if ok else err)
        if ext not in LINTERS:
            return LintResult(skipped=True, message=f'No linter for {ext} files')
        if ext in _SHELL_LINTER_LSP_REDUNDANT and self._lsp_will_handle(path):
            return LintResult(skipped=True, message=f'LSP server handles {ext} — shell linter skipped')
        linter_cmd = LINTERS[ext]
        base_cmd = linter_cmd.split()[0]
        if not self._has_command(base_cmd):
            return LintResult(skipped=True, message=f'{base_cmd} not available')
        cmd = linter_cmd.replace('{file}', self._escape_shell_arg(path))
        result = self._exec(cmd, timeout=30)
        if result.exit_code != 0 and _looks_like_linter_unusable(base_cmd, result.stdout):
            from tools.ansi_strip import strip_ansi
            cleaned = strip_ansi(result.stdout).strip()
            first_line = next((ln.strip() for ln in cleaned.splitlines() if ln.strip()), cleaned[:120])
            return LintResult(skipped=True, message=f'{base_cmd} not usable: {first_line[:200]}')
        return LintResult(success=result.exit_code == 0, output=result.stdout.strip() if result.stdout.strip() else '')

    def _check_lint_delta(self, path: str, pre_content: Optional[str], post_content: Optional[str]=None) -> LintResult:
        """
        Run post-write syntax lint with pre-write baseline comparison.

        Two-tier strategy:

        1. **Syntax check** (in-process or shell-based, microseconds).
           Catches the bug class that motivated this layer: corrupt
           writes, mashed quotes, truncated output.  Hot path.

        2. **Delta refinement against pre-write content** when the
           syntax tier reports errors.  Filter out errors that already
           existed pre-edit so the agent isn't distracted by inherited
           state.

        Semantic diagnostics from the LSP layer are fetched separately
        via :meth:`_maybe_lsp_diagnostics` and surfaced in the
        ``lsp_diagnostics`` field on :class:`WriteResult` /
        :class:`PatchResult`.  Keeping the two channels separate lets
        the agent (and any downstream parsers) read syntax errors and
        semantic errors as independent signals.

        Args:
            path: File path (for linter selection).
            pre_content: File content BEFORE the write.  Pass None for new
                         files or when the pre-state isn't available — the
                         delta refinement is skipped and all post errors
                         are returned.
            post_content: File content AFTER the write.  Optional; if None,
                          the shell linter reads from disk (same as
                          _check_lint).

        Returns:
            LintResult.  ``output`` contains either the full post-lint
            errors (no pre-state) or just the new-error lines (delta
            refinement applied).
        """
        post = self._check_lint(path, content=post_content)
        if post.success or post.skipped:
            return post
        if pre_content is None:
            return post
        pre = self._check_lint(path, content=pre_content)
        if pre.success or pre.skipped or (not pre.output):
            return post
        pre_lines = {ln.strip() for ln in pre.output.splitlines() if ln.strip()}
        post_lines = [ln for ln in post.output.splitlines() if ln.strip() and ln.strip() not in pre_lines]
        if not post_lines:
            return LintResult(success=False, output=post.output, message="Pre-existing lint errors — this edit didn't introduce new ones but the file is still broken.")
        return LintResult(success=False, output='New lint errors introduced by this edit (pre-existing errors filtered out):\n' + '\n'.join(post_lines))

    def _lsp_local_only(self) -> bool:
        """Return True iff this FileOperations is wired to a local backend.

        LSP servers run on the host process — they need access to the
        files they're linting.  Remote/sandboxed backends (Docker,
        Modal, SSH, Daytona) keep files inside the sandbox where the
        host-side LSP server can't reach them, so we skip the LSP
        path for those entirely.
        """
        env = getattr(self, 'env', None)
        if env is None:
            return False
        try:
            from tools.environments.local import LocalEnvironment
        except Exception:
            return False
        return isinstance(env, LocalEnvironment)

    def _lsp_handles_extension(self, ext: str) -> bool:
        """Return True iff some registered LSP server claims this extension.

        Used to decide whether to capture pre-write content for the
        line-shift map.  Capturing is cheap (one ``cat`` on the host)
        but pointless if no LSP would ever look at the file.

        Safe to call on remote backends — the registry is purely
        in-process metadata; we still gate the actual LSP path on
        :meth:`_lsp_local_only`.
        """
        if not ext:
            return False
        try:
            from agent.lsp.servers import SERVERS
        except Exception:
            return False
        ext_lower = ext.lower()
        for srv in SERVERS:
            if ext_lower in srv.extensions:
                return True
        return False

    def _lsp_will_handle(self, path: str) -> bool:
        """Return True iff the LSP service is active AND will lint this file.

        Stronger than :meth:`_lsp_handles_extension` — that one only checks
        the static server registry.  This one additionally requires the
        LSP service to be configured/enabled and the file to pass
        :meth:`agent.lsp.manager.LSPService.enabled_for` (which gates on
        workspace detection, disabled-server set, and the broken-pair
        short-circuit).

        Used by :meth:`_check_lint` to decide whether to skip the per-file
        shell linter for extensions in ``_SHELL_LINTER_LSP_REDUNDANT``.

        Best-effort: any failure path returns False so the shell linter
        runs as before — never suppress lint based on an LSP probe that
        couldn't actually answer the question.
        """
        if not self._lsp_local_only():
            return False
        try:
            from agent.lsp import get_service
        except Exception:
            return False
        try:
            svc = get_service()
        except Exception:
            return False
        if svc is None:
            return False
        try:
            return bool(svc.enabled_for(path))
        except Exception:
            return False

    def _snapshot_lsp_baseline(self, path: str) -> None:
        """Capture pre-edit LSP diagnostics so the post-write delta is correct.

        Best-effort.  Silent on every failure path — LSP is an
        enrichment layer and must never break a write.

        Skipped entirely on non-local backends (Docker, Modal, SSH,
        etc.) — the server can't see files inside the sandbox.
        """
        if not self._lsp_local_only():
            return
        try:
            from agent.lsp import get_service
            svc = get_service()
        except Exception:
            return
        if svc is None:
            return
        try:
            svc.snapshot_baseline(path)
        except Exception:
            pass

    def _maybe_lsp_diagnostics(self, path: str, *, pre_content: Optional[str]=None, post_content: Optional[str]=None) -> str:
        """Best-effort LSP semantic diagnostics for ``path``.

        Returns a formatted ``<diagnostics>`` block, or empty string
        when LSP is unavailable / disabled / produced no errors.

        When both ``pre_content`` and ``post_content`` are provided,
        a line-shift map is built and passed to the LSPService so
        baseline diagnostics are remapped into post-edit coordinates
        before the set-difference.  Without this, edits that delete
        or insert lines surface every pre-existing diagnostic below
        the edit point as "introduced by this edit".

        Wraps everything in a try/except so a misbehaving LSP server
        can't break a write.  This intentionally swallows all errors
        — the calling tier already returned a clean syntax result, so
        ``""`` here just means "no extra info to add".

        Skipped entirely on non-local backends (Docker, Modal, SSH,
        etc.) — same reasoning as ``_snapshot_lsp_baseline``.
        """
        if not self._lsp_local_only():
            return ''
        try:
            from agent.lsp import get_service
        except Exception:
            return ''
        try:
            svc = get_service()
        except Exception:
            return ''
        if svc is None or not svc.enabled_for(path):
            return ''
        line_shift = None
        if pre_content is not None and post_content is not None and (pre_content != post_content):
            try:
                from agent.lsp.range_shift import build_line_shift
                line_shift = build_line_shift(pre_content, post_content)
            except Exception:
                line_shift = None
        try:
            diagnostics = svc.get_diagnostics_sync(path, delta=True, line_shift=line_shift)
        except Exception:
            return ''
        if not diagnostics:
            return ''
        try:
            from agent.lsp.reporter import report_for_file, truncate
            block = report_for_file(path, diagnostics)
            if not block:
                return ''
            return truncate('LSP diagnostics introduced by this edit:\n' + block)
        except Exception:
            return ''

    def search(self, pattern: str, path: str='.', target: str='content', file_glob: Optional[str]=None, limit: int=50, offset: int=0, output_mode: str='content', context: int=0) -> SearchResult:
        """
        Search for content or files.
        
        Args:
            pattern: Regex (for content) or glob pattern (for files)
            path: Directory/file to search (default: cwd)
            target: "content" (grep) or "files" (glob)
            file_glob: File pattern filter for content search (e.g., "*.py")
            limit: Max results (default 50)
            offset: Skip first N results
            output_mode: "content", "files_only", or "count"
            context: Lines of context around matches
        
        Returns:
            SearchResult with matches or file list
        """
        offset, limit = normalize_search_pagination(offset, limit)
        path = self._expand_path(path)
        check = self._exec(f'test -e {self._escape_shell_arg(path)} && echo exists || echo not_found')
        if 'not_found' in check.stdout:
            multi = self._try_multi_path_search(pattern, path, target, file_glob, limit, offset, output_mode, context)
            if multi is not None:
                return multi
            parent = os.path.dirname(path) or '.'
            basename_query = os.path.basename(path)
            hint_parts = [f'Path not found: {path}']
            parent_check = self._exec(f'test -d {self._escape_shell_arg(parent)} && echo yes || echo no')
            if 'yes' in parent_check.stdout and basename_query:
                ls_result = self._exec(f'ls -1 {self._escape_shell_arg(parent)} 2>/dev/null | head -20')
                if ls_result.exit_code == 0 and ls_result.stdout.strip():
                    lower_q = basename_query.lower()
                    candidates = []
                    for entry in ls_result.stdout.strip().split('\n'):
                        if not entry:
                            continue
                        le = entry.lower()
                        if lower_q in le or le in lower_q or le.startswith(lower_q[:3]):
                            candidates.append(os.path.join(parent, entry))
                    if candidates:
                        hint_parts.append('Similar paths: ' + ', '.join(candidates[:5]))
            return SearchResult(error='. '.join(hint_parts), total_count=0)
        if target == 'files':
            return self._search_files(pattern, path, limit, offset)
        else:
            return self._search_content(pattern, path, file_glob, limit, offset, output_mode, context)

    def _try_multi_path_search(self, pattern: str, path: str, target: str, file_glob: Optional[str], limit: int, offset: int, output_mode: str, context: int) -> Optional[SearchResult]:
        """Recover a not-found ``path`` that is really several paths in one string.

        Production trajectories show models passing "dir1 dir2 dir3" (or
        comma-separated lists) as ``path``. Split on whitespace/commas; when
        at least one candidate exists and at least two candidates were given,
        search every existing path, merge results, and note skipped parts.
        Returns None when this doesn't look like a multi-path string.
        """
        parts = [p for chunk in path.split(',') for p in chunk.split() if p.strip()]
        if len(parts) < 2:
            return None
        existing, missing = ([], [])
        for p in parts:
            expanded = self._expand_path(p)
            chk = self._exec(f'test -e {self._escape_shell_arg(expanded)} && echo exists || echo not_found')
            (existing if 'exists' in chk.stdout else missing).append(expanded)
        if not existing:
            return None
        merged = SearchResult()
        for p in existing:
            if target == 'files':
                sub = self._search_files(pattern, p, limit, offset)
            else:
                sub = self._search_content(pattern, p, file_glob, limit, offset, output_mode, context)
            if sub.error:
                continue
            merged.matches.extend(sub.matches)
            merged.files.extend(sub.files)
            merged.counts.update(sub.counts)
            merged.total_count += sub.total_count
            merged.truncated = merged.truncated or sub.truncated
        merged.matches = merged.matches[:limit]
        merged.files = merged.files[:limit]
        note = f'path contained {len(parts)} entries; searched {len(existing)} that exist'
        if missing:
            note += '; skipped missing: ' + ', '.join(missing[:3])
            if len(missing) > 3:
                note += f' (+{len(missing) - 3} more)'
        merged.warning = note
        return merged

    def _zero_match_probe(self, pattern: str, path: str, file_glob: Optional[str]) -> Optional[str]:
        """Return a hint for a 0-match content search, or None.

        13.9% of production content searches return zero matches and give
        the model nothing to steer by. Run ONE cheap case-insensitive count
        probe; if it hits, say so. If the pattern contains regex
        metacharacters, also probe it as a fixed string. Bounded: two rg
        invocations max, count-only output.
        """
        if not self._has_command('rg'):
            return None
        glob_expr = f' --glob {self._escape_shell_arg(file_glob)}' if file_glob else ''
        probe = self._exec(f'rg -i --count-matches{glob_expr} {self._escape_shell_arg(pattern)} {self._escape_shell_arg(path)} 2>/dev/null | head -50', timeout=30)
        ci_total = 0
        ci_files = 0
        for line in (probe.stdout or '').strip().splitlines():
            _p, _sep, n = line.rpartition(':')
            if n.isdigit():
                ci_total += int(n)
                ci_files += 1
        if ci_total > 0:
            return f"0 exact matches, but {ci_total} case-insensitive match(es) in {ci_files} file(s) — the pattern's casing may be wrong."
        hidden = self._exec(f'rg --hidden --no-ignore --count-matches{glob_expr} {self._escape_shell_arg(pattern)} {self._escape_shell_arg(path)} 2>/dev/null | head -50', timeout=30)
        h_total = 0
        h_files = 0
        for line in (hidden.stdout or '').strip().splitlines():
            _p, _sep, n = line.rpartition(':')
            if n.isdigit():
                h_total += int(n)
                h_files += 1
        if h_total > 0:
            return f'0 matches in visible files, but {h_total} match(es) in {h_files} hidden or gitignored file(s) — these are excluded by default. Search the hidden path explicitly to include them.'
        if re.search('[.\\[\\](){}?*+^$\\\\|]', pattern):
            fixed = self._exec(f'rg -F --count-matches{glob_expr} {self._escape_shell_arg(pattern)} {self._escape_shell_arg(path)} 2>/dev/null | head -50', timeout=30)
            f_total = sum((int(line.rpartition(':')[2]) for line in (fixed.stdout or '').strip().splitlines() if line.rpartition(':')[2].isdigit()))
            if f_total > 0:
                return f'0 regex matches, but {f_total} literal match(es) — the pattern contains regex metacharacters that likely need escaping (or pass a simpler substring).'
        return None

    def _search_files(self, pattern: str, path: str, limit: int, offset: int) -> SearchResult:
        """Search for files by name pattern (glob-like)."""
        if not pattern.startswith('**/') and '/' not in pattern:
            search_pattern = pattern
        else:
            search_pattern = pattern.split('/')[-1]
        search_root = Path(path)
        has_hidden_path_ancestor = any((part not in {'.', '..'} and part.startswith('.') for part in search_root.parts))
        if self._has_command('rg'):
            return self._search_files_rg(search_pattern, path, limit, offset)
        if not self._has_command('find'):
            return SearchResult(error="File search requires 'rg' (ripgrep) or 'find'. Install ripgrep for best results: https://github.com/BurntSushi/ripgrep#installation")
        hidden_exclude = "-not -path '*/.*'" if not has_hidden_path_ancestor else ''
        hidden_filter_expr = f' {hidden_exclude}' if hidden_exclude else ''
        pagination_expr = ''
        if not has_hidden_path_ancestor:
            pagination_expr = f' | tail -n +{offset + 1} | head -n {limit}'
        cmd = f"find {self._escape_shell_arg(path)}{hidden_filter_expr} -type f -name {self._escape_shell_arg(search_pattern)} -printf '%T@ %p\\n' 2>/dev/null | sort -rn{pagination_expr}"
        result = self._exec(cmd, timeout=60)
        stdout, limit_reason = _search_stdout_and_limit(result)
        if not stdout.strip() and (not limit_reason):
            cmd_simple = f'find {self._escape_shell_arg(path)}{hidden_filter_expr} -type f -name {self._escape_shell_arg(search_pattern)} 2>/dev/null | sort -rn{pagination_expr}'
            result = self._exec(cmd_simple, timeout=60)
            stdout, limit_reason = _search_stdout_and_limit(result)
        files = []
        for line in stdout.strip().split('\n'):
            if not line:
                continue
            parts = line.split(' ', 1)
            if len(parts) == 2 and parts[0].replace('.', '').isdigit():
                files.append(parts[1])
            else:
                files.append(line)
        if has_hidden_path_ancestor:
            normalized_root = search_root.resolve()
            filtered_files = []
            for file_path in files:
                try:
                    rel_parts = Path(file_path).resolve().relative_to(normalized_root).parts
                except ValueError:
                    rel_parts = Path(file_path).parts
                if any((part not in {'.', '..'} and part.startswith('.') for part in rel_parts)):
                    continue
                filtered_files.append(file_path)
            files = filtered_files[offset:offset + limit]
        return SearchResult(files=files, total_count=len(files), truncated=bool(limit_reason), limit_reason=limit_reason)

    def _search_files_rg(self, pattern: str, path: str, limit: int, offset: int) -> SearchResult:
        """Search for files by name using ripgrep's --files mode.

        rg --files respects .gitignore and excludes hidden directories by
        default, and uses parallel directory traversal for ~200x speedup
        over find on wide trees.  Results are sorted by modification time
        (most recently edited first) when rg >= 13.0 supports --sortr.
        """
        if '/' not in pattern and (not pattern.startswith('*')):
            glob_pattern = f'*{pattern}'
        else:
            glob_pattern = pattern
        fetch_limit = limit + offset
        cmd_sorted = f'rg --files --sortr=modified -g {self._escape_shell_arg(glob_pattern)} {self._escape_shell_arg(path)} 2>/dev/null | head -n {fetch_limit}'
        result = self._exec(cmd_sorted, timeout=60)
        stdout, limit_reason = _search_stdout_and_limit(result)
        all_files = [f for f in stdout.strip().split('\n') if f]
        if not all_files and (not limit_reason):
            cmd_plain = f'rg --files -g {self._escape_shell_arg(glob_pattern)} {self._escape_shell_arg(path)} 2>/dev/null | head -n {fetch_limit}'
            result = self._exec(cmd_plain, timeout=60)
            stdout, limit_reason = _search_stdout_and_limit(result)
            all_files = [f for f in stdout.strip().split('\n') if f]
        page = all_files[offset:offset + limit]
        return SearchResult(files=page, total_count=len(all_files), truncated=len(all_files) >= fetch_limit or bool(limit_reason), limit_reason=limit_reason)

    def _search_content(self, pattern: str, path: str, file_glob: Optional[str], limit: int, offset: int, output_mode: str, context: int) -> SearchResult:
        """Search for content inside files (grep-like)."""
        used_rg = False
        if self._has_command('rg'):
            used_rg = True
            result = self._search_with_rg(pattern, path, file_glob, limit, offset, output_mode, context)
        elif self._has_command('grep'):
            result = self._search_with_grep(pattern, path, file_glob, limit, offset, output_mode, context)
        else:
            return SearchResult(error='Content search requires ripgrep (rg) or grep. Install ripgrep: https://github.com/BurntSushi/ripgrep#installation')
        if not result.error and result.total_count == 0 and (not result.matches) and (not result.files) and (not result.counts):
            try:
                hint = self._zero_match_probe(pattern, path, file_glob)
            except Exception:
                hint = None
            if hint:
                result.warning = hint if not result.warning else f'{result.warning} {hint}'
        if used_rg:
            return result
        return _maybe_warn_line_oriented_newline_pattern(result, pattern)

    def _search_with_rg(self, pattern: str, path: str, file_glob: Optional[str], limit: int, offset: int, output_mode: str, context: int) -> SearchResult:
        """Search using ripgrep."""
        cmd_parts = ['rg', '--line-number', '--no-heading', '--with-filename']
        multiline = _pattern_has_regex_newline(pattern)
        if multiline:
            cmd_parts.append('--multiline')
        if context > 0:
            cmd_parts.extend(['-C', str(context)])
        if file_glob:
            cmd_parts.extend(['--glob', self._escape_shell_arg(file_glob)])
        if output_mode == 'files_only':
            cmd_parts.append('-l')
        elif output_mode == 'count':
            cmd_parts.append('-c')
        cmd_parts.append(self._escape_shell_arg(pattern))
        cmd_parts.append(self._escape_shell_arg(path))
        fetch_limit = limit + offset + 200 if context > 0 else limit + offset
        cmd_parts.extend(['|', 'head', '-n', str(fetch_limit)])
        cmd = 'set -o pipefail; ' + ' '.join(cmd_parts)
        result = self._exec(cmd, timeout=60)
        stdout, limit_reason = _search_stdout_and_limit(result)
        diagnostics, payload = _split_tool_diagnostics(stdout)
        if result.exit_code == 2 and (not payload.strip()):
            error_msg = diagnostics.strip() or result.stdout.strip() or 'Search error'
            return SearchResult(error=f'Search failed: {error_msg}', total_count=0)
        stdout = payload
        _ml_note = 'Pattern contains \\n — multiline mode (-U) was enabled automatically so the regex can match across line boundaries.' if multiline else None
        if output_mode == 'files_only':
            all_files = [f for f in stdout.strip().split('\n') if f]
            total = len(all_files)
            page = all_files[offset:offset + limit]
            return SearchResult(files=page, total_count=total, truncated=bool(limit_reason), limit_reason=limit_reason, warning=_ml_note)
        elif output_mode == 'count':
            counts = {}
            for line in stdout.strip().split('\n'):
                if ':' in line:
                    parts = line.rsplit(':', 1)
                    if len(parts) == 2:
                        try:
                            counts[parts[0]] = int(parts[1])
                        except ValueError:
                            pass
            return SearchResult(counts=counts, total_count=sum(counts.values()), truncated=bool(limit_reason), limit_reason=limit_reason)
        else:
            _match_re = re.compile('^([A-Za-z]:)?(.*?):(\\d+):(.*)$')
            matches = []
            for line in stdout.strip().split('\n'):
                if not line or line == '--':
                    continue
                m = _match_re.match(line)
                if m:
                    matches.append(SearchMatch(path=(m.group(1) or '') + m.group(2), line_number=int(m.group(3)), content=m.group(4)[:500]))
                    continue
                if context > 0:
                    parsed = _parse_search_context_line(line)
                    if parsed:
                        matches.append(SearchMatch(path=parsed[0], line_number=parsed[1], content=parsed[2][:500]))
            total = len(matches)
            page = matches[offset:offset + limit]
            return SearchResult(matches=page, total_count=total, truncated=total > offset + limit or bool(limit_reason), limit_reason=limit_reason, warning=_ml_note)

    def _search_with_grep(self, pattern: str, path: str, file_glob: Optional[str], limit: int, offset: int, output_mode: str, context: int) -> SearchResult:
        """Fallback search using grep."""
        cmd_parts = ['grep', '-rnHE']
        cmd_parts.append("--exclude-dir='.*'")
        if context > 0:
            cmd_parts.extend(['-C', str(context)])
        if file_glob:
            cmd_parts.extend(['--include', self._escape_shell_arg(file_glob)])
        if output_mode == 'files_only':
            cmd_parts.append('-l')
        elif output_mode == 'count':
            cmd_parts.append('-c')
        cmd_parts.append(self._escape_shell_arg(pattern))
        cmd_parts.append(self._escape_shell_arg(path))
        fetch_limit = limit + offset + (200 if context > 0 else 0)
        cmd_parts.extend(['|', 'head', '-n', str(fetch_limit)])
        cmd = 'set -o pipefail; ' + ' '.join(cmd_parts)
        result = self._exec(cmd, timeout=60)
        stdout, limit_reason = _search_stdout_and_limit(result)
        diagnostics, payload = _split_tool_diagnostics(stdout)
        if result.exit_code == 2 and (not payload.strip()):
            error_msg = diagnostics.strip() or result.stdout.strip() or 'Search error'
            return SearchResult(error=f'Search failed: {error_msg}', total_count=0)
        stdout = payload
        if output_mode == 'files_only':
            all_files = [f for f in stdout.strip().split('\n') if f]
            total = len(all_files)
            page = all_files[offset:offset + limit]
            return SearchResult(files=page, total_count=total, truncated=bool(limit_reason), limit_reason=limit_reason)
        elif output_mode == 'count':
            counts = {}
            for line in stdout.strip().split('\n'):
                if ':' in line:
                    parts = line.rsplit(':', 1)
                    if len(parts) == 2:
                        try:
                            counts[parts[0]] = int(parts[1])
                        except ValueError:
                            pass
            return SearchResult(counts=counts, total_count=sum(counts.values()), truncated=bool(limit_reason), limit_reason=limit_reason)
        else:
            _match_re = re.compile('^([A-Za-z]:)?(.*?):(\\d+):(.*)$')
            matches = []
            for line in stdout.strip().split('\n'):
                if not line or line == '--':
                    continue
                m = _match_re.match(line)
                if m:
                    matches.append(SearchMatch(path=(m.group(1) or '') + m.group(2), line_number=int(m.group(3)), content=m.group(4)[:500]))
                    continue
                if context > 0:
                    parsed = _parse_search_context_line(line)
                    if parsed:
                        matches.append(SearchMatch(path=parsed[0], line_number=parsed[1], content=parsed[2][:500]))
            total = len(matches)
            page = matches[offset:offset + limit]
            return SearchResult(matches=page, total_count=total, truncated=total > offset + limit or bool(limit_reason), limit_reason=limit_reason)