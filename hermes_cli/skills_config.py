"""
Skills configuration for Duck Agent.
`duck-agent skills` enters this module.

Toggle individual skills or categories on/off, globally or per-platform.
Config stored in ~/.duck-agent/config.yaml under:

  skills:
    disabled: [skill-a, skill-b]          # global disabled list
    platform_disabled:                    # per-platform overrides
      telegram: [skill-c]
      cli: []
"""
from typing import List, Optional, Set
from hermes_cli.config import cfg_get, load_config, save_config
from hermes_cli.colors import Colors, color
from hermes_cli.platforms import PLATFORMS as _PLATFORMS
PLATFORMS = {k: info.label for k, info in _PLATFORMS.items() if k != 'api_server'}

def _normalize_skill_names(values) -> Set[str]:
    """Normalize a config value into a set of skill names.

    Mirrors ``agent.skill_utils._normalize_string_set``: ``None`` (YAML null)
    means empty, a bare scalar (``disabled: my-skill``) means a single-item
    list — NOT a set of its characters (#13026).
    """
    if values is None:
        return set()
    if isinstance(values, str):
        values = [values]
    try:
        return {str(v).strip() for v in values if str(v).strip()}
    except TypeError:
        return set()

def get_disabled_skills(config: dict, platform: Optional[str]=None) -> Set[str]:
    """Return disabled skill names: the global list unioned with the
    platform-specific list when a platform is given.

    A globally-disabled skill stays disabled on every platform, so the
    platform list adds to the global list rather than replacing it. This
    mirrors ``agent.skill_utils.get_disabled_skill_names``.
    """
    skills_cfg = config.get('skills') or {}
    if not isinstance(skills_cfg, dict):
        return set()
    global_disabled = _normalize_skill_names(skills_cfg.get('disabled'))
    if platform is None:
        return global_disabled
    platform_disabled = cfg_get(skills_cfg, 'platform_disabled', platform)
    if platform_disabled is None:
        return global_disabled
    return global_disabled | _normalize_skill_names(platform_disabled)

def save_disabled_skills(config: dict, disabled: Set[str], platform: Optional[str]=None):
    """Persist disabled skill names to config."""
    config.setdefault('skills', {})
    if platform is None:
        config['skills']['disabled'] = sorted(disabled)
    else:
        config['skills'].setdefault('platform_disabled', {})
        config['skills']['platform_disabled'][platform] = sorted(disabled)
    save_config(config)

def _list_all_skills() -> List[dict]:
    """Return all installed skills (ignoring disabled state)."""
    try:
        from tools.skills_tool import _find_all_skills
        return _find_all_skills(skip_disabled=True)
    except Exception:
        return []

def _get_categories(skills: List[dict]) -> List[str]:
    """Return sorted unique category names (None -> 'uncategorized')."""
    return sorted({s['category'] or 'uncategorized' for s in skills})

def _select_platform() -> Optional[str]:
    """Ask user which platform to configure, or global."""
    options = [('global', 'All platforms (global default)')] + list(PLATFORMS.items())
    print()
    print(color('  Configure skills for:', Colors.BOLD))
    for i, (key, label) in enumerate(options, 1):
        print(f'  {i}. {label}')
    print()
    try:
        raw = input(color('  Select [1]: ', Colors.YELLOW)).strip()
    except (KeyboardInterrupt, EOFError):
        return None
    if not raw:
        return None
    try:
        idx = int(raw) - 1
        if 0 <= idx < len(options):
            key = options[idx][0]
            return None if key == 'global' else key
    except ValueError:
        pass
    return None

def _toggle_by_category(skills: List[dict], disabled: Set[str]) -> Set[str]:
    """Toggle all skills in a category at once."""
    from hermes_cli.curses_ui import curses_checklist
    categories = _get_categories(skills)
    cat_labels = []
    pre_selected = set()
    for i, cat in enumerate(categories):
        cat_skills = [s['name'] for s in skills if (s['category'] or 'uncategorized') == cat]
        cat_labels.append(f'{cat} ({len(cat_skills)} skills)')
        if not all((s in disabled for s in cat_skills)):
            pre_selected.add(i)
    chosen = curses_checklist('Categories — toggle entire categories', cat_labels, pre_selected, cancel_returns=pre_selected)
    new_disabled = set(disabled)
    for i, cat in enumerate(categories):
        cat_skills = {s['name'] for s in skills if (s['category'] or 'uncategorized') == cat}
        if i in chosen:
            new_disabled -= cat_skills
        else:
            new_disabled |= cat_skills
    return new_disabled

def skills_command(args=None):
    """Entry point for `duck-agent skills`."""
    from hermes_cli.curses_ui import curses_checklist
    config = load_config()
    skills = _list_all_skills()
    if not skills:
        print(color('  No skills installed.', Colors.DIM))
        return
    platform = _select_platform()
    platform_label = PLATFORMS.get(platform, 'All platforms') if platform else 'All platforms'
    print()
    print(color(f'  Configure for: {platform_label}', Colors.DIM))
    print()
    print('  1. Toggle individual skills')
    print('  2. Toggle by category')
    print()
    try:
        mode = input(color('  Select [1]: ', Colors.YELLOW)).strip() or '1'
    except (KeyboardInterrupt, EOFError):
        return
    disabled = get_disabled_skills(config, platform)
    if mode == '2':
        new_disabled = _toggle_by_category(skills, disabled)
    else:
        labels = [f"{s['name']}  ({s['category'] or 'uncategorized'})  —  {s['description'][:55]}" for s in skills]
        pre_selected = {i for i, s in enumerate(skills) if s['name'] not in disabled}
        chosen = curses_checklist(f'Skills for {platform_label}', labels, pre_selected, cancel_returns=pre_selected)
        new_disabled = {skills[i]['name'] for i in range(len(skills)) if i not in chosen}
    if new_disabled == disabled:
        print(color('  No changes.', Colors.DIM))
        return
    save_disabled_skills(config, new_disabled, platform)
    enabled_count = len(skills) - len(new_disabled)
    print(color(f'✓ Saved: {enabled_count} enabled, {len(new_disabled)} disabled ({platform_label}).', Colors.GREEN))