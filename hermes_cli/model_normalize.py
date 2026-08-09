"""Per-provider model name normalization.

Different LLM providers expect model identifiers in different formats:

- **Aggregators** (OpenRouter, Nous, AI Gateway, Kilo Code) need
  ``vendor/model`` slugs like ``anthropic/claude-sonnet-4.6``.
- **Anthropic** native API expects bare names with dots replaced by
  hyphens: ``claude-sonnet-4-6``.
- **Copilot** expects bare names *with* dots preserved:
  ``claude-sonnet-4.6``.
- **OpenCode Zen** preserves dots for GPT/GLM/Gemini/Kimi/MiniMax-style
  model IDs, but Claude still uses hyphenated native names like
  ``claude-sonnet-4-6``.
- **OpenCode Go** preserves dots in model names: ``minimax-m2.7``.
- **DeepSeek** accepts only the first-class V-series IDs
  (``deepseek-v4-pro``, ``deepseek-v4-flash``, and any future
  ``deepseek-v<N>-*``).  The legacy aliases ``deepseek-chat`` and
  ``deepseek-reasoner`` were retired on 2026-07-24 and are remapped to
  ``deepseek-v4-flash`` (official non-thinking / thinking shims).  Older
  Duck Agent revisions folded every non-reasoner input into
  ``deepseek-chat``, which on aggregators routes to V3 — so a user
  picking V4 Pro was silently downgraded.
- **Custom** and remaining providers pass the name through as-is.

This module centralises that translation so callers can simply write::

    api_model = normalize_model_for_provider(user_input, provider)

Inspired by Clawdbot's ``normalizeAnthropicModelId`` pattern.
"""
from __future__ import annotations
import re
from typing import Optional
_VENDOR_PREFIXES: dict[str, str] = {'claude': 'anthropic', 'gpt': 'openai', 'o1': 'openai', 'o3': 'openai', 'o4': 'openai', 'gemini': 'google', 'gemma': 'google', 'deepseek': 'deepseek', 'glm': 'z-ai', 'kimi': 'moonshotai', 'minimax': 'minimax', 'grok': 'x-ai', 'qwen': 'qwen', 'mimo': 'xiaomi', 'trinity': 'arcee-ai', 'nemotron': 'nvidia', 'llama': 'meta-llama', 'step': 'stepfun', 'trinity': 'arcee-ai'}
_AGGREGATOR_PROVIDERS: frozenset[str] = frozenset({'openrouter', 'nous', 'ai-gateway', 'kilocode'})
_DOT_TO_HYPHEN_PROVIDERS: frozenset[str] = frozenset({'anthropic'})
_STRIP_VENDOR_ONLY_PROVIDERS: frozenset[str] = frozenset({'copilot', 'copilot-acp', 'openai-codex'})
_AUTHORITATIVE_NATIVE_PROVIDERS: frozenset[str] = frozenset({'huggingface'})
_MATCHING_PREFIX_STRIP_PROVIDERS: frozenset[str] = frozenset({'zai', 'kimi-coding', 'kimi-coding-cn', 'minimax', 'minimax-oauth', 'minimax-cn', 'alibaba', 'qwen-oauth', 'xiaomi', 'arcee', 'ollama-cloud', 'custom', 'gemini', 'xai'})
_CATALOGUE_PREFIX_REPAIR_PROVIDERS: frozenset[str] = frozenset({'nvidia'})
_LOWERCASE_MODEL_PROVIDERS: frozenset[str] = frozenset({'xiaomi'})
_DEEPSEEK_REASONER_KEYWORDS: frozenset[str] = frozenset({'reasoner', 'r1', 'think', 'reasoning', 'cot'})
_DEEPSEEK_RETIRED_ALIASES: frozenset[str] = frozenset({'deepseek-chat', 'deepseek-reasoner'})
_DEEPSEEK_CANONICAL_MODELS: frozenset[str] = frozenset({'deepseek-v4-pro', 'deepseek-v4-flash'})
_DEEPSEEK_V_SERIES_RE = re.compile('^deepseek-v\\d+([-.].+)?$')

def _normalize_for_deepseek(model_name: str) -> str:
    """Map a model input to a DeepSeek-accepted identifier.

    Rules:
    - Retired aliases ``deepseek-chat`` / ``deepseek-reasoner`` (cut off
      2026-07-24) -> ``deepseek-v4-flash``.
    - Already a known canonical (``deepseek-v4-pro``/``deepseek-v4-flash``)
      -> pass through.
    - Matches the V-series pattern ``deepseek-v<digit>...`` -> pass through
      (covers future ``deepseek-v5-*`` and dated variants without a release).
    - Contains a reasoner keyword (r1, think, reasoning, cot, reasoner)
      -> ``deepseek-v4-flash``.
    - Everything else -> ``deepseek-v4-flash``.

    Args:
        model_name: The bare model name (vendor prefix already stripped).

    Returns:
        A DeepSeek-accepted model identifier.
    """
    bare = _strip_vendor_prefix(model_name).lower()
    if bare in _DEEPSEEK_RETIRED_ALIASES:
        return 'deepseek-v4-flash'
    if bare in _DEEPSEEK_CANONICAL_MODELS:
        return bare
    if _DEEPSEEK_V_SERIES_RE.match(bare):
        return bare
    for keyword in _DEEPSEEK_REASONER_KEYWORDS:
        if keyword in bare:
            return 'deepseek-v4-flash'
    return 'deepseek-v4-flash'

def _strip_vendor_prefix(model_name: str) -> str:
    """Remove a ``vendor/`` prefix if present.

    Examples::

        >>> _strip_vendor_prefix("anthropic/claude-sonnet-4.6")
        'claude-sonnet-4.6'
        >>> _strip_vendor_prefix("claude-sonnet-4.6")
        'claude-sonnet-4.6'
        >>> _strip_vendor_prefix("meta-llama/llama-4-scout")
        'llama-4-scout'
    """
    if '/' in model_name:
        return model_name.split('/', 1)[1]
    return model_name

def _dots_to_hyphens(model_name: str) -> str:
    """Replace dots with hyphens in a model name.

    Anthropic's native API uses hyphens where marketing names use dots:
    ``claude-sonnet-4.6`` -> ``claude-sonnet-4-6``.
    """
    return model_name.replace('.', '-')

def _normalize_provider_alias(provider_name: str) -> str:
    """Resolve provider aliases to Duck Agent' canonical ids."""
    raw = (provider_name or '').strip().lower()
    if not raw:
        return raw
    try:
        from hermes_cli.models import normalize_provider
        return normalize_provider(raw)
    except Exception:
        return raw

def _strip_matching_provider_prefix(model_name: str, target_provider: str) -> str:
    """Strip ``provider/`` only when the prefix matches the target provider.

    This prevents arbitrary slash-bearing model IDs from being mangled on
    native providers while still repairing manual config values like
    ``zai/glm-5.1`` for the ``zai`` provider.

    ``custom`` is a generic bucket for arbitrary user-defined endpoints, not
    a vendor identity like ``zai``/``gemini``/``xai``. An alias that merely
    *resolves to* ``custom`` (e.g. ``ollama``, via ``_PROVIDER_ALIASES``)
    does not mean a ``ollama/`` prefix is redundant -- it may be the actual
    routing prefix a proxy in front of the custom endpoint (e.g. LiteLLM)
    requires, as in ``ollama/glm-5.2``. Only a literal ``custom/`` prefix --
    the bucket's own name -- is treated as redundant here.
    """
    if '/' not in model_name:
        return model_name
    prefix, remainder = model_name.split('/', 1)
    if not prefix.strip() or not remainder.strip():
        return model_name
    normalized_target = _normalize_provider_alias(target_provider)
    if normalized_target == 'custom':
        if prefix.strip().lower() == 'custom':
            return remainder.strip()
        return model_name
    normalized_prefix = _normalize_provider_alias(prefix)
    if normalized_prefix and normalized_prefix == normalized_target:
        return remainder.strip()
    return model_name

def detect_vendor(model_name: str) -> Optional[str]:
    """Detect the vendor slug from a bare model name.

    Uses the first hyphen-delimited token of the model name to look up
    the corresponding vendor in ``_VENDOR_PREFIXES``.  Also handles
    case-insensitive matching and special patterns.

    Args:
        model_name: A model name, optionally already including a
            ``vendor/`` prefix.  If a prefix is present it is used
            directly.

    Returns:
        The vendor slug (e.g. ``"anthropic"``, ``"openai"``) or ``None``
        if no vendor can be confidently detected.

    Examples::

        >>> detect_vendor("claude-sonnet-4.6")
        'anthropic'
        >>> detect_vendor("gpt-5.4-mini")
        'openai'
        >>> detect_vendor("anthropic/claude-sonnet-4.6")
        'anthropic'
        >>> detect_vendor("my-custom-model")
    """
    name = model_name.strip()
    if not name:
        return None
    if '/' in name:
        return name.split('/', 1)[0].lower() or None
    name_lower = name.lower()
    first_token = name_lower.split('-')[0]
    if first_token in _VENDOR_PREFIXES:
        return _VENDOR_PREFIXES[first_token]
    for prefix, vendor in _VENDOR_PREFIXES.items():
        if name_lower.startswith(prefix):
            return vendor
    return None

def _prepend_vendor(model_name: str) -> str:
    """Prepend the detected ``vendor/`` prefix if missing.

    Used for aggregator providers that require ``vendor/model`` format.
    If the name already contains a ``/``, it is returned as-is.
    If no vendor can be detected, the name is returned unchanged
    (aggregators may still accept it or return an error).

    Examples::

        >>> _prepend_vendor("claude-sonnet-4.6")
        'anthropic/claude-sonnet-4.6'
        >>> _prepend_vendor("anthropic/claude-sonnet-4.6")
        'anthropic/claude-sonnet-4.6'
        >>> _prepend_vendor("my-custom-thing")
        'my-custom-thing'
    """
    if '/' in model_name:
        return model_name
    vendor = detect_vendor(model_name)
    if vendor:
        return f'{vendor}/{model_name}'
    return model_name

def _repair_prefix_from_catalogue(model_name: str, provider: str) -> str:
    """Restore a dropped ``vendor/`` prefix using the provider's catalogue.

    Unlike :func:`_prepend_vendor`, this never guesses from the model's name
    shape — it only repairs a bare id that matches **exactly one** curated
    entry for this provider modulo the prefix. That keeps self-hosted models
    behind the same provider id (local NIM containers, proxies) untouched,
    since they aren't in the catalogue.

    Examples::

        >>> _repair_prefix_from_catalogue("nemotron-3-ultra-550b-a55b", "nvidia")
        'nvidia/nemotron-3-ultra-550b-a55b'
        >>> _repair_prefix_from_catalogue("my-local-nim", "nvidia")
        'my-local-nim'
    """
    if '/' in model_name:
        return model_name
    try:
        from hermes_cli.models import _PROVIDER_MODELS
    except Exception:
        return model_name
    catalogue = _PROVIDER_MODELS.get(provider) or []
    needle = model_name.strip().lower()
    matches = {entry for entry in catalogue if '/' in entry and entry.split('/', 1)[1].strip().lower() == needle}
    if len(matches) == 1:
        return matches.pop()
    return model_name

def suggest_prefixed_model_id(provider: str, model_name: str) -> Optional[str]:
    """Return the prefixed catalogue id for a bare *model_name*, if unambiguous.

    The diagnostic counterpart to :func:`_repair_prefix_from_catalogue`: used
    to explain a provider's content-free 404 when the configured id lost its
    ``vendor/`` prefix. Returns ``None`` when the name already has a prefix,
    the provider has no curated catalogue, or nothing matches — so callers can
    stay silent rather than guess (#78796).
    """
    name = (model_name or '').strip()
    if not name or '/' in name:
        return None
    try:
        canonical = _normalize_provider_alias(provider)
    except Exception:
        return None
    repaired = _repair_prefix_from_catalogue(name, canonical)
    return repaired if repaired != name else None

def normalize_model_for_provider(model_input: str, target_provider: str) -> str:
    """Translate a model name into the format the target provider's API expects.

    This is the primary entry point for model name normalisation.  It
    accepts any user-facing model identifier and transforms it for the
    specific provider that will receive the API call.

    Args:
        model_input: The model name as provided by the user or config.
            Can be bare (``"claude-sonnet-4.6"``), vendor-prefixed
            (``"anthropic/claude-sonnet-4.6"``), or already in native
            format (``"claude-sonnet-4-6"``).
        target_provider: The canonical Duck Agent provider id, e.g.
            ``"openrouter"``, ``"anthropic"``, ``"copilot"``,
            ``"deepseek"``, ``"custom"``.  Should already be normalised
            via ``hermes_cli.models.normalize_provider()``.

    Returns:
        The model identifier string that the target provider's API
        expects.

    Raises:
        No exceptions -- always returns a best-effort string.

    Examples::

        >>> normalize_model_for_provider("claude-sonnet-4.6", "openrouter")
        'anthropic/claude-sonnet-4.6'

        >>> normalize_model_for_provider("anthropic/claude-sonnet-4.6", "anthropic")
        'claude-sonnet-4-6'

        >>> normalize_model_for_provider("anthropic/claude-sonnet-4.6", "copilot")
        'claude-sonnet-4.6'

        >>> normalize_model_for_provider("openai/gpt-5.4", "copilot")
        'gpt-5.4'

        >>> normalize_model_for_provider("claude-sonnet-4.6", "opencode-zen")
        'claude-sonnet-4-6'

        >>> normalize_model_for_provider("minimax-m2.5-free", "opencode-zen")
        'minimax-m2.5-free'

        >>> normalize_model_for_provider("deepseek-v3", "deepseek")
        'deepseek-v4-flash'

        >>> normalize_model_for_provider("deepseek-r1", "deepseek")
        'deepseek-v4-flash'

        >>> normalize_model_for_provider("deepseek-reasoner", "deepseek")
        'deepseek-v4-flash'

        >>> normalize_model_for_provider("my-model", "custom")
        'my-model'

        >>> normalize_model_for_provider("claude-sonnet-4.6", "zai")
        'claude-sonnet-4.6'

        >>> normalize_model_for_provider("MiMo-V2.5-Pro", "xiaomi")
        'mimo-v2.5-pro'
    """
    name = (model_input or '').strip()
    if not name:
        return name
    provider = _normalize_provider_alias(target_provider)
    if provider in _AGGREGATOR_PROVIDERS:
        return _prepend_vendor(name)
    if provider in {'opencode-zen', 'opencode-go'}:
        if '/' in name:
            _, bare_after_slash = name.split('/', 1)
            name = bare_after_slash.strip() or name
        if provider == 'opencode-zen' and name.lower().startswith('claude-'):
            return _dots_to_hyphens(name)
        return name
    if provider in _DOT_TO_HYPHEN_PROVIDERS:
        bare = _strip_matching_provider_prefix(name, provider)
        if '/' in bare:
            return bare
        return _dots_to_hyphens(bare)
    if provider in {'copilot', 'copilot-acp'}:
        try:
            from hermes_cli.models import normalize_copilot_model_id
            normalized = normalize_copilot_model_id(name)
            if normalized:
                return normalized
        except Exception:
            pass
    if provider in _STRIP_VENDOR_ONLY_PROVIDERS:
        stripped = _strip_matching_provider_prefix(name, provider)
        if stripped == name and name.startswith('openai/'):
            return name.split('/', 1)[1]
        return stripped
    if provider == 'deepseek':
        bare = _strip_matching_provider_prefix(name, provider)
        if '/' in bare:
            return bare
        return _normalize_for_deepseek(bare)
    if provider in _MATCHING_PREFIX_STRIP_PROVIDERS:
        result = _strip_matching_provider_prefix(name, provider)
        if provider in _LOWERCASE_MODEL_PROVIDERS:
            result = result.lower()
        return result
    if provider in _CATALOGUE_PREFIX_REPAIR_PROVIDERS:
        return _repair_prefix_from_catalogue(name, provider)
    if provider in _AUTHORITATIVE_NATIVE_PROVIDERS:
        return name
    return name