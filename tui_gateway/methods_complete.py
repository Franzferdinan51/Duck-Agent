"""Completion / model-key / paste JSON-RPC handlers (moved verbatim from server.py).

Handler bodies are byte-identical to their pre-split server.py form; they
are rebound onto server.py's globals at install time — see method_ctx.py.
"""
from .method_ctx import HandlerRegistry
_registry = HandlerRegistry()
method = _registry.method
_profile_scoped = _registry.profile_scoped

@method('paste.collapse')
def _(rid, params: dict) -> dict:
    global _paste_counter
    text = params.get('text', '')
    if not text:
        return _err(rid, 4004, 'empty paste')
    _paste_counter += 1
    line_count = text.count('\n') + 1
    paste_dir = _hermes_home / 'pastes'
    paste_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime
    paste_file = paste_dir / f"paste_{_paste_counter}_{datetime.now().strftime('%H%M%S')}.txt"
    paste_file.write_text(text, encoding='utf-8')
    placeholder = f'[Pasted text #{_paste_counter}: {line_count} lines → {paste_file}]'
    return _ok(rid, {'placeholder': placeholder, 'path': str(paste_file), 'lines': line_count})

@method('complete.path')
def _(rid, params: dict) -> dict:
    word = params.get('word', '')
    if not word:
        return _ok(rid, {'items': []})
    items: list[dict] = []
    try:
        root = _completion_cwd(params)
        is_context = word.startswith('@')
        query = word[1:] if is_context else word
        if is_context and (not query):
            items = [{'text': '@diff', 'display': '@diff', 'meta': 'git diff'}, {'text': '@staged', 'display': '@staged', 'meta': 'staged diff'}, {'text': '@file:', 'display': '@file:', 'meta': 'attach file'}, {'text': '@folder:', 'display': '@folder:', 'meta': 'attach folder'}, {'text': '@url:', 'display': '@url:', 'meta': 'fetch url'}, {'text': '@git:', 'display': '@git:', 'meta': 'git log'}]
            return _ok(rid, {'items': items})
        if is_context and query in {'file', 'folder'}:
            prefix_tag, path_part = (query, '')
        elif is_context and query.startswith(('file:', 'folder:')):
            prefix_tag, _, tail = query.partition(':')
            path_part = tail
        else:
            prefix_tag = ''
            path_part = query if is_context else query
        if is_context and path_part.startswith('/') and (not path_part.startswith('//')) and (not _abs_completion_prefix_exists(path_part)):
            path_part = path_part.lstrip('/')
        if is_context and path_part and (len(path_part.strip()) >= 2) and ('/' not in path_part) and (prefix_tag != 'folder'):
            ranked: list[tuple[tuple[int, int], str, str, bool]] = []
            walked_dirs: set[str] = set()
            seen: set[str] = set()
            want_hidden = path_part.startswith('.')

            def _consider(rel: str, name: str, is_dir: bool) -> None:
                if rel in seen or (name.startswith('.') and (not want_hidden)):
                    return
                rank = _fuzzy_basename_rank(name, path_part)
                if rank is not None:
                    seen.add(rel)
                    ranked.append((rank, rel, name, is_dir))
            try:
                for entry in os.listdir(root):
                    if entry not in _FUZZY_FALLBACK_EXCLUDES:
                        _consider(entry, entry, os.path.isdir(os.path.join(root, entry)))
            except OSError:
                pass
            for rel in _list_repo_files(root):
                _consider(rel, os.path.basename(rel), False)
                parent = os.path.dirname(rel)
                while parent and parent not in walked_dirs:
                    walked_dirs.add(parent)
                    _consider(parent, os.path.basename(parent), True)
                    parent = os.path.dirname(parent)
            ranked.sort(key=lambda r: (r[0], not r[3], len(r[1]), r[1]))
            tag = prefix_tag or 'file'
            for _, rel, basename, is_dir in ranked[:30]:
                items.append({'text': f"@{('folder' if is_dir else tag)}:{rel}{('/' if is_dir else '')}", 'display': basename + ('/' if is_dir else ''), 'meta': 'dir' if is_dir else os.path.dirname(rel)})
            return _ok(rid, {'items': items})
        expanded = _normalize_completion_path(path_part) if path_part else '.'
        if expanded == '.' or not expanded:
            search_dir, match = ('.', '')
        elif expanded.endswith('/'):
            search_dir, match = (expanded, '')
        else:
            search_dir = os.path.dirname(expanded) or '.'
            match = os.path.basename(expanded)
        search_dir = search_dir if os.path.isabs(search_dir) else os.path.join(root, search_dir)
        if not os.path.isdir(search_dir):
            return _ok(rid, {'items': []})
        want_dir = prefix_tag == 'folder'
        match_lower = match.lower()
        for entry in sorted(os.listdir(search_dir)):
            if match and (not entry.lower().startswith(match_lower)):
                continue
            if is_context and entry in _FUZZY_FALLBACK_EXCLUDES:
                continue
            if is_context and (not prefix_tag) and entry.startswith('.'):
                continue
            full = os.path.join(search_dir, entry)
            is_dir = os.path.isdir(full)
            if prefix_tag and want_dir != is_dir:
                continue
            rel = os.path.relpath(full, root).replace(os.sep, '/')
            suffix = '/' if is_dir else ''
            if is_context and prefix_tag:
                text = f'@{prefix_tag}:{rel}{suffix}'
            elif is_context:
                kind = 'folder' if is_dir else 'file'
                text = f'@{kind}:{rel}{suffix}'
            elif word.startswith('~'):
                text = '~/' + os.path.relpath(full, os.path.expanduser('~')) + suffix
            elif word.startswith('./'):
                text = './' + rel + suffix
            else:
                text = rel + suffix
            items.append({'text': text, 'display': entry + suffix, 'meta': 'dir' if is_dir else ''})
            if len(items) >= 30:
                break
    except Exception as e:
        return _err(rid, 5021, str(e))
    return _ok(rid, {'items': items})

@method('complete.slash')
def _(rid, params: dict) -> dict:
    text = params.get('text', '')
    if not text.startswith('/'):
        return _ok(rid, {'items': []})
    try:
        from hermes_cli.commands import SlashCommandCompleter
        from prompt_toolkit.document import Document
        from prompt_toolkit.formatted_text import to_plain_text
        from agent.skill_commands import get_skill_commands
        from agent.skill_bundles import get_skill_bundles
        completer = SlashCommandCompleter(skill_commands_provider=lambda: get_skill_commands(), skill_bundles_provider=lambda: get_skill_bundles())
        doc = Document(text, len(text))
        skill_names = {key.lstrip('/').lower() for key in (*get_skill_commands(), *get_skill_bundles())}
        items = [{'text': c.text, 'display': to_plain_text(c.display) if c.display else c.text, 'meta': to_plain_text(c.display_meta) if c.display_meta else '', 'kind': 'skill' if c.text.strip().lstrip('/').lower() in skill_names else 'command'} for c in completer.get_completions(doc, None)]
        if text.rsplit(' ', 1)[-1].startswith('/'):
            usage, origin_of = _skill_usage_lookup()
            items = _rank_slash_completions(items, usage, origin_of, browsing=text == '/')
        else:
            items = items[:_SLASH_COMPLETION_LIMIT]
        text_lower = text.lower()
        extras = [{'text': '/density', 'display': '/density', 'meta': 'Toggle compact display mode', 'kind': 'command'}, {'text': '/details', 'display': '/details', 'meta': 'Control agent detail visibility', 'kind': 'command'}, {'text': '/logs', 'display': '/logs', 'meta': 'Show recent gateway log lines', 'kind': 'command'}, {'text': '/mouse', 'display': '/mouse', 'meta': 'Set mouse tracking preset [on|off|toggle|wheel|buttons|all]', 'kind': 'command'}]
        for extra in extras:
            if extra['text'].startswith(text_lower) and (not any((item['text'] == extra['text'] for item in items))):
                items.append(extra)
        details_items = _details_completions(text)
        if details_items is not None:
            return _ok(rid, {'items': details_items, 'replace_from': text.rfind(' ') + 1 if ' ' in text else len(text)})
        return _ok(rid, {'items': items, 'replace_from': text.rfind(' ') + 1 if ' ' in text else 1})
    except Exception as e:
        return _err(rid, 5020, str(e))

@method('model.options')
def _(rid, params: dict) -> dict:
    try:
        from hermes_cli.inventory import build_model_options_payload
        session = _sessions.get(params.get('session_id', ''))
        agent = session.get('agent') if session else None
        ctx = _model_picker_context(agent)
        payload = build_model_options_payload(ctx, explicit_only=bool(params.get('explicit_only')), include_unconfigured=bool(params.get('include_unconfigured')), refresh=bool(params.get('refresh')))
        return _ok(rid, payload)
    except Exception as e:
        return _err(rid, 5033, str(e))

@method('model.save_key')
def _(rid, params: dict) -> dict:
    """Save an API key for a provider, then return its refreshed model list.

    Params:
        slug: provider slug (e.g. "deepseek", "xai")
        api_key: the key value to save

    Returns the provider dict with models populated (same shape as
    model.options entries) on success.
    """
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY
        from hermes_cli.config import is_managed
        from hermes_cli.inventory import build_models_payload
        slug = (params.get('slug') or '').strip()
        api_key = (params.get('api_key') or '').strip()
        if not slug or not api_key:
            return _err(rid, 4001, 'slug and api_key are required')
        if is_managed():
            return _err(rid, 4006, 'managed install — credentials are read-only')
        pconfig = PROVIDER_REGISTRY.get(slug)
        if not pconfig:
            return _err(rid, 4002, f'unknown provider: {slug}')
        if pconfig.auth_type != 'api_key':
            return _err(rid, 4003, f'{pconfig.name} uses {pconfig.auth_type} auth — run `duck-agent model` to configure')
        if not pconfig.api_key_env_vars:
            return _err(rid, 4004, f'no env var defined for {pconfig.name}')
        env_var = pconfig.api_key_env_vars[0]
        from hermes_cli.credential_lifecycle import save_provider_env_credential
        save_provider_env_credential(env_var, api_key)
        import os
        os.environ[env_var] = api_key
        session = _sessions.get(params.get('session_id', ''))
        agent = session.get('agent') if session else None
        ctx = _model_picker_context(agent)
        payload = build_models_payload(ctx, picker_hints=True, max_models=50)
        provider_data = next((p for p in payload['providers'] if p['slug'] == slug), None)
        if provider_data is None:
            provider_data = {'slug': slug, 'name': pconfig.name, 'is_current': False, 'models': [], 'total_models': 0, 'authenticated': True}
        provider_data['authenticated'] = True
        return _ok(rid, {'provider': provider_data})
    except Exception as e:
        return _err(rid, 5034, str(e))

@method('model.disconnect')
def _(rid, params: dict) -> dict:
    """Remove credentials for a provider.

    Params:
        slug: provider slug (e.g. "deepseek", "xai")

    Returns success status and the provider's slug.
    """
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY, clear_provider_auth
        from hermes_cli.credential_lifecycle import remove_provider_env_credential
        slug = (params.get('slug') or '').strip()
        if not slug:
            return _err(rid, 4001, 'slug is required')
        pconfig = PROVIDER_REGISTRY.get(slug)
        cleared_env = False
        cleared_auth = False
        if pconfig and pconfig.api_key_env_vars:
            for ev in pconfig.api_key_env_vars:
                if remove_provider_env_credential(ev).get('found'):
                    cleared_env = True
        cleared_auth = clear_provider_auth(slug)
        if not cleared_env and (not cleared_auth):
            return _err(rid, 4005, f'no credentials found for {slug}')
        provider_name = pconfig.name if pconfig else slug
        return _ok(rid, {'slug': slug, 'name': provider_name, 'disconnected': True})
    except Exception as e:
        return _err(rid, 5035, str(e))

def register(server) -> None:
    """Bind this module's handlers onto ``server``'s globals and registry."""
    _registry.install(server)