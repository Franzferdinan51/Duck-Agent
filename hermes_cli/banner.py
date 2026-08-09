"""Welcome banner, ASCII art, skills summary, and update check for the CLI.

Pure display functions with no HermesCLI state dependency.
"""
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
from hermes_constants import get_hermes_home
from typing import TYPE_CHECKING, Dict, List, Optional
if TYPE_CHECKING:
    from rich.console import Console
logger = logging.getLogger(__name__)
_GOLD = '\x1b[1;38;2;255;215;0m'
_BOLD = '\x1b[1m'
_DIM = '\x1b[2m'
_RST = '\x1b[0m'

def cprint(text: str):
    """Print ANSI-colored text through prompt_toolkit's renderer."""
    from prompt_toolkit import print_formatted_text as _pt_print
    from prompt_toolkit.formatted_text import ANSI as _PT_ANSI
    try:
        _pt_print(_PT_ANSI(text))
    except Exception:
        print(text)

def _skin_color(key: str, fallback: str) -> str:
    """Get a color from the active skin, or return fallback."""
    try:
        from hermes_cli.skin_engine import get_active_skin
        return get_active_skin().get_color(key, fallback)
    except Exception:
        return fallback
from hermes_cli import __version__ as VERSION, __release_date__ as RELEASE_DATE
HERMES_AGENT_LOGO = '[bold #FFD700]██╗  ██╗███████╗██████╗ ███╗   ███╗███████╗███████╗       █████╗  ██████╗ ███████╗███╗   ██╗████████╗[/]\n[bold #FFD700]██║  ██║██╔════╝██╔══██╗████╗ ████║██╔════╝██╔════╝      ██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝[/]\n[#FFBF00]███████║█████╗  ██████╔╝██╔████╔██║█████╗  ███████╗█████╗███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║[/]\n[#FFBF00]██╔══██║██╔══╝  ██╔══██╗██║╚██╔╝██║██╔══╝  ╚════██║╚════╝██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║[/]\n[#CD7F32]██║  ██║███████╗██║  ██║██║ ╚═╝ ██║███████╗███████║      ██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║[/]\n[#CD7F32]╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═╝     ╚═╝╚══════╝╚══════╝      ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝[/]'
HERMES_CADUCEUS = '[#CD7F32]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣀⡀⠀⣀⣀⠀⢀⣀⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]\n[#CD7F32]⠀⠀⠀⠀⠀⠀⢀⣠⣴⣾⣿⣿⣇⠸⣿⣿⠇⣸⣿⣿⣷⣦⣄⡀⠀⠀⠀⠀⠀⠀[/]\n[#FFBF00]⠀⢀⣠⣴⣶⠿⠋⣩⡿⣿⡿⠻⣿⡇⢠⡄⢸⣿⠟⢿⣿⢿⣍⠙⠿⣶⣦⣄⡀⠀[/]\n[#FFBF00]⠀⠀⠉⠉⠁⠶⠟⠋⠀⠉⠀⢀⣈⣁⡈⢁⣈⣁⡀⠀⠉⠀⠙⠻⠶⠈⠉⠉⠀⠀[/]\n[#FFD700]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣴⣿⡿⠛⢁⡈⠛⢿⣿⣦⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]\n[#FFD700]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠿⣿⣦⣤⣈⠁⢠⣴⣿⠿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]\n[#FFBF00]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠉⠻⢿⣿⣦⡉⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]\n[#FFBF00]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠘⢷⣦⣈⠛⠃⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]\n[#CD7F32]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢠⣴⠦⠈⠙⠿⣦⡄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]\n[#CD7F32]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠸⣿⣤⡈⠁⢤⣿⠇⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]\n[#B8860B]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠉⠛⠷⠄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]\n[#B8860B]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣀⠑⢶⣄⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]\n[#B8860B]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⠁⢰⡆⠈⡿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]\n[#B8860B]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠳⠈⣡⠞⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]\n[#B8860B]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]'

def get_available_skills() -> Dict[str, List[str]]:
    """Return skills grouped by category, filtered by platform and disabled state.

    Delegates to ``_find_all_skills()`` from ``tools/skills_tool`` which already
    handles platform gating (``platforms:`` frontmatter) and respects the
    user's ``skills.disabled`` config list.
    """
    try:
        from tools.skills_tool import _find_all_skills
        all_skills = _find_all_skills()
    except Exception:
        return {}
    skills_by_category: Dict[str, List[str]] = {}
    for skill in all_skills:
        category = skill.get('category') or 'general'
        skills_by_category.setdefault(category, []).append(skill['name'])
    return skills_by_category
_UPDATE_CHECK_CACHE_SECONDS = 6 * 3600
UPDATE_AVAILABLE_NO_COUNT = -1
_UPSTREAM_REPO_URL = 'https://github.com/NousResearch/duck-agent.git'
_OFFICIAL_REPO_CANONICAL = 'github.com/nousresearch/duck-agent'

def _canonical_github_remote(url: str | None) -> str:
    """Return ``host/owner/repo`` for common GitHub remote URL forms."""
    if not url:
        return ''
    value = url.strip()
    if value.startswith('git@github.com:'):
        value = 'github.com/' + value[len('git@github.com:'):]
    elif value.startswith('ssh://git@github.com/'):
        value = 'github.com/' + value[len('ssh://git@github.com/'):]
    else:
        parsed = urlparse(value)
        if parsed.netloc and parsed.path:
            value = f'{parsed.netloc}{parsed.path}'
    value = value.strip().rstrip('/')
    if value.endswith('.git'):
        value = value[:-4]
    return value.lower()

def _is_ssh_remote(url: str | None) -> bool:
    if not url:
        return False
    value = url.strip().lower()
    return value.startswith('git@') or value.startswith('ssh://')

def _is_official_ssh_remote(url: str | None) -> bool:
    return _is_ssh_remote(url) and _canonical_github_remote(url) == _OFFICIAL_REPO_CANONICAL

def _git_stdout(args: list[str], *, cwd: Path, timeout: int=5) -> Optional[str]:
    try:
        result = subprocess.run(['git', *args], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout, cwd=str(cwd))
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or '').strip()

def _check_via_rev(local_rev: str) -> Optional[int]:
    """Compare an embedded git revision to upstream main via ls-remote.

    Returns 0 if up-to-date, ``UPDATE_AVAILABLE_NO_COUNT`` if behind,
    or ``None`` on failure.
    """
    try:
        result = subprocess.run(['git', 'ls-remote', _UPSTREAM_REPO_URL, 'refs/heads/main'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
    except Exception:
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    upstream_rev = result.stdout.split()[0]
    if not upstream_rev:
        return None
    return 0 if upstream_rev == local_rev else UPDATE_AVAILABLE_NO_COUNT

def _check_via_local_git(repo_dir: Path) -> Optional[int]:
    """Count commits behind origin/main in a local checkout."""
    origin_url = _git_stdout(['remote', 'get-url', 'origin'], cwd=repo_dir)
    if _is_official_ssh_remote(origin_url):
        head_rev = _git_stdout(['rev-parse', 'HEAD'], cwd=repo_dir)
        checked = _check_via_rev(head_rev) if head_rev else None
        if checked == UPDATE_AVAILABLE_NO_COUNT:
            return 1
        return checked
    shallow = _git_stdout(['rev-parse', '--is-shallow-repository'], cwd=repo_dir)
    is_shallow = shallow == 'true'
    try:
        fetch_args = ['git', 'fetch', 'origin', 'main']
        if is_shallow:
            fetch_args += ['--depth', '1']
        fetch_args.append('--quiet')
        subprocess.run(fetch_args, capture_output=True, timeout=10, cwd=str(repo_dir))
    except Exception:
        pass
    if is_shallow:
        head_rev = _git_stdout(['rev-parse', 'HEAD'], cwd=repo_dir)
        target_rev = _git_stdout(['rev-parse', 'FETCH_HEAD'], cwd=repo_dir) or _git_stdout(['rev-parse', 'origin/main'], cwd=repo_dir)
        if not head_rev or not target_rev:
            return None
        return 0 if head_rev == target_rev else UPDATE_AVAILABLE_NO_COUNT
    try:
        result = subprocess.run(['git', 'rev-list', '--count', 'HEAD..origin/main'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5, cwd=str(repo_dir))
        if result.returncode == 0:
            return int(result.stdout.strip())
    except Exception:
        pass
    return None

def check_for_updates() -> Optional[int]:
    """Check whether a Duck Agent update is available.

    Two paths: if ``HERMES_REVISION`` is set (nix builds embed it), compare
    it to upstream main via ``git ls-remote``. Otherwise look for a local
    git checkout and count commits behind ``origin/main``.

    Returns the number of commits behind, ``UPDATE_AVAILABLE_NO_COUNT`` (-1)
    if behind but the count is unknown, ``0`` if up-to-date, or ``None`` if
    the check failed or doesn't apply. Cached for 6 hours.
    """
    hermes_home = get_hermes_home()
    cache_file = hermes_home / '.update_check'
    embedded_rev = os.environ.get('HERMES_REVISION') or None
    try:
        from hermes_cli.config import detect_install_method, get_project_root
        if detect_install_method(get_project_root()) == 'docker':
            return None
    except Exception:
        pass
    now = time.time()
    try:
        if cache_file.exists():
            cached = json.loads(cache_file.read_text(encoding='utf-8'))
            if now - cached.get('ts', 0) < _UPDATE_CHECK_CACHE_SECONDS and cached.get('rev') == embedded_rev and (cached.get('ver') == VERSION):
                return cached.get('behind')
    except Exception:
        pass
    if embedded_rev:
        behind = _check_via_rev(embedded_rev)
    else:
        repo_dir = Path(__file__).parent.parent.resolve()
        if not (repo_dir / '.git').exists():
            repo_dir = hermes_home / 'duck-agent'
        if not (repo_dir / '.git').exists():
            behind = None
        else:
            behind = _check_via_local_git(repo_dir)
    try:
        cache_file.write_text(json.dumps({'ts': now, 'behind': behind, 'rev': embedded_rev, 'ver': VERSION}), encoding='utf-8')
    except Exception:
        pass
    return behind

def _resolve_repo_dir() -> Optional[Path]:
    """Return the active Duck Agent git checkout, or None if this isn't a git install.

    Prefers the running code's location over the profile-scoped path
    because ``$DUCK_AGENT_HOME/duck-agent/`` may be a stale copy carried
    over by ``--clone-all``.
    """
    repo_dir = Path(__file__).parent.parent.resolve()
    if not (repo_dir / '.git').exists():
        hermes_home = get_hermes_home()
        repo_dir = hermes_home / 'duck-agent'
    return repo_dir if (repo_dir / '.git').exists() else None

def _git_short_hash(repo_dir: Path, rev: str) -> Optional[str]:
    """Resolve a git revision to an 8-character short hash."""
    try:
        result = subprocess.run(['git', 'rev-parse', '--short=8', rev], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5, cwd=str(repo_dir))
    except Exception:
        return None
    if result.returncode != 0:
        return None
    value = (result.stdout or '').strip()
    return value or None

def get_git_banner_state(repo_dir: Optional[Path]=None) -> Optional[dict]:
    """Return upstream/local git hashes for the startup banner.

    For source installs and dev images this runs ``git rev-parse`` against
    the active checkout.  When no checkout is available — the canonical case
    is the published Docker image, which excludes ``.git`` from the build
    context — we fall back to the baked-in build SHA (see
    ``hermes_cli/build_info.py``) and return it as a frozen
    ``upstream == local`` state with ``ahead=0``.  A built image is by
    definition pinned to one commit, so "ahead" is always zero and the
    banner correctly shows ``· upstream <sha>`` with no carried-commits
    annotation.
    """
    repo_dir = repo_dir or _resolve_repo_dir()
    if repo_dir is None:
        try:
            from hermes_cli.build_info import get_build_sha
            baked = get_build_sha(short=8)
            if baked:
                return {'upstream': baked, 'local': baked, 'ahead': 0}
        except Exception:
            pass
        return None
    upstream = _git_short_hash(repo_dir, 'origin/main')
    local = _git_short_hash(repo_dir, 'HEAD')
    if not upstream or not local:
        try:
            from hermes_cli.build_info import get_build_sha
            baked = get_build_sha(short=8)
            if baked:
                return {'upstream': baked, 'local': baked, 'ahead': 0}
        except Exception:
            pass
        return None
    ahead = 0
    try:
        result = subprocess.run(['git', 'rev-list', '--count', 'origin/main..HEAD'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5, cwd=str(repo_dir))
        if result.returncode == 0:
            ahead = int((result.stdout or '0').strip() or '0')
    except Exception:
        ahead = 0
    return {'upstream': upstream, 'local': local, 'ahead': max(ahead, 0)}
_RELEASE_URL_BASE = 'https://github.com/NousResearch/duck-agent/releases/tag'
_latest_release_cache: Optional[tuple] = None

def get_latest_release_tag(repo_dir: Optional[Path]=None) -> Optional[tuple]:
    """Return ``(tag, release_url)`` for the latest git tag, or None.

    Local-only — runs ``git describe --tags --abbrev=0`` against the
    Duck Agent checkout. Cached per-process. Release URL always points at the
    canonical NousResearch/duck-agent repo (forks don't get a link).
    """
    global _latest_release_cache
    if _latest_release_cache is not None:
        return _latest_release_cache or None
    repo_dir = repo_dir or _resolve_repo_dir()
    if repo_dir is None:
        _latest_release_cache = ()
        return None
    try:
        result = subprocess.run(['git', 'describe', '--tags', '--abbrev=0'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=3, cwd=str(repo_dir))
    except Exception:
        _latest_release_cache = ()
        return None
    if result.returncode != 0:
        _latest_release_cache = ()
        return None
    tag = (result.stdout or '').strip()
    if not tag:
        _latest_release_cache = ()
        return None
    url = f'{_RELEASE_URL_BASE}/{tag}'
    _latest_release_cache = (tag, url)
    return _latest_release_cache

def format_banner_version_label() -> str:
    """Return the version label shown in the startup banner title."""
    base = f'Duck Agent v{VERSION} ({RELEASE_DATE})'
    state = get_git_banner_state()
    if not state:
        return base
    upstream = state['upstream']
    local = state['local']
    ahead = int(state.get('ahead') or 0)
    if ahead <= 0 or upstream == local:
        return f'{base} · upstream {upstream}'
    carried_word = 'commit' if ahead == 1 else 'commits'
    return f'{base} · upstream {upstream} · local {local} (+{ahead} carried {carried_word})'
_update_result: Optional[int] = None
_update_check_done = threading.Event()

def prefetch_update_check():
    """Kick off update check in a background daemon thread."""

    def _run():
        global _update_result
        _update_result = check_for_updates()
        _update_check_done.set()
    t = threading.Thread(target=_run, daemon=True)
    t.start()

def get_update_result(timeout: float=0.5) -> Optional[int]:
    """Get result of prefetched check. Returns None if not ready."""
    _update_check_done.wait(timeout=timeout)
    return _update_result

def _format_context_length(tokens: int) -> str:
    """Format a token count for display (e.g. 128000 → '128K', 1048576 → '1M')."""
    if tokens >= 1000000:
        val = tokens / 1000000
        rounded = round(val)
        if abs(val - rounded) < 0.05:
            return f'{rounded}M'
        return f'{val:.1f}M'
    elif tokens >= 1000:
        val = tokens / 1000
        rounded = round(val)
        if abs(val - rounded) < 0.05:
            return f'{rounded}K'
        return f'{val:.1f}K'
    return str(tokens)

def _display_toolset_name(toolset_name: str) -> str:
    """Normalize internal/legacy toolset identifiers for banner display."""
    if not toolset_name:
        return 'unknown'
    return toolset_name[:-6] if toolset_name.endswith('_tools') else toolset_name

def build_welcome_banner(console: 'Console', model: str, cwd: str, tools: List[dict]=None, enabled_toolsets: List[str]=None, session_id: str=None, get_toolset_for_tool=None, context_length: int=None, provider: str=None):
    """Build and print a welcome banner with caduceus on left and info on right.

    Args:
        console: Rich Console instance.
        model: Current model name.
        cwd: Current working directory.
        tools: List of tool definitions.
        enabled_toolsets: List of enabled toolset names.
        session_id: Session identifier.
        get_toolset_for_tool: Callable to map tool name -> toolset name.
        context_length: Model's context window size in tokens.
        provider: Active provider id. When ``"moa"``, ``model`` is a MoA
            preset name and the banner renders the aggregator instead of a
            bare model slug.
    """
    from model_tools import check_tool_availability, TOOLSET_REQUIREMENTS
    from rich.panel import Panel
    from rich.table import Table
    if get_toolset_for_tool is None:
        from model_tools import get_toolset_for_tool
    tools = tools or []
    enabled_toolsets = enabled_toolsets or []
    _, unavailable_toolsets = check_tool_availability(quiet=True)
    _enabled_ts = {str(t) for t in enabled_toolsets}
    if _enabled_ts:
        unavailable_toolsets = [item for item in unavailable_toolsets if str(item.get('id', item.get('name', ''))) in _enabled_ts]
    disabled_tools = set()
    lazy_tools = set()
    for item in unavailable_toolsets:
        toolset_name = item.get('name', '')
        ts_req = TOOLSET_REQUIREMENTS.get(toolset_name, {})
        tools_in_ts = item.get('tools', [])
        if ts_req.get('check_fn'):
            lazy_tools.update(tools_in_ts)
        else:
            disabled_tools.update(tools_in_ts)
    layout_table = Table.grid(padding=(0, 2))
    layout_table.add_column('left', justify='center')
    layout_table.add_column('right', justify='left')
    accent = _skin_color('banner_accent', '#FFBF00')
    dim = _skin_color('banner_dim', '#B8860B')
    text = _skin_color('banner_text', '#FFF8DC')
    session_color = _skin_color('session_border', '#8B8682')
    try:
        from hermes_cli.skin_engine import get_active_skin
        _bskin = get_active_skin()
        _hero = _bskin.banner_hero if hasattr(_bskin, 'banner_hero') and _bskin.banner_hero else HERMES_CADUCEUS
    except Exception:
        _bskin = None
        _hero = HERMES_CADUCEUS
    left_lines = ['', _hero, '']
    if (provider or '').strip().lower() == 'moa':
        preset_name = model
        agg_label = ''
        try:
            from hermes_cli.config import load_config
            from hermes_cli.moa_config import normalize_moa_config
            _moa = normalize_moa_config(load_config().get('moa') or {})
            _preset = _moa.get('presets', {}).get(preset_name)
            if _preset:
                _agg = _preset.get('aggregator') or {}
                _am = str(_agg.get('model') or '')
                agg_label = _am.split('/')[-1] if '/' in _am else _am
        except Exception:
            agg_label = ''
        if len(preset_name) > 28:
            preset_name = preset_name[:25] + '...'
        agg_str = f' [dim {dim}]·[/] [dim {dim}]agg {agg_label}[/]' if agg_label else ''
        ctx_str = f' [dim {dim}]·[/] [dim {dim}]{_format_context_length(context_length)} context[/]' if context_length else ''
        left_lines.append(f'[{accent}]MoA: {preset_name}[/]{agg_str}{ctx_str} [dim {dim}]·[/] [dim {dim}]Nous Research[/]')
    elif not (model or '').strip() or (model or '').strip().lower() == 'unknown':
        left_lines.append(f'[bold red]no model configured[/] [dim {dim}]— run /model or duck-agent setup[/]')
    else:
        model_short = model.split('/')[-1] if '/' in model else model
        if model_short.endswith('.gguf'):
            model_short = model_short[:-5]
        if len(model_short) > 28:
            model_short = model_short[:25] + '...'
        ctx_str = f' [dim {dim}]·[/] [dim {dim}]{_format_context_length(context_length)} context[/]' if context_length else ''
        left_lines.append(f'[{accent}]{model_short}[/]{ctx_str} [dim {dim}]·[/] [dim {dim}]Nous Research[/]')
    if os.getenv('HERMES_YOLO_MODE'):
        left_lines.append(f'[bold red]⚠ YOLO mode[/] [dim {dim}]— all approval prompts bypassed[/]')
    left_lines.append(f'[dim {dim}]{cwd}[/]')
    if session_id:
        left_lines.append(f'[dim {session_color}]Session: {session_id}[/]')
    left_content = '\n'.join(left_lines)
    right_lines = [f'[bold {accent}]Available Tools[/]']
    toolsets_dict: Dict[str, list] = {}
    for tool in tools:
        tool_name = tool['function']['name']
        toolset = _display_toolset_name(get_toolset_for_tool(tool_name) or 'other')
        toolsets_dict.setdefault(toolset, []).append(tool_name)
    for item in unavailable_toolsets:
        toolset_id = item.get('id', item.get('name', 'unknown'))
        display_name = _display_toolset_name(toolset_id)
        if display_name not in toolsets_dict:
            toolsets_dict[display_name] = []
        for tool_name in item.get('tools', []):
            if tool_name not in toolsets_dict[display_name]:
                toolsets_dict[display_name].append(tool_name)
    sorted_toolsets = sorted(toolsets_dict.keys())
    display_toolsets = sorted_toolsets[:8]
    remaining_toolsets = len(sorted_toolsets) - 8
    for toolset in display_toolsets:
        tool_names = toolsets_dict[toolset]
        colored_names = []
        for name in sorted(tool_names):
            if name in disabled_tools:
                colored_names.append(f'[red]{name}[/]')
            elif name in lazy_tools:
                colored_names.append(f'[yellow]{name}[/]')
            else:
                colored_names.append(f'[{text}]{name}[/]')
        tools_str = ', '.join(colored_names)
        if len(', '.join(sorted(tool_names))) > 45:
            short_names = []
            length = 0
            for name in sorted(tool_names):
                if length + len(name) + 2 > 42:
                    short_names.append('...')
                    break
                short_names.append(name)
                length += len(name) + 2
            colored_names = []
            for name in short_names:
                if name == '...':
                    colored_names.append('[dim]...[/]')
                elif name in disabled_tools:
                    colored_names.append(f'[red]{name}[/]')
                elif name in lazy_tools:
                    colored_names.append(f'[yellow]{name}[/]')
                else:
                    colored_names.append(f'[{text}]{name}[/]')
            tools_str = ', '.join(colored_names)
        right_lines.append(f'[dim {dim}]{toolset}:[/] {tools_str}')
    if remaining_toolsets > 0:
        right_lines.append(f'[dim {dim}](and {remaining_toolsets} more toolsets...)[/]')
    try:
        from tools.mcp_tool import get_mcp_status
        mcp_status = get_mcp_status()
    except Exception:
        mcp_status = []
    if mcp_status:
        right_lines.append('')
        right_lines.append(f'[bold {accent}]MCP Servers[/]')
        for srv in mcp_status:
            status = srv.get('status')
            if srv['connected']:
                right_lines.append(f"[dim {dim}]{srv['name']}[/] [{text}]({srv['transport']})[/] [dim {dim}]—[/] [{text}]{srv['tools']} tool(s)[/]")
            elif srv.get('disabled') or status == 'disabled':
                right_lines.append(f"[dim {dim}]{srv['name']}[/] [dim]({srv['transport']})[/] [dim {dim}]— disabled[/]")
            elif status == 'connecting':
                right_lines.append(f"[dim {dim}]{srv['name']}[/] [dim]({srv['transport']})[/] [yellow]— connecting[/]")
            elif status == 'configured':
                right_lines.append(f"[dim {dim}]{srv['name']}[/] [dim]({srv['transport']})[/] [dim {dim}]— configured[/]")
            else:
                right_lines.append(f"[red]{srv['name']}[/] [dim]({srv['transport']})[/] [red]— failed[/]")
    right_lines.append('')
    right_lines.append(f'[bold {accent}]Available Skills[/]')
    _skills_enabled = not _enabled_ts or 'skills' in _enabled_ts
    if _skills_enabled:
        skills_by_category = get_available_skills()
        total_skills = sum((len(s) for s in skills_by_category.values()))
    else:
        skills_by_category = {}
        total_skills = 0
    _term_cols = shutil.get_terminal_size().columns
    _right_col_width = max(int(_term_cols * 0.6) - 10, 30)
    if not _skills_enabled:
        right_lines.append(f'[dim {dim}]Skills toolset disabled[/]')
    elif skills_by_category:
        for category in sorted(skills_by_category.keys()):
            skill_names = sorted(skills_by_category[category])
            _prefix_len = len(category) + 2
            _avail = max(_right_col_width - _prefix_len, 20)
            parts, length = ([], 0)
            for i, name in enumerate(skill_names):
                _sep = ', ' if parts else ''
                _needed = len(_sep) + len(name)
                _after = len(skill_names) - (i + 1)
                _ind_len = len(f', +{_after} more') if _after > 0 else 0
                if parts and length + _needed + _ind_len > _avail:
                    remaining = len(skill_names) - len(parts)
                    parts.append(f'+{remaining} more')
                    break
                parts.append(name)
                length += _needed
            skills_str = ', '.join(parts)
            right_lines.append(f'[dim {dim}]{category}:[/] [{text}]{skills_str}[/]')
    else:
        right_lines.append(f'[dim {dim}]No skills installed[/]')
    right_lines.append('')
    mcp_connected = sum((1 for s in mcp_status if s['connected'])) if mcp_status else 0
    summary_parts = [f'{len(tools)} tools', f'{total_skills} skills']
    if mcp_connected:
        summary_parts.append(f'{mcp_connected} MCP servers')
    summary_parts.append('/help for commands')
    try:
        from hermes_cli.codex_runtime_switch import get_current_runtime
        from hermes_cli.config import load_config as _load_cfg
        if get_current_runtime(_load_cfg()) == 'codex_app_server':
            right_lines.append(f'[bold {accent}]Runtime:[/] [{text}]codex app-server[/] [dim {dim}](terminal/file ops/MCP run inside codex)[/]')
    except Exception:
        pass
    try:
        from hermes_cli.profiles import get_active_profile_name
        _profile_name = get_active_profile_name()
        if _profile_name and _profile_name != 'default':
            right_lines.append(f'[bold {accent}]Profile:[/] [{text}]{_profile_name}[/]')
    except Exception:
        pass
    right_lines.append(f"[dim {dim}]{' · '.join(summary_parts)}[/]")
    try:
        behind = get_update_result(timeout=0.5)
        if behind is not None and behind != 0:
            from hermes_cli.config import get_managed_update_command, recommended_update_command
            if behind > 0:
                commits_word = 'commit' if behind == 1 else 'commits'
                right_lines.append(f'[bold yellow]⚠ {behind} {commits_word} behind[/][dim yellow] — run [bold]{recommended_update_command()}[/bold] to update[/]')
            else:
                managed_cmd = get_managed_update_command()
                line = '[bold yellow]⚠ update available[/]'
                if managed_cmd:
                    line += f'[dim yellow] — run [bold]{managed_cmd}[/bold][/]'
                right_lines.append(line)
    except Exception:
        pass
    right_content = '\n'.join(right_lines)
    layout_table.add_row(left_content, right_content)
    title_color = _skin_color('banner_title', '#FFD700')
    border_color = _skin_color('banner_border', '#CD7F32')
    version_label = format_banner_version_label()
    release_info = get_latest_release_tag()
    if release_info:
        _tag, _url = release_info
        title_markup = f'[bold {title_color}][link={_url}]{version_label}[/link][/]'
    else:
        title_markup = f'[bold {title_color}]{version_label}[/]'
    outer_panel = Panel(layout_table, title=title_markup, border_style=border_color, padding=(0, 2))
    console.print()
    term_width = shutil.get_terminal_size().columns
    if term_width >= 95:
        _logo = _bskin.banner_logo if _bskin and hasattr(_bskin, 'banner_logo') and _bskin.banner_logo else HERMES_AGENT_LOGO
        console.print(_logo)
        console.print()
    console.print(outer_panel)