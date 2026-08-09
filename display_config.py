"""Per-platform display/verbosity configuration resolver.

Provides ``resolve_display_setting()`` — the single entry-point for reading
display settings with platform-specific overrides and sensible defaults.

Resolution order (first non-None wins):
    1. ``display.platforms.<platform>.<key>``  — explicit per-platform user override
    2. ``display.<key>``                       — global user setting
    3. ``_PLATFORM_DEFAULTS[<platform>][<key>]``  — built-in sensible default
    4. ``_GLOBAL_DEFAULTS[<key>]``              — built-in global default

Exception: ``display.streaming`` is CLI-only.  Gateway streaming follows the
top-level ``streaming`` config unless ``display.platforms.<platform>.streaming``
sets an explicit per-platform override.

Backward compatibility: ``display.tool_progress_overrides`` is still read as a
fallback for ``tool_progress`` when no ``display.platforms`` entry exists.  A
config migration (version bump) automatically moves the old format into the new
``display.platforms`` structure.
"""
from __future__ import annotations
from typing import Any
_GLOBAL_DEFAULTS: dict[str, Any] = {'tool_progress': 'all', 'tool_progress_grouping': 'accumulate', 'show_reasoning': False, 'reasoning_style': 'code', 'tool_preview_length': 0, 'streaming': None, 'interim_assistant_messages': True, 'long_running_notifications': True, 'busy_ack_detail': True, 'busy_steer_ack_enabled': True, 'cleanup_progress': False, 'live_status': 'full'}
_TIER_HIGH = {'tool_progress': 'all', 'show_reasoning': False, 'tool_preview_length': 40, 'streaming': None, 'interim_assistant_messages': True, 'long_running_notifications': True, 'busy_ack_detail': True}
_TIER_MEDIUM = {'tool_progress': 'new', 'show_reasoning': False, 'tool_preview_length': 40, 'streaming': None, 'interim_assistant_messages': True, 'long_running_notifications': True, 'busy_ack_detail': True}
_TIER_LOW = {'tool_progress': 'off', 'show_reasoning': False, 'tool_preview_length': 40, 'streaming': False, 'interim_assistant_messages': False, 'long_running_notifications': False, 'busy_ack_detail': False}
_TIER_MINIMAL = {'tool_progress': 'off', 'show_reasoning': False, 'tool_preview_length': 0, 'streaming': False, 'interim_assistant_messages': False, 'long_running_notifications': False, 'busy_ack_detail': False}
_PLATFORM_DEFAULTS: dict[str, dict[str, Any]] = {'telegram': {**_TIER_HIGH, 'tool_progress': 'off', 'busy_ack_detail': False}, 'discord': {**_TIER_HIGH, 'reasoning_style': 'subtext'}, 'slack': {**_TIER_MEDIUM, 'tool_progress': 'off', 'long_running_notifications': False, 'busy_ack_detail': False}, 'mattermost': _TIER_MEDIUM, 'matrix': _TIER_MEDIUM, 'feishu': _TIER_MEDIUM, 'signal': _TIER_LOW, 'whatsapp': _TIER_MEDIUM, 'whatsapp_cloud': _TIER_LOW, 'photon': _TIER_LOW, 'bluebubbles': _TIER_LOW, 'weixin': _TIER_LOW, 'wecom': _TIER_LOW, 'wecom_callback': _TIER_LOW, 'dingtalk': _TIER_LOW, 'email': _TIER_MINIMAL, 'sms': _TIER_MINIMAL, 'webhook': _TIER_MINIMAL, 'homeassistant': _TIER_MINIMAL, 'api_server': {**_TIER_HIGH, 'tool_preview_length': 0}}
OVERRIDEABLE_KEYS = frozenset(_GLOBAL_DEFAULTS.keys())

def resolve_display_setting(user_config: dict, platform_key: str, setting: str, fallback: Any=None) -> Any:
    """Resolve a display setting with per-platform override support.

    Parameters
    ----------
    user_config : dict
        The full parsed config.yaml dict.
    platform_key : str
        Platform config key (e.g. ``"telegram"``, ``"slack"``).  Use
        ``_platform_config_key(source.platform)`` from gateway/run.py.
    setting : str
        Display setting name (e.g. ``"tool_progress"``, ``"show_reasoning"``).
    fallback : Any
        Fallback value when the setting isn't found anywhere.

    Returns
    -------
    The resolved value, or *fallback* if nothing is configured.
    """
    display_cfg = user_config.get('display') or {}
    platforms = display_cfg.get('platforms') or {}
    plat_overrides = platforms.get(platform_key)
    if isinstance(plat_overrides, dict):
        val = plat_overrides.get(setting)
        if val is not None:
            return _normalise(setting, val)
    if setting == 'tool_progress':
        legacy = display_cfg.get('tool_progress_overrides')
        if isinstance(legacy, dict):
            val = legacy.get(platform_key)
            if val is not None:
                return _normalise(setting, val)
    if setting != 'streaming':
        val = display_cfg.get(setting)
        if val is not None:
            return _normalise(setting, val)
    plat_defaults = _PLATFORM_DEFAULTS.get(platform_key)
    if plat_defaults:
        val = plat_defaults.get(setting)
        if val is not None:
            return val
    val = _GLOBAL_DEFAULTS.get(setting)
    if val is not None:
        return val
    return fallback

def _normalise(setting: str, value: Any) -> Any:
    """Normalise YAML quirks (bare ``off`` → False in YAML 1.1)."""
    if setting == 'tool_progress':
        if value is False:
            return 'off'
        if value is True:
            return 'all'
        val = str(value).strip().lower()
        if val in {'false', '0', 'no'}:
            return 'off'
        if val in {'true', '1', 'yes', 'on'}:
            return 'all'
        return val if val in {'off', 'new', 'all', 'verbose', 'log'} else 'all'
    if setting in {'show_reasoning', 'streaming', 'interim_assistant_messages', 'long_running_notifications', 'busy_ack_detail', 'busy_steer_ack_enabled', 'thinking_progress'}:
        if isinstance(value, str):
            val = value.strip().lower()
            if val == 'generic' and setting == 'long_running_notifications':
                return 'generic'
            return val in {'true', '1', 'yes', 'on', 'raw', 'verbose'}
        return bool(value)
    if setting == 'cleanup_progress':
        if isinstance(value, str):
            return value.lower() in {'true', '1', 'yes', 'on'}
        return bool(value)
    if setting == 'live_status':
        if value is True:
            return 'full'
        if value is False:
            return 'off'
        val = str(value).strip().lower()
        if val in {'true', '1', 'yes', 'on', 'all'}:
            return 'full'
        if val in {'false', '0', 'no'}:
            return 'off'
        return val if val in {'full', 'verb', 'off'} else 'full'
    if setting == 'tool_progress_grouping':
        val = str(value).lower()
        return val if val in ('accumulate', 'separate') else 'accumulate'
    if setting == 'reasoning_style':
        val = str(value).lower()
        return val if val in ('code', 'blockquote', 'subtext') else 'code'
    if setting == 'tool_preview_length':
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    return value