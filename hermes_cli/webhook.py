"""duck-agent webhook — manage dynamic webhook subscriptions from the CLI.

Usage:
    duck-agent webhook subscribe <name> [options]
    duck-agent webhook list
    duck-agent webhook remove <name>
    duck-agent webhook test <name> [--payload '{"key": "value"}']

Subscriptions persist to ~/.duck-agent/webhook_subscriptions.json and are
hot-reloaded by the webhook adapter without a gateway restart.
"""
import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from typing import Dict
from hermes_constants import display_hermes_home
from utils import atomic_replace
from hermes_cli.config import cfg_get
_SUBSCRIPTIONS_FILENAME = 'webhook_subscriptions.json'
_SUBSCRIPTIONS_FILE_MODE = 384

def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home()

def _subscriptions_path() -> Path:
    return _hermes_home() / _SUBSCRIPTIONS_FILENAME

def _load_subscriptions() -> Dict[str, dict]:
    path = _subscriptions_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _save_subscriptions(subs: Dict[str, dict]) -> None:
    path = _subscriptions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent, text=True)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(subs, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, _SUBSCRIPTIONS_FILE_MODE)
        atomic_replace(tmp_path, path)
        os.chmod(path, _SUBSCRIPTIONS_FILE_MODE)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

def _get_webhook_config() -> dict:
    """Load webhook platform config. Returns {} if not configured."""
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        return cfg_get(cfg, 'platforms', 'webhook', default={})
    except Exception:
        return {}

def _is_webhook_enabled() -> bool:
    return bool(_get_webhook_config().get('enabled'))

def _get_webhook_base_url() -> str:
    wh = _get_webhook_config().get('extra', {})
    host = wh.get('host')
    port = wh.get('port', 8644)
    display_host = 'localhost' if not host or host in {'0.0.0.0', '::'} else host
    if ':' in display_host and (not display_host.startswith('[')):
        display_host = f'[{display_host}]'
    return f'http://{display_host}:{port}'

def _setup_hint() -> str:
    _dhh = display_hermes_home()
    return f'\n  Webhook platform is not enabled. To set it up:\n\n  1. Run the gateway setup wizard:\n     duck-agent gateway setup\n\n  2. Or manually add to {_dhh}/config.yaml:\n     platforms:\n       webhook:\n         enabled: true\n         extra:\n           port: 8644\n           secret: "your-global-hmac-secret"\n\n  3. Or set environment variables in {_dhh}/.env:\n     WEBHOOK_ENABLED=true\n     WEBHOOK_PORT=8644\n     WEBHOOK_SECRET=your-global-secret\n\n  Then start the gateway: duck-agent gateway run\n'

def _require_webhook_enabled() -> bool:
    """Check webhook is enabled. Print setup guide and return False if not."""
    if _is_webhook_enabled():
        return True
    print(_setup_hint())
    return False

def webhook_command(args):
    """Entry point for 'duck-agent webhook' subcommand."""
    sub = getattr(args, 'webhook_action', None)
    if not sub:
        print('Usage: duck-agent webhook {subscribe|list|remove|test}')
        print("Run 'duck-agent webhook --help' for details.")
        return
    if not _require_webhook_enabled():
        return
    if sub in {'subscribe', 'add'}:
        _cmd_subscribe(args)
    elif sub in {'list', 'ls'}:
        _cmd_list(args)
    elif sub in {'remove', 'rm'}:
        _cmd_remove(args)
    elif sub == 'test':
        _cmd_test(args)

def _cmd_subscribe(args):
    name = args.name.strip().lower().replace(' ', '-')
    if not re.match('^[a-z0-9][a-z0-9_-]*$', name):
        print(f"Error: Invalid name '{name}'. Use lowercase alphanumeric with hyphens/underscores.")
        return
    subs = _load_subscriptions()
    is_update = name in subs
    secret = args.secret or secrets.token_urlsafe(32)
    events = [e.strip() for e in args.events.split(',')] if args.events else []
    route = {'description': args.description or f'Agent-created subscription: {name}', 'events': events, 'secret': secret, 'prompt': args.prompt or '', 'skills': [s.strip() for s in args.skills.split(',')] if args.skills else [], 'deliver': args.deliver or 'log', 'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    if getattr(args, 'deliver_only', False):
        if route['deliver'] == 'log':
            print("Error: --deliver-only requires --deliver to be a real target (telegram, discord, slack, github_comment, etc.) — not 'log'.")
            return
        route['deliver_only'] = True
    script = getattr(args, 'script', '') or ''
    if script.strip():
        route['script'] = script.strip()
    if args.deliver_chat_id:
        route['deliver_extra'] = {'chat_id': args.deliver_chat_id}
    subs[name] = route
    _save_subscriptions(subs)
    base_url = _get_webhook_base_url()
    status = 'Updated' if is_update else 'Created'
    print(f'\n  {status} webhook subscription: {name}')
    print(f'  URL:    {base_url}/webhooks/{name}')
    print(f'  Secret: {secret}')
    if events:
        print(f"  Events: {', '.join(events)}")
    else:
        print('  Events: (all)')
    print(f"  Deliver: {route['deliver']}")
    if route.get('deliver_only'):
        print('  Mode: direct delivery (no agent, zero LLM cost)')
    if route.get('prompt'):
        prompt_preview = route['prompt'][:80] + ('...' if len(route['prompt']) > 80 else '')
        label = 'Message' if route.get('deliver_only') else 'Prompt'
        print(f'  {label}: {prompt_preview}')
    if route.get('script'):
        print(f"  Script: {route['script']}")
    print('\n  Configure your service to POST to the URL above.')
    print('  Use the secret for HMAC-SHA256 signature validation.')
    print('  The gateway must be running to receive events (duck-agent gateway run).\n')

def _cmd_list(args):
    subs = _load_subscriptions()
    if not subs:
        print('  No dynamic webhook subscriptions.')
        print('  Create one with: duck-agent webhook subscribe <name>')
        return
    base_url = _get_webhook_base_url()
    print(f'\n  {len(subs)} webhook subscription(s):\n')
    for name, route in subs.items():
        events = ', '.join(route.get('events', [])) or '(all)'
        deliver = route.get('deliver', 'log')
        if route.get('deliver_only'):
            deliver = f'{deliver} (direct — no agent)'
        desc = route.get('description', '')
        print(f'  ◆ {name}')
        if desc:
            print(f'    {desc}')
        print(f'    URL:     {base_url}/webhooks/{name}')
        print(f'    Events:  {events}')
        print(f'    Deliver: {deliver}')
        if route.get('script'):
            print(f"    Script:  {route['script']}")
        print()

def _cmd_remove(args):
    name = args.name.strip().lower()
    subs = _load_subscriptions()
    if name not in subs:
        print(f"  No subscription named '{name}'.")
        print('  Note: Static routes from config.yaml cannot be removed here.')
        return
    del subs[name]
    _save_subscriptions(subs)
    print(f'  Removed webhook subscription: {name}')

def _cmd_test(args):
    """Send a test POST to a webhook route."""
    name = args.name.strip().lower()
    subs = _load_subscriptions()
    if name not in subs:
        print(f"  No subscription named '{name}'.")
        return
    route = subs[name]
    secret = route.get('secret', '')
    base_url = _get_webhook_base_url()
    url = f'{base_url}/webhooks/{name}'
    payload = args.payload or '{"test": true, "event_type": "test", "message": "Hello from duck-agent webhook test"}'
    import hmac
    import hashlib
    sig = 'sha256=' + hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    print(f'  Sending test POST to {url}')
    try:
        import urllib.request
        req = urllib.request.Request(url, data=payload.encode(), headers={'Content-Type': 'application/json', 'X-Hub-Signature-256': sig, 'X-GitHub-Event': 'test'}, method='POST')
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            print(f'  Response ({resp.status}): {body}')
    except Exception as e:
        print(f'  Error: {e}')
        print('  Is the gateway running? (duck-agent gateway run)')