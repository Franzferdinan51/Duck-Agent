"""Table-driven config migration registry.

This module holds the per-version migration steps that used to live as a
768-line ladder of ``if current_ver < N:`` blocks inside
``hermes_cli.config.migrate_config``. Each step is a function
``_migrate_to_N(results, quiet)`` whose body is copied verbatim from the
original block; only the shared skeleton (the version gate and the strict
ascending ordering) lives in the :func:`run_migrations` driver.

Semantics preserved exactly from the original ladder:

* ``current_ver`` is computed ONCE by the caller (``check_config_version``)
  and never advances while the ladder runs — every step compares against the
  same initial value. The driver replicates that: it applies every registry
  entry whose target version is ``> current_ver``, in ascending order.
* Each step re-reads the raw on-disk config itself (``read_raw_config``) and
  persists via ``_persist_migration`` — steps therefore observe the writes of
  earlier steps through the filesystem, which is why strict ascending order
  is mandatory.
* All ``results['config_added']`` / ``results['warnings']`` appends and all
  conditional ``print`` output stay inside the step functions, byte-identical
  to the original blocks.

Import direction / cycle avoidance:

``hermes_cli.config`` imports :func:`run_migrations` lazily (inside
``migrate_config``), and every step function here resolves its helpers
(``read_raw_config``, ``_persist_migration``, ``get_env_value``, …) lazily
through the live ``hermes_cli.config`` module object at call time via
:func:`_cfg`. There is deliberately NO module-level import of
``hermes_cli.config`` here, so no circular import can form — and, just as
importantly, tests that monkeypatch helpers on ``hermes_cli.config`` (e.g.
``patch("hermes_cli.config.read_raw_config", ...)``) keep working, because
the steps always go through the module attribute rather than a bound-early
reference.
"""
from __future__ import annotations
import copy
from typing import Any, Callable, Dict, List, Tuple
SUPPORT_FLOOR_VERSION = 12

def support_floor_message() -> str:
    """Human-facing explanation shown when a config is below the floor."""
    from hermes_constants import display_hermes_home
    return f'This config predates version {SUPPORT_FLOOR_VERSION} (~2 years old) and can no longer be auto-migrated. Back up {display_hermes_home()}/config.yaml and run `duck-agent setup` to regenerate, or manually set _config_version: {SUPPORT_FLOOR_VERSION} after reviewing the changelog.'

def _cfg():
    """Return the live ``hermes_cli.config`` module (lazy, cycle-free)."""
    from hermes_cli import config
    return config

def _migrate_to_12(results: Dict[str, Any], quiet: bool) -> None:
    _c = _cfg()
    read_raw_config = _c.read_raw_config
    _persist_migration = _c._persist_migration
    _custom_provider_entry_to_provider_config = _c._custom_provider_entry_to_provider_config
    config = read_raw_config()
    custom_list = config.get('custom_providers')
    if isinstance(custom_list, list) and custom_list:
        providers_dict = config.get('providers', {})
        if not isinstance(providers_dict, dict):
            providers_dict = {}
        migrated_count = 0
        for entry in custom_list:
            if not isinstance(entry, dict):
                continue
            old_name = entry.get('name', '')
            old_url = entry.get('base_url', '') or entry.get('url', '') or entry.get('api', '') or ''
            if not old_url:
                continue
            key = old_name.strip().lower().replace(' ', '-').replace('(', '').replace(')', '')
            while '--' in key:
                key = key.replace('--', '-')
            key = key.strip('-')
            if not key:
                try:
                    from urllib.parse import urlparse
                    parsed = urlparse(old_url)
                    key = (parsed.hostname or 'endpoint').replace('.', '-')
                except Exception:
                    key = f'endpoint-{migrated_count}'
            base_key = key
            suffix = migrated_count
            while key in providers_dict:
                key = f'{base_key}-{suffix}'
                suffix += 1
            new_entry = _custom_provider_entry_to_provider_config(entry, provider_key=key)
            if new_entry is None:
                continue
            if not old_name:
                new_entry.pop('name', None)
            if new_entry.get('api_key') in {'no-key', 'no-key-required', ''}:
                new_entry.pop('api_key', None)
            providers_dict[key] = new_entry
            migrated_count += 1
        if migrated_count > 0:
            config['providers'] = providers_dict
            config.pop('custom_providers', None)
            _persist_migration(config)
            if not quiet:
                print(f'  ✓ Migrated {migrated_count} custom provider(s) to providers: section')
                for key in list(providers_dict.keys())[-migrated_count:]:
                    ep = providers_dict[key]
                    print(f"    → {key}: {ep.get('api', '')}")

def _migrate_to_13(results: Dict[str, Any], quiet: bool) -> None:
    _c = _cfg()
    get_env_value = _c.get_env_value
    save_env_value = _c.save_env_value
    for dead_var in ('LLM_MODEL', 'OPENAI_MODEL'):
        try:
            old_val = get_env_value(dead_var)
            if old_val:
                save_env_value(dead_var, '')
                if not quiet:
                    print(f'  ✓ Cleared {dead_var} from .env (no longer used — config.yaml is source of truth)')
        except Exception:
            pass

def _migrate_to_14(results: Dict[str, Any], quiet: bool) -> None:
    _c = _cfg()
    read_raw_config = _c.read_raw_config
    _persist_migration = _c._persist_migration
    raw = read_raw_config()
    raw_stt = raw.get('stt', {})
    if isinstance(raw_stt, dict) and 'model' in raw_stt:
        legacy_model = raw_stt['model']
        provider = raw_stt.get('provider', 'local')
        config = read_raw_config()
        stt = config.get('stt', {})
        stt.pop('model', None)
        if provider in {'local', 'local_command'}:
            _local_models = {'tiny.en', 'tiny', 'base.en', 'base', 'small.en', 'small', 'medium.en', 'medium', 'large-v1', 'large-v2', 'large-v3', 'large', 'distil-large-v2', 'distil-medium.en', 'distil-small.en', 'distil-large-v3', 'distil-large-v3.5', 'large-v3-turbo', 'turbo'}
            if legacy_model in _local_models:
                raw_local = raw_stt.get('local', {})
                if not isinstance(raw_local, dict) or 'model' not in raw_local:
                    local_cfg = stt.setdefault('local', {})
                    local_cfg['model'] = legacy_model
        else:
            raw_provider = raw_stt.get(provider, {})
            if not isinstance(raw_provider, dict) or 'model' not in raw_provider:
                provider_cfg = stt.setdefault(provider, {})
                provider_cfg['model'] = legacy_model
        config['stt'] = stt
        _persist_migration(config)
        if not quiet:
            print('  ✓ Migrated legacy stt.model to provider-specific config')

def _migrate_to_15(results: Dict[str, Any], quiet: bool) -> None:
    _c = _cfg()
    read_raw_config = _c.read_raw_config
    _persist_migration = _c._persist_migration
    config = read_raw_config()
    display = config.get('display', {})
    if not isinstance(display, dict):
        display = {}
    if 'interim_assistant_messages' not in display:
        display['interim_assistant_messages'] = True
        config['display'] = display
        results['config_added'].append('display.interim_assistant_messages=true (default)')
        _persist_migration(config)
        if not quiet:
            print('  ✓ Added display.interim_assistant_messages=true')

def _migrate_to_16(results: Dict[str, Any], quiet: bool) -> None:
    _c = _cfg()
    read_raw_config = _c.read_raw_config
    _persist_migration = _c._persist_migration
    config = read_raw_config()
    display = config.get('display', {})
    if not isinstance(display, dict):
        display = {}
    old_overrides = display.get('tool_progress_overrides')
    if isinstance(old_overrides, dict) and old_overrides:
        platforms = display.get('platforms', {})
        if not isinstance(platforms, dict):
            platforms = {}
        for plat, mode in old_overrides.items():
            if plat not in platforms:
                platforms[plat] = {}
            if 'tool_progress' not in platforms[plat]:
                platforms[plat]['tool_progress'] = mode
        display['platforms'] = platforms
        config['display'] = display
        _persist_migration(config)
        if not quiet:
            migrated = ', '.join((f'{p}={m}' for p, m in old_overrides.items()))
            print(f'  ✓ Migrated tool_progress_overrides → display.platforms: {migrated}')
        results['config_added'].append('display.platforms (migrated from tool_progress_overrides)')

def _migrate_to_17(results: Dict[str, Any], quiet: bool) -> None:
    _c = _cfg()
    read_raw_config = _c.read_raw_config
    _persist_migration = _c._persist_migration
    config = read_raw_config()
    comp = config.get('compression', {})
    if isinstance(comp, dict):
        s_model = comp.pop('summary_model', None)
        s_provider = comp.pop('summary_provider', None)
        s_base_url = comp.pop('summary_base_url', None)
        migrated_keys = []
        if s_model and str(s_model).strip():
            aux = config.setdefault('auxiliary', {})
            aux_comp = aux.setdefault('compression', {})
            if not aux_comp.get('model'):
                aux_comp['model'] = str(s_model).strip()
                migrated_keys.append(f'model={s_model}')
        if s_provider and str(s_provider).strip() not in {'', 'auto'}:
            aux = config.setdefault('auxiliary', {})
            aux_comp = aux.setdefault('compression', {})
            if not aux_comp.get('provider') or aux_comp.get('provider') == 'auto':
                aux_comp['provider'] = str(s_provider).strip()
                migrated_keys.append(f'provider={s_provider}')
        if s_base_url and str(s_base_url).strip():
            aux = config.setdefault('auxiliary', {})
            aux_comp = aux.setdefault('compression', {})
            if not aux_comp.get('base_url'):
                aux_comp['base_url'] = str(s_base_url).strip()
                migrated_keys.append(f'base_url={s_base_url}')
        if migrated_keys or s_model is not None or s_provider is not None or (s_base_url is not None):
            config['compression'] = comp
            _persist_migration(config)
            if not quiet:
                if migrated_keys:
                    print(f"  ✓ Migrated compression.summary_* → auxiliary.compression: {', '.join(migrated_keys)}")
                else:
                    print('  ✓ Removed unused compression.summary_* keys')

def _migrate_to_21(results: Dict[str, Any], quiet: bool) -> None:
    _c = _cfg()
    read_raw_config = _c.read_raw_config
    _persist_migration = _c._persist_migration
    get_hermes_home = _c.get_hermes_home
    fast_safe_load = _c.fast_safe_load
    config = read_raw_config()
    plugins_cfg = config.get('plugins')
    if not isinstance(plugins_cfg, dict):
        plugins_cfg = {}
    if 'enabled' not in plugins_cfg:
        disabled = plugins_cfg.get('disabled', []) or []
        if not isinstance(disabled, list):
            disabled = []
        disabled_set = set(disabled)
        grandfathered: List[str] = []
        try:
            user_plugins_dir = get_hermes_home() / 'plugins'
            if user_plugins_dir.is_dir():
                for child in sorted(user_plugins_dir.iterdir()):
                    if not child.is_dir():
                        continue
                    manifest_file = child / 'plugin.yaml'
                    if not manifest_file.exists():
                        manifest_file = child / 'plugin.yml'
                    if not manifest_file.exists():
                        continue
                    try:
                        with open(manifest_file, encoding='utf-8') as _mf:
                            manifest = fast_safe_load(_mf) or {}
                    except Exception:
                        manifest = {}
                    name = manifest.get('name') or child.name
                    if name in disabled_set:
                        continue
                    grandfathered.append(name)
        except Exception:
            grandfathered = []
        plugins_cfg['enabled'] = grandfathered
        config['plugins'] = plugins_cfg
        _persist_migration(config)
        results['config_added'].append(f'plugins.enabled (opt-in allow-list, {len(grandfathered)} grandfathered)')
        if not quiet:
            if grandfathered:
                print(f'  ✓ Plugins now opt-in: grandfathered {len(grandfathered)} existing plugin(s) into plugins.enabled')
            else:
                print('  ✓ Plugins now opt-in: no existing plugins to grandfather. Use `duck-agent plugins enable <name>` to activate.')

def _migrate_to_23(results: Dict[str, Any], quiet: bool) -> None:
    _c = _cfg()
    read_raw_config = _c.read_raw_config
    _persist_migration = _c._persist_migration
    get_hermes_home = _c.get_hermes_home
    DEFAULT_CONFIG = _c.DEFAULT_CONFIG
    try:
        curator_dir = get_hermes_home() / 'logs' / 'curator'
        curator_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        results['warnings'].append(f'Could not create {curator_dir}: {e}')
    config = read_raw_config()
    touched = False
    _curator_defaults = DEFAULT_CONFIG.get('curator', {})
    raw_curator = config.get('curator')
    if not isinstance(raw_curator, dict):
        raw_curator = {}
    added_curator: List[str] = []
    for k, v in _curator_defaults.items():
        if k not in raw_curator:
            raw_curator[k] = copy.deepcopy(v)
            added_curator.append(k)
    if added_curator:
        config['curator'] = raw_curator
        touched = True
    _aux_curator_defaults = DEFAULT_CONFIG.get('auxiliary', {}).get('curator', {})
    raw_aux = config.get('auxiliary')
    if not isinstance(raw_aux, dict):
        raw_aux = {}
    raw_aux_curator = raw_aux.get('curator')
    if not isinstance(raw_aux_curator, dict):
        raw_aux_curator = {}
    added_aux: List[str] = []
    for k, v in _aux_curator_defaults.items():
        if k not in raw_aux_curator:
            raw_aux_curator[k] = copy.deepcopy(v)
            added_aux.append(k)
    if added_aux:
        raw_aux['curator'] = raw_aux_curator
        config['auxiliary'] = raw_aux
        touched = True
    if touched:
        _persist_migration(config)
        if added_curator:
            results['config_added'].append(f'curator ({len(added_curator)} default key(s))')
            if not quiet:
                print(f"  ✓ Curator settings now available ({', '.join(added_curator)}) — edit via `duck-agent config set`")
        if added_aux:
            results['config_added'].append(f'auxiliary.curator ({len(added_aux)} default key(s))')
            if not quiet:
                print(f"  ✓ auxiliary.curator settings now available ({', '.join(added_aux)}) — edit via `duck-agent config set`")

def _migrate_to_25(results: Dict[str, Any], quiet: bool) -> None:
    _c = _cfg()
    read_raw_config = _c.read_raw_config
    _persist_migration = _c._persist_migration
    config = read_raw_config()
    raw_mc = config.get('model_catalog')
    if isinstance(raw_mc, dict) and raw_mc.get('ttl_hours') == 24:
        raw_mc['ttl_hours'] = 1
        config['model_catalog'] = raw_mc
        _persist_migration(config)
        results['config_added'].append('model_catalog.ttl_hours 24→1')
        if not quiet:
            print('  ✓ Lowered model_catalog.ttl_hours to 1 (hourly picker refresh)')

def _migrate_to_29(results: Dict[str, Any], quiet: bool) -> None:
    _c = _cfg()
    read_raw_config = _c.read_raw_config
    _persist_migration = _c._persist_migration
    config = read_raw_config()
    touched = False
    for subsystem in ('memory', 'skills'):
        sub = config.get(subsystem)
        if not isinstance(sub, dict) or 'write_mode' not in sub:
            continue
        old = sub.pop('write_mode')
        old_norm = old.strip().lower() if isinstance(old, str) else old
        sub['write_approval'] = old_norm == 'approve'
        config[subsystem] = sub
        touched = True
        results['config_added'].append(f"{subsystem}.write_mode → write_approval={sub['write_approval']}")
    if touched:
        _persist_migration(config)
        if not quiet:
            print('  ✓ Renamed write_mode → write_approval (boolean gate)')

def _migrate_to_31(results: Dict[str, Any], quiet: bool) -> None:
    _c = _cfg()
    read_raw_config = _c.read_raw_config
    _persist_migration = _c._persist_migration
    config = read_raw_config()
    raw_agent = config.get('agent')
    if not isinstance(raw_agent, dict):
        raw_agent = {}
    cur = raw_agent.get('verify_on_stop')
    is_auto_sentinel = isinstance(cur, str) and cur.strip().lower() == 'auto'
    if cur is None or is_auto_sentinel:
        raw_agent['verify_on_stop'] = False
        config['agent'] = raw_agent
        _persist_migration(config)
        results['config_added'].append('agent.verify_on_stop=false')
        if not quiet:
            print('  ✓ Turned off verify-on-stop (agent.verify_on_stop: false). Set it to true to re-enable, or "auto" for the legacy surface-aware behavior.')

def _migrate_to_32(results: Dict[str, Any], quiet: bool) -> None:
    _c = _cfg()
    read_raw_config = _c.read_raw_config
    _persist_migration = _c._persist_migration
    config = read_raw_config()
    raw_agent = config.get('agent')
    if isinstance(raw_agent, dict) and raw_agent.get('verify_on_stop') is True:
        raw_agent['verify_on_stop'] = False
        config['agent'] = raw_agent
        _persist_migration(config)
        results['config_added'].append('agent.verify_on_stop=false')
        if not quiet:
            print('  ✓ Turned off verify-on-stop (agent.verify_on_stop: false) — the old default was written into your config as a literal true. Set it to true again to re-enable, or "auto" for the legacy surface-aware behavior.')

def _migrate_to_33(results: Dict[str, Any], quiet: bool) -> None:
    _c = _cfg()
    read_raw_config = _c.read_raw_config
    _persist_migration = _c._persist_migration
    config = read_raw_config()
    raw_deleg = config.get('delegation')
    if isinstance(raw_deleg, dict) and 'max_async_children' in raw_deleg:
        old_async = raw_deleg.pop('max_async_children')
        try:
            old_async_i = int(old_async)
        except (TypeError, ValueError):
            old_async_i = None
        if old_async_i is not None and old_async_i > 3:
            try:
                cur_children = int(raw_deleg.get('max_concurrent_children', 3))
            except (TypeError, ValueError):
                cur_children = 3
            if old_async_i > cur_children:
                raw_deleg['max_concurrent_children'] = old_async_i
                results['config_added'].append(f'delegation.max_concurrent_children={old_async_i} (folded from deprecated max_async_children)')
        config['delegation'] = raw_deleg
        _persist_migration(config)
        if not quiet:
            print('  ✓ Removed deprecated delegation.max_async_children — delegation.max_concurrent_children now caps background delegations too.')
MIGRATIONS: Tuple[Tuple[int, Callable[[Dict[str, Any], bool], None]], ...] = ((12, _migrate_to_12), (13, _migrate_to_13), (14, _migrate_to_14), (15, _migrate_to_15), (16, _migrate_to_16), (17, _migrate_to_17), (21, _migrate_to_21), (23, _migrate_to_23), (25, _migrate_to_25), (29, _migrate_to_29), (31, _migrate_to_31), (32, _migrate_to_32), (33, _migrate_to_33))

def run_migrations(current_ver: int, results: Dict[str, Any], quiet: bool) -> None:
    """Apply every registered migration whose target version exceeds *current_ver*.

    Replicates the original ladder's semantics exactly: *current_ver* is the
    on-disk schema version captured ONCE (via ``check_config_version()``)
    before any step runs, and it does not advance between steps — each step
    is gated on the same initial value, exactly like the original sequential
    ``if current_ver < N:`` blocks. Steps run in strict ascending registry
    order and mutate ``results`` in place. The final ``_config_version`` bump
    is NOT performed here; it stays in ``migrate_config`` (persisted once,
    after the informational missing-config scan), matching the original flow.
    """
    for target_ver, migration_fn in MIGRATIONS:
        if current_ver < target_ver:
            migration_fn(results, quiet)