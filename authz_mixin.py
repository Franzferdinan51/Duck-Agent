"""User-authorization methods for ``GatewayRunner``.

Extracted from ``gateway/run.py`` as part of the god-file decomposition campaign
(``~/.duck-agent/plans/god-file-decomposition.md``, Phase 3 mechanical mixin lifts).
This mixin holds the inbound-message authorization cluster: whether a user/chat
is allowed to talk to the agent, the per-adapter DM policy, and the
unauthorized-DM behavior.

Behavior-neutral: every method is lifted verbatim from ``GatewayRunner``.
``self.*`` calls resolve unchanged via the MRO. Neutral dependencies import at
module top; the module-level ``logger`` is imported lazily inside the one method
that uses it (``from gateway.run import logger`` resolves at call time, when
``gateway.run`` is fully loaded) so this module never imports ``gateway.run`` at
import time -> no import cycle. The lazy import preserves the exact logger name
(``"gateway.run"``) so log records are unchanged.
"""
from __future__ import annotations
import os
from typing import Optional
from gateway.config import Platform
from gateway.session import SessionSource
from gateway.whatsapp_identity import expand_whatsapp_aliases as _expand_whatsapp_auth_aliases, normalize_whatsapp_identifier as _normalize_whatsapp_identifier

def _auth_env(name: str, default: str='') -> str:
    """Read allowlist/auth env; prefer profile secret_scope under multiplex."""
    if not name:
        return default
    try:
        from agent.secret_scope import get_secret
        val = get_secret(name)
        if val is not None and str(val).strip():
            return str(val).strip()
    except Exception:
        pass
    return (os.getenv(name) or default).strip()

def _platform_gate_env(name: str, default: str='') -> str:
    """Read a platform allow/deny gate env var with per-profile isolation.

    Like ``_auth_env`` but authoritative under multiplex: when a profile
    secret scope is installed AND multiplexing is active, a key absent from
    the scope returns ``default`` instead of falling through to
    ``os.environ``. Under multiplex the process env may hold ANOTHER
    profile's first-writer-bridged value (the YAML→env bridges in the
    Discord/Telegram adapters' ``_apply_yaml_config`` are first-writer-wins),
    so falling through would leak profile A's allowlist into profile B
    (issue #72348). Single-profile deployments — no scope installed, or
    multiplex off — behave exactly like the legacy ``os.getenv`` read.
    """
    if not name:
        return default
    try:
        from agent.secret_scope import current_secret_scope, is_multiplex_active
        scope = current_secret_scope()
        if scope is not None and is_multiplex_active():
            val = scope.get(name)
            if val is None:
                return default
            return str(val).strip()
    except Exception:
        pass
    return (os.getenv(name) or default).strip()

def _coerce_allow_set(raw) -> set[str]:
    """Parse allowlist values from config or env var into a set of strings.

    Handles both list inputs (YAML sequences) and comma-separated string
    inputs (env vars or scalar YAML values).  A scalar string is split on
    commas so ``allow_from: "123,456"`` yields ``{"123", "456"}``, not
    ``{"1", "2", "3", ",", ...}``.
    """
    if raw is None:
        return set()
    if isinstance(raw, list):
        return {str(part).strip() for part in raw if str(part).strip()}
    return {part.strip() for part in str(raw).split(',') if part.strip()}

class GatewayAuthorizationMixin:
    """User/chat authorization methods for ``GatewayRunner``."""

    def _authorization_adapter(self, platform: Optional[Platform], profile: Optional[str]=None):
        """Resolve the live adapter whose intake policy should gate authorization.

        In multiplex mode, secondary-profile adapters live in
        ``_profile_adapters[profile]`` while the default/active profile uses
        ``self.adapters``. ``SessionSource.profile`` selects which map to consult.
        When a stamped profile has its own adapter registry entry, the default
        profile's same-platform adapter must not be consulted as a fallback.
        """
        if not platform:
            return None
        profile_name = (profile or '').strip() or None
        if profile_name and profile_name != 'default':
            active_profile = None
            active_profile_fn = getattr(self, '_active_profile_name', None)
            if callable(active_profile_fn):
                try:
                    active_profile = active_profile_fn()
                except Exception:
                    active_profile = None
            if profile_name == active_profile:
                adapters = getattr(self, 'adapters', None) or {}
                return adapters.get(platform)
            profile_adapters = getattr(self, '_profile_adapters', None) or {}
            if profile_name in profile_adapters:
                return profile_adapters[profile_name].get(platform)
            return None
        adapters = getattr(self, 'adapters', None) or {}
        return adapters.get(platform)

    def _adapter_for_source(self, source: Optional[SessionSource]):
        """Resolve the live adapter for an inbound ``SessionSource``."""
        if source is None:
            return None
        transport_adapter = self._registered_transport_adapter(source)
        if transport_adapter is not None:
            return transport_adapter
        if getattr(source, 'delivered_via_upstream_relay', False) is True:
            adapters = getattr(self, 'adapters', None) or {}
            return adapters.get(Platform.RELAY)
        return self._authorization_adapter(getattr(source, 'platform', None), getattr(source, 'profile', None))

    def _registered_transport_adapter(self, source: SessionSource):
        """Return the registered adapter that created *source*, if retained.

        ``source.profile`` is the runtime/session namespace. A chat-based
        profile route can therefore differ from the adapter profile when one
        shared credential serves several routed runtimes. ``build_source``
        keeps the receiving adapter as in-process provenance so replies and
        intake-policy checks stay on that transport without weakening the
        fail-closed fallback for restored or hand-built sources.
        """
        adapter_ref = getattr(source, '_transport_adapter_ref', None)
        adapter = adapter_ref() if callable(adapter_ref) else None
        platform = getattr(source, 'platform', None)
        if adapter is None or platform is None:
            return None
        if adapter is (getattr(self, 'adapters', None) or {}).get(platform):
            return adapter
        profile_maps = getattr(self, '_profile_adapters', None) or {}
        for profile_adapters in profile_maps.values():
            if adapter is profile_adapters.get(platform):
                return adapter
        return None

    def _adapter_profile_for_source(self, source: SessionSource) -> Optional[str]:
        """Resolve the transport-owning profile for adapter policy lookups."""
        adapter = self._registered_transport_adapter(source)
        platform = getattr(source, 'platform', None)
        if adapter is not None:
            if adapter is (getattr(self, 'adapters', None) or {}).get(platform):
                return None
            for profile, profile_adapters in (getattr(self, '_profile_adapters', None) or {}).items():
                if adapter is profile_adapters.get(platform):
                    return profile
        return getattr(source, 'profile', None)

    def _adapter_authorization_is_upstream(self, platform: Optional[Platform], *, profile: Optional[str]=None) -> bool:
        """Whether the adapter for *platform* delegates authz to a trusted upstream.

        Mirrors ``BasePlatformAdapter.authorization_is_upstream``. The relay
        adapter sets this True: the Team Gateway connector authenticates the
        gateway's WS and resolves owner-only author bindings before delivering,
        so an inbound relay event is already authorized as this instance's bound
        user. Unlike ``_adapter_enforces_own_access_policy`` (a LOCAL config
        policy the gateway mirrors only when it's an allowlist), this is an
        UPSTREAM decision the gateway honors directly. Defaults to ``False`` when
        the adapter is unknown or doesn't expose the flag.
        """
        if not platform:
            return False
        adapter = self._authorization_adapter(platform, profile)
        if adapter is None:
            return False
        return bool(getattr(adapter, 'authorization_is_upstream', False))

    def _adapter_enforces_own_access_policy(self, platform: Optional[Platform], *, profile: Optional[str]=None) -> bool:
        """Whether the adapter for *platform* gates access at intake itself.

        Mirrors ``BasePlatformAdapter.enforces_own_access_policy``. Adapters
        such as WeCom, Weixin, Yuanbao, QQBot, and WhatsApp evaluate their
        documented ``dm_policy`` / ``group_policy`` / ``allow_from`` config before a
        message is dispatched to the gateway. The flag alone is NOT "already
        authorized": these adapters default to ``open``, which forwards every
        sender, so ``_is_user_authorized`` only trusts the adapter when its
        effective policy for the chat type is an actual ``allowlist`` restriction
        (see that method). Defaults to ``False`` when the adapter is unknown or
        doesn't expose the flag.
        """
        if not platform:
            return False
        adapter = self._authorization_adapter(platform, profile)
        if adapter is None:
            return False
        return bool(getattr(adapter, 'enforces_own_access_policy', False))

    def _adapter_dm_policy(self, platform: Optional[Platform], *, profile: Optional[str]=None) -> str:
        """Best-effort read of an own-policy adapter's effective DM policy.

        Returns the lowercased ``dm_policy`` (``"open"`` / ``"allowlist"`` /
        ``"disabled"`` / ``"pairing"``) for *platform*, or ``""`` when unknown.
        Prefers the live adapter's resolved ``_dm_policy`` — which already folds
        in both ``config.extra`` and the ``<PLATFORM>_DM_POLICY`` env var (the
        env var is not always bridged back into ``config.extra``) — and falls
        back to ``config.extra`` for bare runners built without a live adapter.

        Used by ``_is_user_authorized`` to decide whether an own-policy adapter
        actually restricted DM senders to a configured allowlist (trustworthy)
        or merely forwarded everyone under ``dm_policy: open`` / for a pairing
        handshake (not authorization). "Reached the gateway" only carries an
        authorization signal in the ``allowlist`` case.
        """
        if not platform:
            return ''
        adapter = self._authorization_adapter(platform, profile)
        policy = getattr(adapter, '_dm_policy', None) if adapter is not None else None
        if policy is None:
            config = getattr(self, 'config', None)
            platform_cfg = config.platforms.get(platform) if config is not None and hasattr(config, 'platforms') else None
            extra = getattr(platform_cfg, 'extra', None) if platform_cfg else None
            if isinstance(extra, dict):
                policy = extra.get('dm_policy')
        return str(policy or '').strip().lower()

    def _adapter_group_policy(self, platform: Optional[Platform], *, profile: Optional[str]=None) -> str:
        """Best-effort read of an own-policy adapter's effective group policy.

        Mirror of ``_adapter_dm_policy`` for group / forum / channel traffic:
        returns the lowercased ``group_policy`` (``"open"`` / ``"allowlist"`` /
        ``"disabled"``) for *platform*, or ``""`` when unknown. Prefers the live
        adapter's resolved ``_group_policy`` and falls back to ``config.extra``
        for bare runners built without a live adapter.

        Used by ``_is_user_authorized`` to decide whether an own-policy adapter
        restricted group senders to a configured allowlist (trustworthy) or
        forwarded the whole channel under ``group_policy: open`` (not
        authorization).
        """
        if not platform:
            return ''
        adapter = self._authorization_adapter(platform, profile)
        policy = getattr(adapter, '_group_policy', None) if adapter is not None else None
        if policy is None:
            config = getattr(self, 'config', None)
            platform_cfg = config.platforms.get(platform) if config is not None and hasattr(config, 'platforms') else None
            extra = getattr(platform_cfg, 'extra', None) if platform_cfg else None
            if isinstance(extra, dict):
                policy = extra.get('group_policy')
        return str(policy or '').strip().lower()

    def _adapter_group_has_sender_allowlist(self, platform: Optional[Platform], chat_id: Optional[str], *, profile: Optional[str]=None) -> bool:
        """Whether a per-group sender allowlist gated this group message.

        WeCom supports ``groups.<group_id>.allow_from`` on top of the top-level
        ``group_policy``. A group may be open at the chat level while still
        restricting which senders inside that group can invoke Duck Agent. If such a
        message reached the gateway, the adapter already checked that sender
        allowlist, so it is a trustworthy intake decision rather than the
        fail-open ``group_policy: open`` case.
        """
        if not platform or not chat_id:
            return False
        adapter = self._authorization_adapter(platform, profile)
        groups = getattr(adapter, '_groups', None) if adapter is not None else None
        if groups is None:
            config = getattr(self, 'config', None)
            platform_cfg = config.platforms.get(platform) if config is not None and hasattr(config, 'platforms') else None
            extra = getattr(platform_cfg, 'extra', None) if platform_cfg else None
            if isinstance(extra, dict):
                groups = extra.get('groups')
        if not isinstance(groups, dict):
            return False
        chat_id_str = str(chat_id)
        group_cfg = groups.get(chat_id_str)
        if not isinstance(group_cfg, dict):
            lowered = chat_id_str.lower()
            for key, value in groups.items():
                if isinstance(key, str) and key.lower() == lowered and isinstance(value, dict):
                    group_cfg = value
                    break
        if not isinstance(group_cfg, dict):
            group_cfg = groups.get('*')
        if not isinstance(group_cfg, dict):
            return False
        sender_allow = group_cfg.get('allow_from') or group_cfg.get('allowFrom')
        if isinstance(sender_allow, str):
            return bool(sender_allow.strip())
        if isinstance(sender_allow, (list, tuple, set)):
            return any((str(item).strip() for item in sender_allow))
        return False

    def _pairing_store_for(self, source: 'SessionSource'):
        """Pick the per-profile PairingStore for a source, falling back to global.

        In a multiplexing gateway, each profile owns its own pairing whitelist
        so isolation is preserved. When the source has no profile (single-
        profile gateway, or a path that hasn't stamped profile yet) or the
        profile isn't registered, fall back to ``self.pairing_store`` (the
        global default) so existing behavior is preserved.
        """
        per_profile = getattr(self, 'pairing_stores', None) or {}
        profile = getattr(source, 'profile', None)
        if profile and profile in per_profile:
            return per_profile[profile]
        return getattr(self, 'pairing_store', None)

    def _is_user_authorized(self, source: SessionSource) -> bool:
        """
        Check if a user is authorized to use the bot.
        
        Checks in order:
        1. Per-platform allow-all flag (e.g., DISCORD_ALLOW_ALL_USERS=true)
        2. Environment variable allowlists (TELEGRAM_ALLOWED_USERS, etc.)
        3. DM pairing approved list
        4. Global allow-all (GATEWAY_ALLOW_ALL_USERS=true)
        5. Default: deny
        """
        from gateway.run import logger
        if source.platform in {Platform.HOMEASSISTANT, Platform.WEBHOOK}:
            return True
        adapter_profile = self._adapter_profile_for_source(source)
        if source.delivered_via_upstream_relay is True or self._adapter_authorization_is_upstream(source.platform, profile=adapter_profile):
            return True
        user_id = source.user_id
        if source.chat_type in {'group', 'forum', 'channel'} and source.chat_id:
            chat_allowlist_env = {Platform.TELEGRAM: 'TELEGRAM_GROUP_ALLOWED_CHATS', Platform.QQBOT: 'QQ_GROUP_ALLOWED_USERS'}.get(source.platform, '')
            if chat_allowlist_env:
                raw_chat_allowlist = _platform_gate_env(chat_allowlist_env)
                if raw_chat_allowlist:
                    allowed_group_ids = {cid.strip() for cid in raw_chat_allowlist.split(',') if cid.strip()}
                    if '*' in allowed_group_ids or source.chat_id in allowed_group_ids:
                        return True
            try:
                adapter = self._adapter_for_source(source)
                if adapter is not None:
                    extra = getattr(getattr(adapter, 'config', None), 'extra', None) or {}
                    adapter_group_allowed = extra.get('group_allowed_chats')
                    if adapter_group_allowed:
                        allowed = _coerce_allow_set(adapter_group_allowed)
                        if '*' in allowed or source.chat_id in allowed:
                            return True
            except Exception:
                pass
        platform_allow_bots_map = {Platform.DISCORD: 'DISCORD_ALLOW_BOTS', Platform.FEISHU: 'FEISHU_ALLOW_BOTS', Platform.TELEGRAM: 'TELEGRAM_ALLOW_BOTS', Platform.SLACK: 'SLACK_ALLOW_BOTS'}
        if getattr(source, 'is_bot', False):
            allow_bots_var = platform_allow_bots_map.get(source.platform)
            if allow_bots_var and _platform_gate_env(allow_bots_var, 'none').lower().strip() in {'mentions', 'all'}:
                return True
        if not user_id:
            return False
        platform_env_map = {Platform.TELEGRAM: 'TELEGRAM_ALLOWED_USERS', Platform.DISCORD: 'DISCORD_ALLOWED_USERS', Platform.WHATSAPP: 'WHATSAPP_ALLOWED_USERS', Platform.WHATSAPP_CLOUD: 'WHATSAPP_CLOUD_ALLOWED_USERS', Platform.SLACK: 'SLACK_ALLOWED_USERS', Platform.SIGNAL: 'SIGNAL_ALLOWED_USERS', Platform.EMAIL: 'EMAIL_ALLOWED_USERS', Platform.SMS: 'SMS_ALLOWED_USERS', Platform.MATTERMOST: 'MATTERMOST_ALLOWED_USERS', Platform.MATRIX: 'MATRIX_ALLOWED_USERS', Platform.DINGTALK: 'DINGTALK_ALLOWED_USERS', Platform.FEISHU: 'FEISHU_ALLOWED_USERS', Platform.WECOM: 'WECOM_ALLOWED_USERS', Platform.WECOM_CALLBACK: 'WECOM_CALLBACK_ALLOWED_USERS', Platform.WEIXIN: 'WEIXIN_ALLOWED_USERS', Platform.BLUEBUBBLES: 'BLUEBUBBLES_ALLOWED_USERS', Platform.QQBOT: 'QQ_ALLOWED_USERS', Platform.YUANBAO: 'YUANBAO_ALLOWED_USERS'}
        platform_group_user_env_map = {Platform.TELEGRAM: 'TELEGRAM_GROUP_ALLOWED_USERS'}
        platform_group_chat_env_map = {Platform.TELEGRAM: 'TELEGRAM_GROUP_ALLOWED_CHATS', Platform.QQBOT: 'QQ_GROUP_ALLOWED_USERS'}
        platform_allow_all_map = {Platform.TELEGRAM: 'TELEGRAM_ALLOW_ALL_USERS', Platform.DISCORD: 'DISCORD_ALLOW_ALL_USERS', Platform.WHATSAPP: 'WHATSAPP_ALLOW_ALL_USERS', Platform.WHATSAPP_CLOUD: 'WHATSAPP_CLOUD_ALLOW_ALL_USERS', Platform.SLACK: 'SLACK_ALLOW_ALL_USERS', Platform.SIGNAL: 'SIGNAL_ALLOW_ALL_USERS', Platform.EMAIL: 'EMAIL_ALLOW_ALL_USERS', Platform.SMS: 'SMS_ALLOW_ALL_USERS', Platform.MATTERMOST: 'MATTERMOST_ALLOW_ALL_USERS', Platform.MATRIX: 'MATRIX_ALLOW_ALL_USERS', Platform.DINGTALK: 'DINGTALK_ALLOW_ALL_USERS', Platform.FEISHU: 'FEISHU_ALLOW_ALL_USERS', Platform.WECOM: 'WECOM_ALLOW_ALL_USERS', Platform.WECOM_CALLBACK: 'WECOM_CALLBACK_ALLOW_ALL_USERS', Platform.WEIXIN: 'WEIXIN_ALLOW_ALL_USERS', Platform.BLUEBUBBLES: 'BLUEBUBBLES_ALLOW_ALL_USERS', Platform.QQBOT: 'QQ_ALLOW_ALL_USERS', Platform.YUANBAO: 'YUANBAO_ALLOW_ALL_USERS'}
        if source.platform not in platform_env_map:
            try:
                from gateway.platform_registry import platform_registry
                entry = platform_registry.get(source.platform.value)
                if entry:
                    if entry.allowed_users_env:
                        platform_env_map[source.platform] = entry.allowed_users_env
                    if entry.allow_all_env:
                        platform_allow_all_map[source.platform] = entry.allow_all_env
            except Exception:
                pass
        platform_allow_all_var = platform_allow_all_map.get(source.platform, '')
        if platform_allow_all_var and _auth_env(platform_allow_all_var).lower() in {'true', '1', 'yes'}:
            return True
        if getattr(source, 'role_authorized', False) is True:
            return True
        platform_name = source.platform.value if source.platform else ''
        pairing_store = self._pairing_store_for(source)
        if pairing_store is not None and pairing_store.is_approved(platform_name, user_id):
            return True
        platform_allowlist = _auth_env(platform_env_map.get(source.platform, ''))
        group_user_allowlist = ''
        group_chat_allowlist = ''
        if source.chat_type in {'group', 'forum'}:
            group_user_allowlist = _auth_env(platform_group_user_env_map.get(source.platform, ''))
            group_chat_allowlist = _auth_env(platform_group_chat_env_map.get(source.platform, ''))
        global_allowlist = _auth_env('GATEWAY_ALLOWED_USERS')
        if not platform_allowlist and (not group_user_allowlist) and (not group_chat_allowlist) and (not global_allowlist):
            if self._adapter_enforces_own_access_policy(source.platform, profile=adapter_profile):
                if source.chat_type in {'group', 'forum', 'channel'}:
                    effective_policy = self._adapter_group_policy(source.platform, profile=adapter_profile)
                    if self._adapter_group_has_sender_allowlist(source.platform, source.chat_id, profile=adapter_profile):
                        return True
                else:
                    effective_policy = self._adapter_dm_policy(source.platform, profile=adapter_profile)
                if effective_policy == 'allowlist':
                    if source.chat_type not in {'group', 'forum', 'channel'}:
                        adapter = self._authorization_adapter(source.platform, profile=adapter_profile)
                        dm_check = getattr(adapter, '_is_dm_allowed', None) if adapter is not None else None
                        if callable(dm_check):
                            return bool(dm_check(user_id))
                    return True
            adapter = self._adapter_for_source(source)
            if adapter is not None:
                extra = getattr(getattr(adapter, 'config', None), 'extra', None) or {}
                if source.chat_type in {'group', 'forum', 'channel'}:
                    adapter_allow = extra.get('group_allow_from')
                else:
                    adapter_allow = extra.get('allow_from')
                if adapter_allow:
                    allowed = _coerce_allow_set(adapter_allow)
                    if user_id in allowed or '*' in allowed:
                        return True
            return _auth_env('GATEWAY_ALLOW_ALL_USERS').lower() in {'true', '1', 'yes'}
        if group_chat_allowlist and source.chat_type in {'group', 'forum'} and source.chat_id:
            allowed_group_ids = {chat_id.strip() for chat_id in group_chat_allowlist.split(',') if chat_id.strip()}
            if '*' in allowed_group_ids or source.chat_id in allowed_group_ids:
                return True
        if source.platform == Platform.TELEGRAM and group_user_allowlist and (source.chat_type in {'group', 'forum'}) and source.chat_id:
            legacy_chat_ids = {v.strip() for v in group_user_allowlist.split(',') if v.strip().startswith('-')}
            if legacy_chat_ids:
                if not getattr(self, '_warned_telegram_group_users_legacy', False):
                    logger.warning('TELEGRAM_GROUP_ALLOWED_USERS contains chat-ID-shaped values (%s). Treating them as chat IDs for backward compatibility. Move chat IDs to TELEGRAM_GROUP_ALLOWED_CHATS — the _USERS var is now for sender user IDs.', ','.join(sorted(legacy_chat_ids)))
                    self._warned_telegram_group_users_legacy = True
                if source.chat_id in legacy_chat_ids:
                    return True
        allowed_ids = set()
        if platform_allowlist:
            allowed_ids.update((uid.strip() for uid in platform_allowlist.split(',') if uid.strip()))
        if group_user_allowlist:
            allowed_ids.update((uid.strip() for uid in group_user_allowlist.split(',') if uid.strip()))
        if global_allowlist:
            allowed_ids.update((uid.strip() for uid in global_allowlist.split(',') if uid.strip()))
        if '*' in allowed_ids:
            return True
        check_ids = {user_id}
        if '@' in user_id:
            check_ids.add(user_id.split('@')[0])
        if source.platform in {Platform.WHATSAPP, Platform.WHATSAPP_CLOUD}:
            normalized_allowed_ids = set()
            for allowed_id in allowed_ids:
                normalized_allowed_ids.update(_expand_whatsapp_auth_aliases(allowed_id))
            if normalized_allowed_ids:
                allowed_ids = normalized_allowed_ids
            check_ids.update(_expand_whatsapp_auth_aliases(user_id))
            normalized_user_id = _normalize_whatsapp_identifier(user_id)
            if normalized_user_id:
                check_ids.add(normalized_user_id)
        if source.platform is not None and source.platform.value == 'simplex' and source.user_name:
            check_ids.add(source.user_name)
        return bool(check_ids & allowed_ids)

    def _get_unauthorized_dm_behavior(self, platform: Optional[Platform], *, profile: Optional[str]=None) -> str:
        """Return how unauthorized DMs should be handled for a platform.

        Resolution order:
        1. Explicit per-platform ``unauthorized_dm_behavior`` in config — always wins.
        2. Email defaults to ``"ignore"`` unless explicitly opted into
           pairing. Inboxes may contain arbitrary unread human messages, so
           replying with pairing codes is not a safe platform default.
        3. Explicit global ``unauthorized_dm_behavior`` in config — wins for
           chat-shaped platforms when no per-platform override is set.
        4. When an adapter-level DM policy opts into pairing or silent drop, honor it.
        5. When an allowlist (``PLATFORM_ALLOWED_USERS``,
           ``PLATFORM_GROUP_ALLOWED_USERS`` / ``PLATFORM_GROUP_ALLOWED_CHATS``,
           or ``GATEWAY_ALLOWED_USERS``) is configured, default to ``"ignore"`` —
           the allowlist signals that the owner has deliberately restricted
           access; spamming unknown contacts with pairing codes is both noisy
           and a potential info-leak. (#9337)
        6. No allowlist and no explicit config → ``"pair"`` (open-gateway default).
        """
        config = getattr(self, 'config', None)
        if config and hasattr(config, 'get_unauthorized_dm_behavior') and platform:
            platform_cfg = config.platforms.get(platform) if hasattr(config, 'platforms') else None
            if platform_cfg and 'unauthorized_dm_behavior' in getattr(platform_cfg, 'extra', {}):
                return config.get_unauthorized_dm_behavior(platform)
        if platform == Platform.EMAIL:
            return 'ignore'
        if config and hasattr(config, 'unauthorized_dm_behavior'):
            if config.unauthorized_dm_behavior != 'pair':
                return config.unauthorized_dm_behavior
        if platform:
            dm_policy = self._adapter_dm_policy(platform, profile=profile)
            if not dm_policy and config and hasattr(config, 'platforms'):
                platform_cfg = config.platforms.get(platform)
                extra = getattr(platform_cfg, 'extra', None) if platform_cfg else None
                if isinstance(extra, dict):
                    dm_policy = str(extra.get('dm_policy') or '').strip().lower()
            if dm_policy == 'pairing':
                return 'pair'
            if dm_policy in {'allowlist', 'disabled'}:
                return 'ignore'
        if platform:
            platform_env_map = {Platform.TELEGRAM: 'TELEGRAM_ALLOWED_USERS', Platform.DISCORD: 'DISCORD_ALLOWED_USERS', Platform.WHATSAPP: 'WHATSAPP_ALLOWED_USERS', Platform.WHATSAPP_CLOUD: 'WHATSAPP_CLOUD_ALLOWED_USERS', Platform.SLACK: 'SLACK_ALLOWED_USERS', Platform.SIGNAL: 'SIGNAL_ALLOWED_USERS', Platform.EMAIL: 'EMAIL_ALLOWED_USERS', Platform.SMS: 'SMS_ALLOWED_USERS', Platform.MATTERMOST: 'MATTERMOST_ALLOWED_USERS', Platform.MATRIX: 'MATRIX_ALLOWED_USERS', Platform.DINGTALK: 'DINGTALK_ALLOWED_USERS', Platform.FEISHU: 'FEISHU_ALLOWED_USERS', Platform.WECOM: 'WECOM_ALLOWED_USERS', Platform.WECOM_CALLBACK: 'WECOM_CALLBACK_ALLOWED_USERS', Platform.WEIXIN: 'WEIXIN_ALLOWED_USERS', Platform.BLUEBUBBLES: 'BLUEBUBBLES_ALLOWED_USERS', Platform.QQBOT: 'QQ_ALLOWED_USERS'}
            platform_group_env_map = {Platform.TELEGRAM: ('TELEGRAM_GROUP_ALLOWED_USERS', 'TELEGRAM_GROUP_ALLOWED_CHATS'), Platform.QQBOT: ('QQ_GROUP_ALLOWED_USERS',)}
            if _platform_gate_env(platform_env_map.get(platform, '')).strip():
                return 'ignore'
            for env_key in platform_group_env_map.get(platform, ()):
                if _platform_gate_env(env_key).strip():
                    return 'ignore'
        if _platform_gate_env('GATEWAY_ALLOWED_USERS').strip():
            return 'ignore'
        return 'pair'