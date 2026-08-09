"""
Platform Adapter Registry

Allows platform adapters (built-in and plugin) to self-register so the gateway
can discover and instantiate them without hardcoded if/elif chains.

Built-in adapters continue to use the existing if/elif in _create_adapter()
for now.  Plugin adapters register here via PluginContext.register_platform()
and are looked up first -- if nothing is found the gateway falls through to
the legacy code path.

Usage (plugin side):

    from gateway.platform_registry import platform_registry, PlatformEntry

    platform_registry.register(PlatformEntry(
        name="irc",
        label="IRC",
        adapter_factory=lambda cfg: IRCAdapter(cfg),
        check_fn=check_requirements,
        validate_config=lambda cfg: bool(cfg.extra.get("server")),
        required_env=["IRC_SERVER"],
        install_hint="pip install irc",
    ))

Usage (gateway side):

    adapter = platform_registry.create_adapter("irc", platform_config)
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional
logger = logging.getLogger(__name__)

@dataclass
class PlatformEntry:
    """Metadata and factory for a single platform adapter."""
    name: str
    label: str
    adapter_factory: Callable[[Any], Any]
    check_fn: Callable[[], bool]
    validate_config: Optional[Callable[[Any], bool]] = None
    ensure_deps_fn: Optional[Callable[[], bool]] = None
    is_connected: Optional[Callable[[Any], bool]] = None
    required_env: list = field(default_factory=list)
    install_hint: str = ''
    setup_fn: Optional[Callable[[], None]] = None
    source: str = 'plugin'
    plugin_name: str = ''
    allowed_users_env: str = ''
    allow_all_env: str = ''
    max_message_length: int = 0
    pii_safe: bool = False
    emoji: str = '🔌'
    allow_update_command: bool = True
    platform_hint: str = ''
    env_enablement_fn: Optional[Callable[[], Optional[dict]]] = None
    apply_yaml_config_fn: Optional[Callable[[dict, dict], Optional[dict]]] = None
    cron_deliver_env_var: str = ''
    standalone_sender_fn: Optional[Callable[..., Awaitable[dict]]] = None

class PlatformRegistry:
    """Central registry of platform adapters.

    Thread-safe for reads (dict lookups are atomic under GIL).
    Writes happen at startup during sequential discovery.
    """

    def __init__(self) -> None:
        self._entries: dict[str, PlatformEntry] = {}
        self._deferred: dict[str, Callable[[], None]] = {}

    def register_deferred(self, name: str, loader: Callable[[], None]) -> None:
        """Register a lazy loader for a platform that hasn't been imported yet.

        *loader* is a zero-arg callable that imports the owning plugin module,
        which is expected to call :meth:`register` with the real entry for
        *name*.  The loader runs at most once, the first time *name* is looked
        up (or when the full entry list is materialized).  A real entry that is
        registered directly (e.g. a built-in) takes precedence -- the deferred
        loader is then dropped.
        """
        if name in self._entries:
            return
        self._deferred[name] = loader

    def _resolve(self, name: str) -> None:
        """Run the deferred loader for *name* if one is pending."""
        loader = self._deferred.pop(name, None)
        if loader is None:
            return
        try:
            loader()
        except Exception as e:
            logger.warning("Deferred load of platform '%s' failed: %s", name, e, exc_info=True)

    def _resolve_all(self) -> None:
        """Run every pending deferred loader.

        Used by the iterate-all accessors (``all_entries``/``plugin_entries``),
        which are only called by paths that genuinely need every adapter:
        gateway startup, ``duck-agent setup``/``gateway status``, channel
        directory.  CLI chat never iterates the full set.
        """
        if not self._deferred:
            return
        for name in list(self._deferred):
            self._resolve(name)

    def register(self, entry: PlatformEntry) -> None:
        """Register a platform adapter entry.

        If an entry with the same name exists, it is replaced (last writer
        wins -- this lets plugins override built-in adapters if desired).
        """
        self._deferred.pop(entry.name, None)
        if entry.name in self._entries:
            prev = self._entries[entry.name]
            logger.info("Platform '%s' re-registered (was %s, now %s)", entry.name, prev.source, entry.source)
        self._entries[entry.name] = entry
        logger.debug('Registered platform adapter: %s (%s)', entry.name, entry.source)

    def unregister(self, name: str) -> bool:
        """Remove a platform entry.  Returns True if it existed."""
        self._deferred.pop(name, None)
        return self._entries.pop(name, None) is not None

    def get(self, name: str) -> Optional[PlatformEntry]:
        """Look up a platform entry by name."""
        if name not in self._entries:
            self._resolve(name)
        return self._entries.get(name)

    def all_entries(self) -> list[PlatformEntry]:
        """Return all registered platform entries."""
        self._resolve_all()
        return list(self._entries.values())

    def plugin_entries(self) -> list[PlatformEntry]:
        """Return only plugin-registered platform entries."""
        self._resolve_all()
        return [e for e in self._entries.values() if e.source == 'plugin']

    def is_registered(self, name: str) -> bool:
        return name in self._entries or name in self._deferred

    def create_adapter(self, name: str, config: Any) -> Optional[Any]:
        """Create an adapter instance for the given platform name.

        Returns None if:
        - No entry registered for *name*
        - check_fn() returns False and deps can't be installed
          (no ensure_deps_fn, or ensure_deps_fn() returned False)
        - validate_config() returns False (misconfigured)
        - The factory raises an exception
        """
        if name not in self._entries:
            self._resolve(name)
        entry = self._entries.get(name)
        if entry is None:
            return None
        deps_ok = False
        try:
            deps_ok = bool(entry.check_fn())
        except Exception as e:
            logger.warning("Platform '%s' check_fn raised: %s", entry.label, e)
        if not deps_ok and entry.ensure_deps_fn is not None:
            logger.info("Platform '%s' dependencies missing — attempting install...", entry.label)
            try:
                deps_ok = bool(entry.ensure_deps_fn())
            except Exception as e:
                logger.warning("Platform '%s' dependency install raised: %s", entry.label, e)
                deps_ok = False
        if not deps_ok:
            hint = f' ({entry.install_hint})' if entry.install_hint else ''
            logger.warning("Platform '%s' requirements not met%s", entry.label, hint)
            return None
        if entry.validate_config is not None:
            try:
                if not entry.validate_config(config):
                    logger.warning("Platform '%s' config validation failed", entry.label)
                    return None
            except Exception as e:
                logger.warning("Platform '%s' config validation error: %s", entry.label, e)
                return None
        try:
            adapter = entry.adapter_factory(config)
            return adapter
        except Exception as e:
            logger.error("Failed to create adapter for platform '%s': %s", entry.label, e, exc_info=True)
            return None
platform_registry = PlatformRegistry()