"""Codex model discovery from API, local cache, and config."""
from __future__ import annotations
import base64
import json
import logging
from pathlib import Path
from typing import List, Optional
import os
logger = logging.getLogger(__name__)
DEFAULT_CODEX_MODELS: List[str] = ['gpt-5.6-sol', 'gpt-5.6-sol-pro', 'gpt-5.6-terra', 'gpt-5.6-terra-pro', 'gpt-5.6-luna', 'gpt-5.6-luna-pro', 'gpt-5.5', 'gpt-5.4-mini', 'gpt-5.4', 'gpt-5.3-codex', 'gpt-5.3-codex-spark']
_FORWARD_COMPAT_TEMPLATE_MODELS: List[tuple[str, tuple[str, ...]]] = [('gpt-5.6-sol', ('gpt-5.5', 'gpt-5.4')), ('gpt-5.6-sol-pro', ('gpt-5.5', 'gpt-5.4')), ('gpt-5.6-terra', ('gpt-5.5', 'gpt-5.4')), ('gpt-5.6-terra-pro', ('gpt-5.5', 'gpt-5.4')), ('gpt-5.6-luna', ('gpt-5.5', 'gpt-5.4')), ('gpt-5.6-luna-pro', ('gpt-5.5', 'gpt-5.4')), ('gpt-5.5', ('gpt-5.4', 'gpt-5.4-mini', 'gpt-5.3-codex')), ('gpt-5.4-mini', ('gpt-5.3-codex',)), ('gpt-5.4', ('gpt-5.3-codex',)), ('gpt-5.3-codex-spark', ('gpt-5.3-codex',))]

def _add_forward_compat_models(model_ids: List[str]) -> List[str]:
    """Add Clawdbot-style synthetic forward-compat Codex models.

    If a newer Codex slug isn't returned by live discovery, surface it when an
    older compatible template model is present. This mirrors Clawdbot's
    synthetic catalog / forward-compat behavior for GPT-5 Codex variants.
    """
    ordered: List[str] = []
    seen: set[str] = set()
    for model_id in model_ids:
        if model_id not in seen:
            ordered.append(model_id)
            seen.add(model_id)
    for synthetic_model, template_models in _FORWARD_COMPAT_TEMPLATE_MODELS:
        if synthetic_model in seen:
            continue
        if any((template in seen for template in template_models)):
            ordered.append(synthetic_model)
            seen.add(synthetic_model)
    return ordered

def _extract_chatgpt_account_id(access_token: str) -> Optional[str]:
    """Best-effort extraction of ``chatgpt_account_id`` from the OAuth JWT.

    The Codex backend requires the ``ChatGPT-Account-Id`` header for the
    per-account catalog. Without it, ``GET /backend-api/codex/models``
    returns ``{"models":[]}`` (HTTP 200) — which masquerades as "no
    models available" and silently degrades the picker to the curated
    fallback list. The request-side path in ``auxiliary_client.py``
    already extracts the same claim; this mirrors that logic here so the
    probe sees the same catalog the request path will actually use.

    Returns ``None`` on any parse error — the probe then degrades
    gracefully to the unauthenticated fallback list instead of crashing.
    """
    try:
        parts = access_token.split('.')
        if len(parts) < 2:
            return None
        payload_b64 = parts[1] + '=' * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        acct_id = claims.get('https://api.openai.com/auth', {}).get('chatgpt_account_id') if isinstance(claims, dict) else None
        return acct_id if isinstance(acct_id, str) and acct_id else None
    except Exception:
        return None

def _fetch_models_from_api(access_token: str) -> List[str]:
    """Fetch available models from the Codex API. Returns visible models sorted by priority."""
    try:
        import httpx
        headers = {'Authorization': f'Bearer {access_token}'}
        acct_id = _extract_chatgpt_account_id(access_token)
        if acct_id:
            headers['ChatGPT-Account-Id'] = acct_id
        resp = httpx.get('https://chatgpt.com/backend-api/codex/models?client_version=1.0.0', headers=headers, timeout=10)
        if resp.status_code != 200:
            return []
        data = resp.json()
        entries = data.get('models', []) if isinstance(data, dict) else []
    except Exception as exc:
        logger.debug('Failed to fetch Codex models from API: %s', exc)
        return []
    sortable = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        slug = item.get('slug')
        if not isinstance(slug, str) or not slug.strip():
            continue
        slug = slug.strip()
        visibility = item.get('visibility', '')
        if isinstance(visibility, str) and visibility.strip().lower() in {'hide', 'hidden'}:
            continue
        priority = item.get('priority')
        rank = int(priority) if isinstance(priority, (int, float)) else 10000
        sortable.append((rank, slug))
    sortable.sort(key=lambda x: (x[0], x[1]))
    return _add_forward_compat_models([slug for _, slug in sortable])

def _read_default_model(codex_home: Path) -> Optional[str]:
    config_path = codex_home / 'config.toml'
    if not config_path.exists():
        return None
    try:
        import tomllib
    except Exception:
        return None
    try:
        payload = tomllib.loads(config_path.read_text(encoding='utf-8'))
    except Exception:
        return None
    model = payload.get('model') if isinstance(payload, dict) else None
    if isinstance(model, str) and model.strip():
        return model.strip()
    return None

def _read_cache_models(codex_home: Path) -> List[str]:
    cache_path = codex_home / 'models_cache.json'
    if not cache_path.exists():
        return []
    try:
        raw = json.loads(cache_path.read_text(encoding='utf-8'))
    except Exception:
        return []
    entries = raw.get('models') if isinstance(raw, dict) else None
    sortable = []
    if isinstance(entries, list):
        for item in entries:
            if not isinstance(item, dict):
                continue
            slug = item.get('slug')
            if not isinstance(slug, str) or not slug.strip():
                continue
            slug = slug.strip()
            visibility = item.get('visibility')
            if isinstance(visibility, str) and visibility.strip().lower() in {'hide', 'hidden'}:
                continue
            priority = item.get('priority')
            rank = int(priority) if isinstance(priority, (int, float)) else 10000
            sortable.append((rank, slug))
    sortable.sort(key=lambda item: (item[0], item[1]))
    deduped: List[str] = []
    for _, slug in sortable:
        if slug not in deduped:
            deduped.append(slug)
    return deduped

def get_codex_model_ids(access_token: Optional[str]=None) -> List[str]:
    """Return available Codex model IDs, trying API first, then local sources.
    
    Resolution order: API (live, if token provided) > config.toml default >
    local cache > hardcoded defaults.
    """
    codex_home_str = os.getenv('CODEX_HOME', '').strip() or str(Path.home() / '.codex')
    codex_home = Path(codex_home_str).expanduser()
    ordered: List[str] = []
    if access_token:
        api_models = _fetch_models_from_api(access_token)
        if api_models:
            return _add_forward_compat_models(api_models)
    default_model = _read_default_model(codex_home)
    if default_model:
        ordered.append(default_model)
    for model_id in _read_cache_models(codex_home):
        if model_id not in ordered:
            ordered.append(model_id)
    for model_id in DEFAULT_CODEX_MODELS:
        if model_id not in ordered:
            ordered.append(model_id)
    return _add_forward_compat_models(ordered)