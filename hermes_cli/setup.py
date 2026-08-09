"""
Interactive setup wizard for Duck Agent.

Modular wizard with independently-runnable sections:
  1. Model & Provider — choose your AI provider and model
  2. Terminal Backend — where your agent runs commands
  3. Agent Settings — iterations, compression, session reset
  4. Messaging Platforms — connect Telegram, Discord, etc.
  5. Tools — configure TTS, web search, image generation, etc.

Config files are stored in ~/.duck-agent/ for easy access.
"""
import importlib.util
import json
import logging
import os
import re
import shutil
import sys
import copy
from pathlib import Path
from typing import Optional, Dict, Any
from hermes_cli.nous_subscription import get_nous_subscription_features
from tools.tool_backend_helpers import managed_nous_tools_enabled
from hermes_constants import get_optional_skills_dir
logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
_DOCS_BASE = 'https://duck-agent.nousresearch.com/docs'

def _model_config_dict(config: Dict[str, Any]) -> Dict[str, Any]:
    current_model = config.get('model')
    if isinstance(current_model, dict):
        return dict(current_model)
    if isinstance(current_model, str) and current_model.strip():
        return {'default': current_model.strip()}
    return {}

def _get_credential_pool_strategies(config: Dict[str, Any]) -> Dict[str, str]:
    strategies = config.get('credential_pool_strategies')
    return dict(strategies) if isinstance(strategies, dict) else {}

def _set_credential_pool_strategy(config: Dict[str, Any], provider: str, strategy: str) -> None:
    if not provider:
        return
    strategies = _get_credential_pool_strategies(config)
    strategies[provider] = strategy
    config['credential_pool_strategies'] = strategies

def _supports_same_provider_pool_setup(provider: str) -> bool:
    if not provider or provider == 'custom':
        return False
    if provider == 'openrouter':
        return True
    from hermes_cli.auth import PROVIDER_REGISTRY
    pconfig = PROVIDER_REGISTRY.get(provider)
    if not pconfig:
        return False
    return pconfig.auth_type in {'api_key', 'oauth_device_code'}
_DEFAULT_PROVIDER_MODELS = {'copilot-acp': ['copilot-acp'], 'copilot': ['gpt-5.4', 'gpt-5.4-mini', 'gpt-5-mini', 'gpt-5.3-codex', 'gpt-5.2-codex', 'gpt-4.1', 'gpt-4o', 'gpt-4o-mini', 'claude-opus-4.6', 'claude-sonnet-5', 'claude-sonnet-4.6', 'claude-sonnet-4.5', 'claude-haiku-4.5', 'gemini-2.5-pro'], 'gemini': ['gemini-3.1-pro-preview', 'gemini-3-pro-preview', 'gemini-3.6-flash', 'gemini-3.1-flash-lite-preview'], 'vertex': ['google/gemini-3.1-pro-preview', 'google/gemini-3-pro-preview', 'google/gemini-3-flash-preview', 'google/gemini-3.1-flash-lite-preview', 'google/gemini-2.5-pro', 'google/gemini-2.5-flash'], 'zai': ['glm-5.2', 'glm-5.1', 'glm-5', 'glm-4.7', 'glm-4.5', 'glm-4.5-flash'], 'kimi-coding': ['kimi-k3', 'kimi-k2.6', 'kimi-k2.5', 'kimi-k2-thinking', 'kimi-k2-turbo-preview'], 'kimi-coding-cn': ['kimi-k3', 'kimi-k2.6', 'kimi-k2.5', 'kimi-k2-thinking', 'kimi-k2-turbo-preview'], 'stepfun': ['step-3.5-flash', 'step-3.5-flash-2603'], 'arcee': ['trinity-large-thinking', 'trinity-large-preview', 'trinity-mini'], 'minimax': ['MiniMax-M2.7', 'MiniMax-M2.5', 'MiniMax-M2.1', 'MiniMax-M2'], 'minimax-cn': ['MiniMax-M2.7', 'MiniMax-M2.5', 'MiniMax-M2.1', 'MiniMax-M2'], 'ai-gateway': ['anthropic/claude-opus-4.6', 'anthropic/claude-sonnet-4.6', 'openai/gpt-5', 'google/gemini-3-flash'], 'kilocode': ['anthropic/claude-sonnet-5', 'anthropic/claude-opus-4.6', 'anthropic/claude-sonnet-4.6', 'openai/gpt-5.4', 'google/gemini-3-pro-preview', 'google/gemini-3-flash-preview'], 'opencode-zen': ['gpt-5.4', 'gpt-5.3-codex', 'claude-sonnet-5', 'claude-sonnet-4-6', 'gemini-3-flash', 'glm-5', 'kimi-k2.5', 'minimax-m2.7'], 'opencode-go': ['kimi-k3', 'kimi-k2.6', 'kimi-k2.5', 'glm-5.1', 'glm-5', 'mimo-v2.5-pro', 'mimo-v2.5', 'mimo-v2-pro', 'mimo-v2-omni', 'minimax-m2.7', 'minimax-m2.5', 'qwen3.7-max', 'qwen3.6-plus', 'qwen3.5-plus'], 'huggingface': ['Qwen/Qwen3.5-397B-A17B', 'Qwen/Qwen3-235B-A22B-Thinking-2507', 'Qwen/Qwen3-Coder-480B-A35B-Instruct', 'deepseek-ai/DeepSeek-R1-0528', 'deepseek-ai/DeepSeek-V3.2', 'moonshotai/Kimi-K2.5']}

def _current_reasoning_effort(config: Dict[str, Any]) -> str:
    agent_cfg = config.get('agent')
    if isinstance(agent_cfg, dict):
        return str(agent_cfg.get('reasoning_effort') or '').strip().lower()
    return ''

def _set_reasoning_effort(config: Dict[str, Any], effort: str) -> None:
    agent_cfg = config.get('agent')
    if not isinstance(agent_cfg, dict):
        agent_cfg = {}
        config['agent'] = agent_cfg
    agent_cfg['reasoning_effort'] = effort
from hermes_cli.config import cfg_get, DEFAULT_CONFIG, get_hermes_home, get_config_path, get_env_path, load_config, save_config, save_env_value, remove_env_value, get_env_value, ensure_hermes_home
from hermes_cli.colors import Colors, color

def print_header(title: str):
    """Print a section header."""
    print()
    print(color(f'◆ {title}', Colors.CYAN, Colors.BOLD))
from hermes_cli.cli_output import print_error, print_info, print_success, print_warning
from hermes_cli.secret_prompt import masked_secret_prompt

def is_interactive_stdin() -> bool:
    """Return True when stdin looks like a usable interactive TTY."""
    stdin = getattr(sys, 'stdin', None)
    if stdin is None:
        return False
    try:
        return bool(stdin.isatty())
    except Exception:
        return False

def print_noninteractive_setup_guidance(reason: str | None=None) -> None:
    """Print guidance for headless/non-interactive setup flows."""
    print()
    print(color('⚕ Duck Agent Setup — Non-interactive mode', Colors.CYAN, Colors.BOLD))
    print()
    if reason:
        print_info(reason)
    print_info('The interactive wizard cannot be used here.')
    print()
    print_info('Configure Duck Agent using environment variables or config commands:')
    print_info('  duck-agent config set model.provider custom')
    print_info('  duck-agent config set model.base_url http://localhost:8080/v1')
    print_info('  duck-agent config set model.default your-model-name')
    print()
    print_info('Or set OPENROUTER_API_KEY / OPENAI_API_KEY in your environment.')
    print_info("Run 'duck-agent setup' in an interactive terminal to use the full wizard.")
    print()

def prompt(question: str, default: str=None, password: bool=False) -> str:
    """Prompt for input with optional default."""
    if default:
        display = f'{question} [{default}]: '
    else:
        display = f'{question}: '
    try:
        if password:
            value = masked_secret_prompt(color(display, Colors.YELLOW))
        else:
            value = input(color(display, Colors.YELLOW))
        cleaned = _sanitize_pasted_input(value)
        return cleaned.strip() or default or ''
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(1)
_BRACKETED_PASTE_PATTERN = re.compile('\\x1b\\[\\s*200~|\\x1b\\[\\s*201~')

def _sanitize_pasted_input(value: str) -> str:
    """Strip terminal bracketed-paste control markers from pasted text."""
    if not isinstance(value, str) or not value:
        return value
    return _BRACKETED_PASTE_PATTERN.sub('', value)

def _curses_prompt_choice(question: str, choices: list, default: int=0, description: str | None=None) -> int:
    """Single-select menu using curses. Delegates to curses_radiolist."""
    from hermes_cli.curses_ui import curses_radiolist
    return curses_radiolist(question, choices, selected=default, cancel_returns=-1, description=description)

def prompt_choice(question: str, choices: list, default: int=0, description: str | None=None) -> int:
    """Prompt for a choice from a list with arrow key navigation.

    Escape keeps the current default (skips the question).
    Ctrl+C exits the wizard.
    """
    idx = _curses_prompt_choice(question, choices, default, description=description)
    if idx >= 0:
        if idx == default:
            print_info('  Skipped (keeping current)')
            print()
            return default
        print()
        return idx
    print(color(question, Colors.YELLOW))
    for i, choice in enumerate(choices):
        marker = '●' if i == default else '○'
        if i == default:
            print(color(f'  {marker} {choice}', Colors.GREEN))
        else:
            print(f'  {marker} {choice}')
    print_info(f'  Enter for default ({default + 1})  Ctrl+C to exit')
    while True:
        try:
            value = input(color(f'  Select [1-{len(choices)}] ({default + 1}): ', Colors.DIM))
            if not value:
                return default
            idx = int(value) - 1
            if 0 <= idx < len(choices):
                return idx
            print_error(f'Please enter a number between 1 and {len(choices)}')
        except ValueError:
            print_error('Please enter a number')
        except (KeyboardInterrupt, EOFError):
            print()
            sys.exit(1)

def is_noninteractive() -> bool:
    """True when no human is available to answer a prompt.

    The dashboard/desktop spawn CLI actions with ``stdin=DEVNULL`` and
    ``HERMES_NONINTERACTIVE=1`` (see ``hermes_cli/web_server.py``). In that
    context an ``input()`` raises ``EOFError`` immediately, so a prompt that
    aborts on EOF kills the spawned action — this is what made the desktop
    "restart gateway" fail when the Windows gateway service was not yet
    installed (the start path asks "Install it now?" with no one to answer).
    Honour the explicit env flag here so callers fall back to their default.
    """
    return os.environ.get('HERMES_NONINTERACTIVE', '').strip().lower() in {'1', 'true', 'yes', 'on'}

def prompt_yes_no(question: str, default: bool=True) -> bool:
    """Prompt for yes/no. Ctrl+C exits, empty input returns default.

    Non-interactive callers (``HERMES_NONINTERACTIVE=1`` or a closed/redirected
    stdin) have no one to answer, so fall back to ``default`` instead of
    aborting the whole process.
    """
    if is_noninteractive():
        return default
    default_str = 'Y/n' if default else 'y/N'
    while True:
        try:
            value = input(color(f'{question} [{default_str}]: ', Colors.YELLOW)).strip().lower()
        except KeyboardInterrupt:
            print()
            sys.exit(1)
        except EOFError:
            print()
            return default
        if not value:
            return default
        if value in {'y', 'yes'}:
            return True
        if value in {'n', 'no'}:
            return False
        print_error("Please enter 'y' or 'n'")

def prompt_checklist(title: str, items: list, pre_selected: list=None) -> list:
    """
    Display a multi-select checklist and return the indices of selected items.

    Each item in `items` is a display string. `pre_selected` is a list of
    indices that should be checked by default. A "Continue →" option is
    appended at the end — the user toggles items with Space and confirms
    with Enter on "Continue →".

    Falls back to a numbered toggle interface when curses is
    unavailable.

    Returns:
        List of selected indices (not including the Continue option).
    """
    if pre_selected is None:
        pre_selected = []
    from hermes_cli.curses_ui import curses_checklist
    chosen = curses_checklist(title, items, set(pre_selected), cancel_returns=set(pre_selected))
    return sorted(chosen)

def _prompt_api_key(var: dict):
    """Display a nicely formatted API key input screen for a single env var."""
    tools = var.get('tools', [])
    tools_str = ', '.join(tools[:3])
    if len(tools) > 3:
        tools_str += f', +{len(tools) - 3} more'
    print()
    print(color(f"  ─── {var.get('description', var['name'])} ───", Colors.CYAN))
    print()
    if tools_str:
        print_info(f'  Enables: {tools_str}')
    if var.get('url'):
        print_info(f"  Get your key at: {var['url']}")
    print()
    if var.get('password'):
        value = prompt(f"  {var.get('prompt', var['name'])}", password=True)
    else:
        value = prompt(f"  {var.get('prompt', var['name'])}")
    if value:
        save_env_value(var['name'], value)
        print_success('  ✓ Saved')
    else:
        print_warning("  Skipped (configure later with 'duck-agent setup')")

def _print_setup_summary(config: dict, hermes_home):
    """Print the setup completion summary."""
    try:
        from hermes_cli.auth import resolve_provider
        resolve_provider()
        _provider_ready = True
    except Exception:
        _provider_ready = False
    if not _provider_ready:
        print()
        print_warning('No inference provider is configured — Duck Agent cannot chat yet.')
        print_info('  Finish this one step with either of:')
        print_info('    duck-agent model            (pick any provider/model)')
        print_info('    duck-agent setup --portal   (Nous Portal OAuth, no API key)')
    print()
    print_header('Tool Availability Summary')
    tool_status = []
    subscription_features = get_nous_subscription_features(config)
    try:
        from agent.auxiliary_client import get_available_vision_backends
        _vision_backends = get_available_vision_backends()
    except Exception:
        _vision_backends = []
    if _vision_backends:
        tool_status.append(('Vision (image analysis)', True, None))
    else:
        tool_status.append(('Vision (image analysis)', False, "run 'duck-agent setup' to configure"))
    if subscription_features.web.managed_by_nous:
        tool_status.append(('Web Search & Extract (Nous subscription)', True, None))
    elif subscription_features.web.available:
        label = 'Web Search & Extract'
        if subscription_features.web.current_provider:
            label = f'Web Search & Extract ({subscription_features.web.current_provider})'
        tool_status.append((label, True, None))
    else:
        tool_status.append(('Web Search & Extract', False, 'EXA_API_KEY, PARALLEL_API_KEY, FIRECRAWL_API_KEY/FIRECRAWL_API_URL, TAVILY_API_KEY, or SEARXNG_URL'))
    browser_provider = subscription_features.browser.current_provider
    if subscription_features.browser.managed_by_nous:
        tool_status.append(('Browser Automation (Nous Browser Use)', True, None))
    elif subscription_features.browser.available:
        label = 'Browser Automation'
        if browser_provider:
            label = f'Browser Automation ({browser_provider})'
        tool_status.append((label, True, None))
    else:
        missing_browser_hint = 'npm install -g agent-browser, set CAMOFOX_URL, or configure Browser Use or Browserbase'
        if browser_provider == 'Browserbase':
            missing_browser_hint = 'npm install -g agent-browser and set BROWSERBASE_API_KEY/BROWSERBASE_PROJECT_ID'
        elif browser_provider == 'Browser Use':
            missing_browser_hint = 'npm install -g agent-browser and set BROWSER_USE_API_KEY'
        elif browser_provider == 'Camofox':
            missing_browser_hint = 'CAMOFOX_URL'
        elif browser_provider == 'Local browser':
            missing_browser_hint = 'npm install -g agent-browser && agent-browser install --with-deps'
        tool_status.append(('Browser Automation', False, missing_browser_hint))
    if subscription_features.image_gen.managed_by_nous:
        tool_status.append(('Image Generation (Nous subscription)', True, None))
    elif subscription_features.image_gen.available:
        tool_status.append(('Image Generation', True, None))
    else:
        _img_backend = None
        try:
            from agent.image_gen_registry import list_providers
            from hermes_cli.plugins import _ensure_plugins_discovered
            _ensure_plugins_discovered()
            for _p in list_providers():
                if _p.name == 'fal':
                    continue
                try:
                    if _p.is_available():
                        _img_backend = _p.display_name
                        break
                except Exception:
                    continue
        except Exception:
            pass
        if _img_backend:
            tool_status.append((f'Image Generation ({_img_backend})', True, None))
        else:
            tool_status.append(('Image Generation', False, 'FAL_KEY or OPENAI_API_KEY'))
    if subscription_features.video_gen.managed_by_nous:
        tool_status.append(('Video Generation (FAL via Nous subscription)', True, None))
    else:
        try:
            from agent.video_gen_registry import list_providers as _list_video_providers
            from hermes_cli.plugins import _ensure_plugins_discovered as _ensure_plugins
            _ensure_plugins()
            _video_backend = None
            for _vp in _list_video_providers():
                try:
                    if _vp.is_available():
                        _video_backend = _vp.display_name
                        break
                except Exception:
                    continue
        except Exception:
            _video_backend = None
        if _video_backend:
            tool_status.append((f'Video Generation ({_video_backend})', True, None))
    tts_provider = cfg_get(config, 'tts', 'provider', default='edge')
    if subscription_features.tts.managed_by_nous:
        tool_status.append(('Text-to-Speech (OpenAI via Nous subscription)', True, None))
    elif tts_provider == 'elevenlabs' and get_env_value('ELEVENLABS_API_KEY'):
        tool_status.append(('Text-to-Speech (ElevenLabs)', True, None))
    elif tts_provider == 'openai' and (get_env_value('VOICE_TOOLS_OPENAI_KEY') or get_env_value('OPENAI_API_KEY')):
        tool_status.append(('Text-to-Speech (OpenAI)', True, None))
    elif tts_provider == 'minimax' and get_env_value('MINIMAX_API_KEY'):
        tool_status.append(('Text-to-Speech (MiniMax)', True, None))
    elif tts_provider == 'mistral' and get_env_value('MISTRAL_API_KEY'):
        tool_status.append(('Text-to-Speech (Mistral Voxtral)', True, None))
    elif tts_provider == 'gemini' and (get_env_value('GEMINI_API_KEY') or get_env_value('GOOGLE_API_KEY')):
        tool_status.append(('Text-to-Speech (Google Gemini)', True, None))
    elif tts_provider == 'neutts':
        try:
            neutts_ok = importlib.util.find_spec('neutts') is not None
        except Exception:
            neutts_ok = False
        if neutts_ok:
            tool_status.append(('Text-to-Speech (NeuTTS local)', True, None))
        else:
            tool_status.append(('Text-to-Speech (NeuTTS — not installed)', False, "run 'duck-agent setup tts'"))
    elif tts_provider == 'kittentts':
        try:
            kittentts_ok = importlib.util.find_spec('kittentts') is not None
        except Exception:
            kittentts_ok = False
        if kittentts_ok:
            tool_status.append(('Text-to-Speech (KittenTTS local)', True, None))
        else:
            tool_status.append(('Text-to-Speech (KittenTTS — not installed)', False, "run 'duck-agent setup tts'"))
    else:
        tool_status.append(('Text-to-Speech (Edge TTS)', True, None))
    stt_provider = cfg_get(config, 'stt', 'provider', default='local') or 'local'
    _stt_feature = subscription_features.features.get('stt')
    if _stt_feature is not None and _stt_feature.managed_by_nous:
        tool_status.append(('Speech-to-Text (OpenAI via Nous subscription)', True, None))
    elif stt_provider == 'openai' and (get_env_value('VOICE_TOOLS_OPENAI_KEY') or get_env_value('OPENAI_API_KEY')):
        tool_status.append(('Speech-to-Text (OpenAI)', True, None))
    elif stt_provider == 'groq' and get_env_value('GROQ_API_KEY'):
        tool_status.append(('Speech-to-Text (Groq Whisper)', True, None))
    elif stt_provider == 'elevenlabs' and get_env_value('ELEVENLABS_API_KEY'):
        tool_status.append(('Speech-to-Text (ElevenLabs Scribe)', True, None))
    elif stt_provider == 'xai':
        tool_status.append(('Speech-to-Text (xAI)', True, None))
    elif stt_provider == 'deepinfra' and get_env_value('DEEPINFRA_API_KEY'):
        tool_status.append(('Speech-to-Text (DeepInfra)', True, None))
    else:
        try:
            fw_ok = importlib.util.find_spec('faster_whisper') is not None
        except Exception:
            fw_ok = False
        if fw_ok:
            tool_status.append(('Speech-to-Text (Local Whisper)', True, None))
        else:
            tool_status.append(('Speech-to-Text (Local Whisper — not installed)', False, "run 'duck-agent tools' → Speech-to-Text"))
    if subscription_features.modal.managed_by_nous:
        tool_status.append(('Modal Execution (Nous subscription)', True, None))
    elif cfg_get(config, 'terminal', 'backend') == 'modal':
        if subscription_features.modal.direct_override:
            tool_status.append(('Modal Execution (direct Modal)', True, None))
        else:
            tool_status.append(('Modal Execution', False, "run 'duck-agent setup terminal'"))
    elif managed_nous_tools_enabled() and subscription_features.nous_auth_present:
        tool_status.append(('Modal Execution (optional via Nous subscription)', True, None))
    if get_env_value('HASS_TOKEN'):
        tool_status.append(('Smart Home (Home Assistant)', True, None))
    try:
        from hermes_cli.auth import get_provider_auth_state
        _spotify_state = get_provider_auth_state('spotify') or {}
        if _spotify_state.get('access_token') or _spotify_state.get('refresh_token'):
            tool_status.append(('Spotify (PKCE OAuth)', True, None))
    except Exception:
        pass
    if get_env_value('GITHUB_TOKEN'):
        tool_status.append(('Skills Hub (GitHub)', True, None))
    else:
        tool_status.append(('Skills Hub (GitHub)', False, 'GITHUB_TOKEN'))
    tool_status.append(('Terminal/Commands', True, None))
    tool_status.append(('Task Planning (todo)', True, None))
    tool_status.append(('Skills (view, create, edit)', True, None))
    available_count = sum((1 for _, avail, _ in tool_status if avail))
    total_count = len(tool_status)
    print_info(f'{available_count}/{total_count} tool categories available:')
    print()
    for name, available, missing_var in tool_status:
        if available:
            print(f"   {color('✓', Colors.GREEN)} {name}")
        else:
            print(f"   {color('✗', Colors.RED)} {name} {color(f'(missing {missing_var})', Colors.DIM)}")
    print()
    disabled_tools = [(name, var) for name, avail, var in tool_status if not avail]
    if disabled_tools:
        print_warning("Some tools are disabled. Run 'duck-agent setup tools' to configure them,")
        from hermes_constants import display_hermes_home as _dhh
        print_warning(f'or edit {_dhh()}/.env directly to add the missing API keys.')
        print()
    print()
    print(color('┌─────────────────────────────────────────────────────────┐', Colors.GREEN))
    print(color('│              ✓ Setup Complete!                          │', Colors.GREEN))
    print(color('└─────────────────────────────────────────────────────────┘', Colors.GREEN))
    print()
    from hermes_constants import display_hermes_home as _dhh
    print(color(f'📁 All your files are in {_dhh()}/:', Colors.CYAN, Colors.BOLD))
    print()
    print(f"   {color('Settings:', Colors.YELLOW)}  {get_config_path()}")
    print(f"   {color('API Keys:', Colors.YELLOW)}  {get_env_path()}")
    print(f"   {color('Data:', Colors.YELLOW)}      {hermes_home}/cron/, sessions/, logs/")
    print()
    print(color('─' * 60, Colors.DIM))
    print()
    print(color('📝 To edit your configuration:', Colors.CYAN, Colors.BOLD))
    print()
    print(f"   {color('duck-agent setup', Colors.GREEN)}          Re-run the full wizard")
    print(f"   {color('duck-agent setup model', Colors.GREEN)}    Change model/provider")
    print(f"   {color('duck-agent setup terminal', Colors.GREEN)} Change terminal backend")
    print(f"   {color('duck-agent setup gateway', Colors.GREEN)}  Configure messaging")
    print(f"   {color('duck-agent setup tools', Colors.GREEN)}    Configure tool providers")
    print()
    print(f"   {color('duck-agent config', Colors.GREEN)}         View current settings")
    print(f"   {color('duck-agent config edit', Colors.GREEN)}    Open config in your editor")
    print(f"   {color('duck-agent config set <key> <value>', Colors.GREEN)}")
    print('                          Set a specific value')
    print()
    print('   Or edit the files directly:')
    print(f"   {color(f'nano {get_config_path()}', Colors.DIM)}")
    print(f"   {color(f'nano {get_env_path()}', Colors.DIM)}")
    print()
    print(color('─' * 60, Colors.DIM))
    print()
    print(color('🚀 Ready to go!', Colors.CYAN, Colors.BOLD))
    print()
    print(f"   {color('duck-agent', Colors.GREEN)}              Start chatting")
    print(f"   {color('duck-agent gateway', Colors.GREEN)}      Start messaging gateway")
    print(f"   {color('duck-agent doctor', Colors.GREEN)}       Check for issues")
    print()

def _prompt_container_resources(config: dict):
    """Prompt for container resource settings (Docker, Singularity, Modal, Daytona)."""
    terminal = config.setdefault('terminal', {})
    print()
    print_info('Container Resource Settings:')
    current_persist = terminal.get('container_persistent', True)
    persist_label = 'yes' if current_persist else 'no'
    print_info('  Persistent filesystem keeps files between sessions.')
    print_info("  Set to 'no' for ephemeral sandboxes that reset each time.")
    persist_str = prompt('  Persist filesystem across sessions? (yes/no)', persist_label)
    terminal['container_persistent'] = persist_str.lower() in {'yes', 'true', 'y', '1'}
    current_cpu = terminal.get('container_cpu', 1)
    cpu_str = prompt('  CPU cores', str(current_cpu))
    try:
        terminal['container_cpu'] = float(cpu_str)
    except ValueError:
        pass
    current_mem = terminal.get('container_memory', 5120)
    mem_str = prompt('  Memory in MB (5120 = 5GB)', str(current_mem))
    try:
        terminal['container_memory'] = int(mem_str)
    except ValueError:
        pass
    current_disk = terminal.get('container_disk', 51200)
    disk_str = prompt('  Disk in MB (51200 = 50GB)', str(current_disk))
    try:
        terminal['container_disk'] = int(disk_str)
    except ValueError:
        pass

def _prompt_vercel_sandbox_settings(config: dict):
    """Prompt for Vercel Sandbox settings without exposing unsupported disk sizing."""
    terminal = config.setdefault('terminal', {})
    print()
    print_info('Vercel Sandbox settings:')
    print_info('  Filesystem persistence uses Vercel snapshots.')
    print_info('  Snapshots restore files only; live processes do not continue after sandbox recreation.')
    from tools.terminal_tool import _SUPPORTED_VERCEL_RUNTIMES
    current_runtime = terminal.get('vercel_runtime') or 'node24'
    supported_label = ', '.join(_SUPPORTED_VERCEL_RUNTIMES)
    runtime = prompt(f'  Runtime ({supported_label})', current_runtime).strip() or current_runtime
    if runtime not in _SUPPORTED_VERCEL_RUNTIMES:
        print_warning(f"Unsupported Vercel runtime '{runtime}', keeping {current_runtime}.")
        runtime = current_runtime if current_runtime in _SUPPORTED_VERCEL_RUNTIMES else 'node24'
    terminal['vercel_runtime'] = runtime
    save_env_value('TERMINAL_VERCEL_RUNTIME', runtime)
    current_persist = terminal.get('container_persistent', True)
    persist_label = 'yes' if current_persist else 'no'
    terminal['container_persistent'] = prompt('  Persist filesystem with snapshots? (yes/no)', persist_label).lower() in {'yes', 'true', 'y', '1'}
    current_cpu = terminal.get('container_cpu', 1)
    cpu_str = prompt('  CPU cores', str(current_cpu))
    try:
        terminal['container_cpu'] = float(cpu_str)
    except ValueError:
        pass
    current_mem = terminal.get('container_memory', 5120)
    mem_str = prompt('  Memory in MB (5120 = 5GB)', str(current_mem))
    try:
        terminal['container_memory'] = int(mem_str)
    except ValueError:
        pass
    if terminal.get('container_disk', 51200) not in {0, 51200}:
        print_warning('Vercel Sandbox does not support custom disk sizing; resetting container_disk to 51200.')
    terminal['container_disk'] = 51200
    print()
    print_info('Vercel authentication:')
    print_info('  Use a long-lived Vercel access token plus project/team IDs.')
    linked_project = _read_nearest_vercel_project()
    if linked_project:
        print_info('  Found defaults in nearest .vercel/project.json.')
    remove_env_value('VERCEL_OIDC_TOKEN')
    token = prompt('    Vercel access token', get_env_value('VERCEL_TOKEN') or '', password=True)
    project = prompt('    Vercel project ID', get_env_value('VERCEL_PROJECT_ID') or linked_project.get('projectId', ''))
    team = prompt('    Vercel team ID', get_env_value('VERCEL_TEAM_ID') or linked_project.get('orgId', ''))
    if token:
        save_env_value('VERCEL_TOKEN', token)
    if project:
        save_env_value('VERCEL_PROJECT_ID', project)
    if team:
        save_env_value('VERCEL_TEAM_ID', team)

def _read_nearest_vercel_project(start: Path | None=None) -> dict[str, str]:
    """Read project/team defaults from the nearest Vercel link file."""
    current = (start or Path.cwd()).resolve()
    if current.is_file():
        current = current.parent
    for directory in (current, *current.parents):
        project_file = directory / '.vercel' / 'project.json'
        if not project_file.exists():
            continue
        try:
            data = json.loads(project_file.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {key: value for key, value in {'projectId': data.get('projectId'), 'orgId': data.get('orgId')}.items() if isinstance(value, str) and value.strip()}
    return {}

def setup_model_provider(config: dict, *, quick: bool=False):
    """Configure the inference provider and default model.

    Delegates to ``cmd_model()`` (the same flow used by ``duck-agent model``)
    for provider selection, credential prompting, and model picking.
    This ensures a single code path for all provider setup — any new
    provider added to ``duck-agent model`` is automatically available here.

    When *quick* is True, skips credential rotation, vision, and TTS
    configuration — used by the streamlined first-time quick setup.
    """
    from hermes_cli.config import load_config, save_config
    print_header('Inference Provider')
    print_info('Choose how to connect to your main chat model.')
    print_info(f'   Guide: {_DOCS_BASE}/integrations/providers')
    print()
    from hermes_cli.main import select_provider_and_model
    try:
        select_provider_and_model()
    except (SystemExit, KeyboardInterrupt):
        print()
        print_info('Provider setup skipped.')
    except Exception as exc:
        logger.debug('select_provider_and_model error during setup: %s', exc)
        print_warning(f'Provider setup encountered an error: {exc}')
        print_info('You can try again later with: duck-agent model')
    _refreshed = load_config()
    config.clear()
    config.update(_refreshed)
    save_config(config)

def _check_espeak_ng() -> bool:
    """Check if espeak-ng is installed."""
    return shutil.which('espeak-ng') is not None or shutil.which('espeak') is not None

def _install_neutts_deps() -> bool:
    """Install NeuTTS dependencies with user approval. Returns True on success."""
    import subprocess
    import sys
    if not _check_espeak_ng():
        print()
        print_warning('NeuTTS requires espeak-ng for phonemization.')
        if sys.platform == 'darwin':
            print_info('Install with: brew install espeak-ng')
        elif sys.platform == 'win32':
            print_info('Install with: choco install espeak-ng')
        else:
            print_info('Install with: sudo apt install espeak-ng')
        print()
        if prompt_yes_no('Install espeak-ng now?', True):
            try:
                if sys.platform == 'darwin':
                    subprocess.run(['brew', 'install', 'espeak-ng'], check=True)
                elif sys.platform == 'win32':
                    subprocess.run(['choco', 'install', 'espeak-ng', '-y'], check=True)
                else:
                    subprocess.run(['sudo', 'apt', 'install', '-y', 'espeak-ng'], check=True)
                print_success('espeak-ng installed')
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                print_warning(f'Could not install espeak-ng automatically: {e}')
                print_info('Please install it manually and re-run setup.')
                return False
        else:
            print_warning('espeak-ng is required for NeuTTS. Install it manually before using NeuTTS.')
    print()
    print_info('Installing neutts Python package...')
    print_info('This will also download the TTS model (~300MB) on first use.')
    print()
    from hermes_cli.tools_config import _pip_install
    try:
        result = _pip_install(['-U', 'neutts[all]', '--quiet'], timeout=300)
    except Exception as e:
        print_error(f'Failed to install neutts: {e}')
        print_info("Try manually: uv pip install -U 'neutts[all]'")
        return False
    if result.returncode == 0:
        print_success('neutts installed successfully')
        return True
    err = (result.stderr or '').strip()
    print_error(f"Failed to install neutts: {(err[:300] if err else 'install failed')}")
    print_info("Try manually: uv pip install -U 'neutts[all]'")
    return False

def _install_kittentts_deps() -> bool:
    """Install KittenTTS dependencies with user approval. Returns True on success."""
    wheel_url = 'https://github.com/KittenML/KittenTTS/releases/download/0.8.1/kittentts-0.8.1-py3-none-any.whl'
    print()
    print_info('Installing kittentts Python package (~25-80MB model downloaded on first use)...')
    print()
    from hermes_cli.tools_config import _pip_install
    try:
        result = _pip_install(['-U', wheel_url, 'soundfile', '--quiet'], timeout=300)
    except Exception as e:
        print_error(f'Failed to install kittentts: {e}')
        print_info(f"Try manually: uv pip install -U '{wheel_url}' soundfile")
        return False
    if result.returncode == 0:
        print_success('kittentts installed successfully')
        return True
    err = (result.stderr or '').strip()
    print_error(f"Failed to install kittentts: {(err[:300] if err else 'install failed')}")
    print_info(f"Try manually: uv pip install -U '{wheel_url}' soundfile")
    return False

def _xai_oauth_logged_in_for_setup() -> bool:
    """True iff xAI Grok OAuth credentials are already stored locally.

    Lets TTS / STT setup skip the API-key prompt for users who logged in
    through ``duck-agent model`` -> xAI Grok OAuth (SuperGrok / Premium+).
    """
    try:
        from hermes_cli.auth import get_xai_oauth_auth_status
        return bool(get_xai_oauth_auth_status().get('logged_in'))
    except Exception:
        return False

def _run_xai_oauth_login_from_setup() -> bool:
    """Run the xAI Grok OAuth device-code login from inside the setup wizard.

    Saves OAuth tokens only. Does **not** switch the active inference
    provider or rewrite ``model.provider`` — callers (TTS setup, tools
    config) only need credentials for side tools.

    Returns True on success, False on any failure (the caller falls back
    to whatever the user picked next, e.g. Edge TTS).
    """
    try:
        from hermes_cli.auth import _is_remote_session, _save_xai_oauth_tokens, _xai_oauth_device_code_login, unsuppress_credential_source
    except Exception as exc:
        print_warning(f'xAI Grok OAuth helpers unavailable: {exc}')
        return False
    open_browser = not _is_remote_session()
    print()
    print_info('Signing in to xAI Grok OAuth (SuperGrok / Premium+)...')
    try:
        creds = _xai_oauth_device_code_login(open_browser=open_browser)
        _save_xai_oauth_tokens(creds['tokens'], discovery=creds.get('discovery'), redirect_uri=creds.get('redirect_uri', ''), last_refresh=creds.get('last_refresh'), auth_mode='oauth_device_code', set_active=False)
        unsuppress_credential_source('xai-oauth', 'device_code')
        return True
    except Exception as exc:
        print_warning(f'xAI Grok OAuth login failed: {exc}')
        return False

def _setup_tts_provider(config: dict):
    """Interactive TTS provider selection with install flow for NeuTTS."""
    tts_config = config.get('tts', {})
    current_provider = tts_config.get('provider', 'edge')
    subscription_features = get_nous_subscription_features(config)
    provider_labels = {'edge': 'Edge TTS', 'elevenlabs': 'ElevenLabs', 'openai': 'OpenAI TTS', 'xai': 'xAI TTS', 'minimax': 'MiniMax TTS', 'mistral': 'Mistral Voxtral TTS', 'gemini': 'Google Gemini TTS', 'neutts': 'NeuTTS', 'kittentts': 'KittenTTS'}
    current_label = provider_labels.get(current_provider, current_provider)
    print()
    print_header('Text-to-Speech Provider (optional)')
    print_info(f'Current: {current_label}')
    print()
    choices = []
    providers = []
    if managed_nous_tools_enabled() and subscription_features.nous_auth_present:
        choices.append('Nous Subscription (managed OpenAI TTS, billed to your subscription)')
        providers.append('nous-openai')
    choices.extend(['Edge TTS (free, cloud-based, no setup needed)', 'ElevenLabs (premium quality, needs API key)', 'OpenAI TTS (good quality, needs API key)', 'xAI TTS (Grok voices — OAuth login or API key)', 'MiniMax TTS (high quality with voice cloning, needs API key)', 'Mistral Voxtral TTS (multilingual, native Opus, needs API key)', 'Google Gemini TTS (30 prebuilt voices, prompt-controllable, needs API key)', 'NeuTTS (local on-device, free, ~300MB model download)', 'KittenTTS (local on-device, free, lightweight ~25-80MB ONNX)'])
    providers.extend(['edge', 'elevenlabs', 'openai', 'xai', 'minimax', 'mistral', 'gemini', 'neutts', 'kittentts'])
    choices.append(f'Keep current ({current_label})')
    keep_current_idx = len(choices) - 1
    idx = prompt_choice('Select TTS provider:', choices, keep_current_idx)
    if idx == keep_current_idx:
        return
    selected = providers[idx]
    selected_via_nous = selected == 'nous-openai'
    if selected == 'nous-openai':
        selected = 'openai'
        print_info('OpenAI TTS will use the managed Nous gateway and bill to your subscription.')
        if get_env_value('VOICE_TOOLS_OPENAI_KEY') or get_env_value('OPENAI_API_KEY'):
            print_warning('Direct OpenAI credentials are still configured and may take precedence until removed from ~/.duck-agent/.env.')
    if selected == 'neutts':
        try:
            already_installed = importlib.util.find_spec('neutts') is not None
        except Exception:
            already_installed = False
        if already_installed:
            print_success('NeuTTS is already installed')
        else:
            print()
            print_info('NeuTTS requires:')
            print_info('  • Python package: neutts (~50MB install + ~300MB model on first use)')
            print_info('  • System package: espeak-ng (phonemizer)')
            print()
            if prompt_yes_no('Install NeuTTS dependencies now?', True):
                if not _install_neutts_deps():
                    print_warning('NeuTTS installation incomplete. Falling back to Edge TTS.')
                    selected = 'edge'
            else:
                print_info("Skipping install. Set tts.provider to 'neutts' after installing manually.")
                selected = 'edge'
    elif selected == 'elevenlabs':
        existing = get_env_value('ELEVENLABS_API_KEY')
        if not existing:
            print()
            api_key = prompt('ElevenLabs API key', password=True)
            if api_key:
                save_env_value('ELEVENLABS_API_KEY', api_key)
                print_success('ElevenLabs API key saved')
            else:
                print_warning('No API key provided. Falling back to Edge TTS.')
                selected = 'edge'
    elif selected == 'openai' and (not selected_via_nous):
        existing = get_env_value('VOICE_TOOLS_OPENAI_KEY') or get_env_value('OPENAI_API_KEY')
        if not existing:
            print()
            api_key = prompt('OpenAI API key for TTS', password=True)
            if api_key:
                save_env_value('VOICE_TOOLS_OPENAI_KEY', api_key)
                print_success('OpenAI TTS API key saved')
            else:
                print_warning('No API key provided. Falling back to Edge TTS.')
                selected = 'edge'
    elif selected == 'xai':
        oauth_logged_in = _xai_oauth_logged_in_for_setup()
        existing_api_key = get_env_value('XAI_API_KEY')
        if oauth_logged_in:
            print_success('xAI TTS will use your xAI Grok OAuth (SuperGrok / Premium+) credentials')
        elif existing_api_key:
            print_success('xAI TTS will use your existing XAI_API_KEY')
        else:
            print()
            choice_idx = prompt_choice('How do you want xAI TTS to authenticate?', choices=['Sign in with xAI Grok OAuth (SuperGrok / Premium+) — browser login', 'Paste an xAI API key (console.x.ai)', 'Skip → fallback to Edge TTS'], default=0)
            if choice_idx == 0:
                if _run_xai_oauth_login_from_setup():
                    print_success('Logged in — xAI TTS will use these OAuth credentials')
                else:
                    print_warning('xAI Grok OAuth login did not complete. Falling back to Edge TTS.')
                    selected = 'edge'
            elif choice_idx == 1:
                api_key = prompt('xAI API key for TTS', password=True)
                if api_key:
                    save_env_value('XAI_API_KEY', api_key)
                    print_success('xAI TTS API key saved')
                else:
                    from hermes_constants import display_hermes_home as _dhh
                    print_warning(f'No xAI API key provided for TTS. Configure XAI_API_KEY via duck-agent setup model or {_dhh()}/.env to use xAI TTS. Falling back to Edge TTS.')
                    selected = 'edge'
            else:
                print_warning('xAI TTS skipped. Falling back to Edge TTS.')
                selected = 'edge'
        if selected == 'xai':
            print()
            voice_id = prompt("xAI voice_id (Enter for 'eve', or paste a custom voice ID)")
            if voice_id and voice_id.strip():
                config.setdefault('tts', {}).setdefault('xai', {})['voice_id'] = voice_id.strip()
                print_success(f'xAI voice_id set to: {voice_id.strip()}')
    elif selected == 'minimax':
        existing = get_env_value('MINIMAX_API_KEY')
        if not existing:
            print()
            api_key = prompt('MiniMax API key for TTS', password=True)
            if api_key:
                save_env_value('MINIMAX_API_KEY', api_key)
                print_success('MiniMax TTS API key saved')
            else:
                print_warning('No API key provided. Falling back to Edge TTS.')
                selected = 'edge'
    elif selected == 'mistral':
        existing = get_env_value('MISTRAL_API_KEY')
        if not existing:
            print()
            api_key = prompt('Mistral API key for TTS', password=True)
            if api_key:
                save_env_value('MISTRAL_API_KEY', api_key)
                print_success('Mistral TTS API key saved')
            else:
                print_warning('No API key provided. Falling back to Edge TTS.')
                selected = 'edge'
    elif selected == 'gemini':
        existing = get_env_value('GEMINI_API_KEY') or get_env_value('GOOGLE_API_KEY')
        if not existing:
            print()
            print_info('Get a free API key at https://aistudio.google.com/app/apikey')
            api_key = prompt('Gemini API key for TTS', password=True)
            if api_key:
                save_env_value('GEMINI_API_KEY', api_key)
                print_success('Gemini TTS API key saved')
            else:
                print_warning('No API key provided. Falling back to Edge TTS.')
                selected = 'edge'
    elif selected == 'kittentts':
        try:
            already_installed = importlib.util.find_spec('kittentts') is not None
        except Exception:
            already_installed = False
        if already_installed:
            print_success('KittenTTS is already installed')
        else:
            print()
            print_info('KittenTTS is lightweight (~25-80MB, CPU-only, no API key required).')
            print_info('Voices: Jasper, Bella, Luna, Bruno, Rosie, Hugo, Kiki, Leo')
            print()
            if prompt_yes_no('Install KittenTTS now?', True):
                if not _install_kittentts_deps():
                    print_warning('KittenTTS installation incomplete. Falling back to Edge TTS.')
                    selected = 'edge'
            else:
                print_info("Skipping install. Set tts.provider to 'kittentts' after installing manually.")
                selected = 'edge'
    if 'tts' not in config:
        config['tts'] = {}
    config['tts']['provider'] = selected
    save_config(config)
    print_success(f'TTS provider set to: {provider_labels.get(selected, selected)}')

def setup_tts(config: dict):
    """Standalone TTS setup (for 'duck-agent setup tts')."""
    _setup_tts_provider(config)

def setup_terminal_backend(config: dict):
    """Configure the terminal execution backend."""
    import platform as _platform
    print_header('Terminal Backend')
    print_info('Choose where Duck Agent runs shell commands and code.')
    print_info('This affects tool execution, file access, and isolation.')
    print_info(f'   Guide: {_DOCS_BASE}/user-guide/configuration#terminal-backend-configuration')
    print()
    current_backend = cfg_get(config, 'terminal', 'backend', default='local')
    is_linux = _platform.system() == 'Linux'
    terminal_choices = ['Local - run directly on this machine (default)', 'Docker - isolated container with configurable resources', 'Modal - serverless cloud sandbox', 'SSH - run on a remote machine', 'Daytona - persistent cloud development environment', 'Vercel Sandbox - cloud microVM with snapshot filesystem persistence']
    idx_to_backend = {0: 'local', 1: 'docker', 2: 'modal', 3: 'ssh', 4: 'daytona', 5: 'vercel_sandbox'}
    backend_to_idx = {'local': 0, 'docker': 1, 'modal': 2, 'ssh': 3, 'daytona': 4, 'vercel_sandbox': 5}
    next_idx = 6
    if is_linux:
        terminal_choices.append('Singularity/Apptainer - HPC-friendly container')
        idx_to_backend[next_idx] = 'singularity'
        backend_to_idx['singularity'] = next_idx
        next_idx += 1
    keep_current_idx = next_idx
    terminal_choices.append(f'Keep current ({current_backend})')
    idx_to_backend[keep_current_idx] = current_backend
    terminal_idx = prompt_choice('Select terminal backend:', terminal_choices, keep_current_idx)
    selected_backend = idx_to_backend.get(terminal_idx)
    if terminal_idx == keep_current_idx:
        print_info(f'Keeping current backend: {current_backend}')
        return
    config.setdefault('terminal', {})['backend'] = selected_backend
    if selected_backend == 'local':
        print_success('Terminal backend: Local')
        print_info('Commands run directly on this machine.')
        config['terminal'].setdefault('cwd', str(Path.home()))
    elif selected_backend == 'docker':
        print_success('Terminal backend: Docker')
        docker_bin = shutil.which('docker')
        if not docker_bin:
            print_warning('Docker not found in PATH!')
            print_info('Install Docker: https://docs.docker.com/get-docker/')
        else:
            print_info(f'Docker found: {docker_bin}')
        config['terminal'].setdefault('docker_image', 'nikolaik/python-nodejs:python3.11-nodejs20')
        print()
        print_info('Docker sandboxes can be protected with the egress credential firewall.')
        print_info('It routes sandbox traffic through iron-proxy so containers receive proxy tokens instead of real API keys.')
        print_info('   Docker only for now; Modal, SSH, Daytona, and Singularity are not wired yet.')
        if prompt_yes_no('  Enable egress firewall for Docker sandboxes?', False):
            proxy_cfg = config.setdefault('proxy', {})
            proxy_cfg['enabled'] = True
            proxy_cfg.setdefault('enforce_on_docker', True)
            print_success('Egress firewall enabled in config')
            print_info('Run `duck-agent egress setup` then `duck-agent egress start` to mint tokens and launch the proxy.')
        else:
            print_info('Skipping egress firewall. You can enable it later with `duck-agent egress setup`.')
    elif selected_backend == 'singularity':
        print_success('Terminal backend: Singularity/Apptainer')
        sing_bin = shutil.which('apptainer') or shutil.which('singularity')
        if not sing_bin:
            print_warning('Singularity/Apptainer not found in PATH!')
            print_info('Install: https://apptainer.org/docs/admin/main/installation.html')
        else:
            print_info(f'Found: {sing_bin}')
        config['terminal'].setdefault('singularity_image', 'docker://nikolaik/python-nodejs:python3.11-nodejs20')
    elif selected_backend == 'modal':
        print_success('Terminal backend: Modal')
        print_info('Serverless cloud sandboxes. Each session gets its own container.')
        from tools.managed_tool_gateway import is_managed_tool_gateway_ready
        from tools.tool_backend_helpers import normalize_modal_mode
        managed_modal_available = bool(managed_nous_tools_enabled() and get_nous_subscription_features(config).nous_auth_present and is_managed_tool_gateway_ready('modal'))
        modal_mode = normalize_modal_mode(cfg_get(config, 'terminal', 'modal_mode'))
        use_managed_modal = False
        if managed_modal_available:
            modal_choices = ['Use my Nous subscription', 'Use my own Modal account']
            if modal_mode == 'managed':
                default_modal_idx = 0
            elif modal_mode == 'direct':
                default_modal_idx = 1
            else:
                default_modal_idx = 1 if get_env_value('MODAL_TOKEN_ID') else 0
            modal_mode_idx = prompt_choice('Select how Modal execution should be billed:', modal_choices, default_modal_idx)
            use_managed_modal = modal_mode_idx == 0
        if use_managed_modal:
            config['terminal']['modal_mode'] = 'managed'
            print_info('Modal execution will use the managed Nous gateway and bill to your subscription.')
            if get_env_value('MODAL_TOKEN_ID') or get_env_value('MODAL_TOKEN_SECRET'):
                print_info('Direct Modal credentials are still configured, but this backend is pinned to managed mode.')
        else:
            config['terminal']['modal_mode'] = 'direct'
            print_info('Requires a Modal account: https://modal.com')
            try:
                __import__('modal')
            except ImportError:
                print_info('Installing modal SDK...')
                from hermes_cli.tools_config import _pip_install
                result = _pip_install(['modal'])
                if result.returncode == 0:
                    print_success('modal SDK installed')
                else:
                    print_warning('Install failed — run manually: uv pip install modal')
            print()
            print_info('Modal authentication:')
            print_info('  Get your token at: https://modal.com/settings')
            existing_token = get_env_value('MODAL_TOKEN_ID')
            if existing_token:
                print_info('  Modal token: already configured')
                if prompt_yes_no('  Update Modal credentials?', False):
                    token_id = prompt('    Modal Token ID', password=True)
                    token_secret = prompt('    Modal Token Secret', password=True)
                    if token_id:
                        save_env_value('MODAL_TOKEN_ID', token_id)
                    if token_secret:
                        save_env_value('MODAL_TOKEN_SECRET', token_secret)
            else:
                token_id = prompt('    Modal Token ID', password=True)
                token_secret = prompt('    Modal Token Secret', password=True)
                if token_id:
                    save_env_value('MODAL_TOKEN_ID', token_id)
                if token_secret:
                    save_env_value('MODAL_TOKEN_SECRET', token_secret)
    elif selected_backend == 'daytona':
        print_success('Terminal backend: Daytona')
        print_info('Persistent cloud development environments.')
        print_info('Each session gets a dedicated sandbox with filesystem persistence.')
        print_info('Sign up at: https://daytona.io')
        try:
            __import__('daytona')
        except ImportError:
            print_info('Installing daytona SDK...')
            from hermes_cli.tools_config import _pip_install
            result = _pip_install(['daytona'])
            if result.returncode == 0:
                print_success('daytona SDK installed')
            else:
                print_warning('Install failed — run manually: uv pip install daytona')
                if result.stderr:
                    print_info(f'  Error: {result.stderr.strip().splitlines()[-1]}')
        print()
        existing_key = get_env_value('DAYTONA_API_KEY')
        if existing_key:
            print_info('  Daytona API key: already configured')
            if prompt_yes_no('  Update API key?', False):
                api_key = prompt('    Daytona API key', password=True)
                if api_key:
                    save_env_value('DAYTONA_API_KEY', api_key)
                    print_success('    Updated')
        else:
            api_key = prompt('    Daytona API key', password=True)
            if api_key:
                save_env_value('DAYTONA_API_KEY', api_key)
                print_success('    Configured')
        config['terminal'].setdefault('daytona_image', 'nikolaik/python-nodejs:python3.11-nodejs20')
    elif selected_backend == 'vercel_sandbox':
        print_success('Terminal backend: Vercel Sandbox')
        print_info('Cloud microVM sandboxes with snapshot-backed filesystem persistence.')
        print_info("Requires the optional SDK: pip install 'duck-agent[vercel]'")
        try:
            __import__('vercel')
        except ImportError:
            print_info('Installing vercel SDK...')
            import subprocess
            from hermes_cli.managed_uv import ensure_uv
            uv_bin = ensure_uv()
            if uv_bin:
                result = subprocess.run([uv_bin, 'pip', 'install', '--python', sys.executable, 'vercel'], capture_output=True, text=True)
            else:
                result = subprocess.run([sys.executable, '-m', 'pip', 'install', 'vercel'], capture_output=True, text=True)
            if result.returncode == 0:
                print_success('vercel SDK installed')
            else:
                print_warning("Install failed — run manually: pip install 'duck-agent[vercel]'")
                if result.stderr:
                    print_info(f'  Error: {result.stderr.strip().splitlines()[-1]}')
        _prompt_vercel_sandbox_settings(config)
    elif selected_backend == 'ssh':
        print_success('Terminal backend: SSH')
        print_info('Run commands on a remote machine via SSH.')
        current_host = get_env_value('TERMINAL_SSH_HOST') or ''
        host = prompt('  SSH host (hostname or IP)', current_host)
        if host:
            save_env_value('TERMINAL_SSH_HOST', host)
        current_user = get_env_value('TERMINAL_SSH_USER') or ''
        user = prompt('  SSH user', current_user or os.getenv('USER', ''))
        if user:
            save_env_value('TERMINAL_SSH_USER', user)
        current_port = get_env_value('TERMINAL_SSH_PORT') or '22'
        port = prompt('  SSH port', current_port)
        if port and port != '22':
            save_env_value('TERMINAL_SSH_PORT', port)
        current_key = get_env_value('TERMINAL_SSH_KEY') or ''
        default_key = str(Path.home() / '.ssh' / 'id_rsa')
        ssh_key = prompt('  SSH private key path', current_key or default_key)
        if ssh_key:
            save_env_value('TERMINAL_SSH_KEY', ssh_key)
        if host and prompt_yes_no('  Test SSH connection?', True):
            print_info('  Testing connection...')
            import subprocess
            ssh_cmd = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5']
            if ssh_key:
                ssh_cmd.extend(['-i', ssh_key])
            if port and port != '22':
                ssh_cmd.extend(['-p', port])
            ssh_cmd.append(f'{user}@{host}' if user else host)
            ssh_cmd.append('echo ok')
            result = subprocess.run(ssh_cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
            if result.returncode == 0:
                print_success('  SSH connection successful!')
            else:
                print_warning(f'  SSH connection failed: {result.stderr.strip()}')
                print_info('  Check your SSH key and host settings.')
    save_env_value('TERMINAL_ENV', selected_backend)
    if selected_backend == 'modal':
        save_env_value('TERMINAL_MODAL_MODE', config['terminal'].get('modal_mode', 'auto'))
    if selected_backend == 'vercel_sandbox':
        save_env_value('TERMINAL_VERCEL_RUNTIME', config['terminal'].get('vercel_runtime', 'node24'))
    save_config(config)
    print()
    print_success(f'Terminal backend set to: {selected_backend}')

def _apply_default_agent_settings(config: dict):
    """Apply recommended defaults for all agent settings without prompting."""
    config.setdefault('agent', {})['max_turns'] = 150
    remove_env_value('HERMES_MAX_ITERATIONS')
    config.setdefault('display', {})['tool_progress'] = 'all'
    config.setdefault('compression', {})['enabled'] = True
    config['compression']['threshold'] = 0.5
    config.setdefault('session_reset', {})['mode'] = 'none'
    save_config(config)
    print_success('Applied recommended defaults:')
    print_info('  Max iterations: 150')
    print_info('  Tool progress: all')
    print_info('  Compression threshold: 0.50')
    print_info('  Session reset: never (use /reset or compression)')
    print_info('  Run `duck-agent setup agent` later to customize.')

def setup_agent_settings(config: dict):
    """Configure agent behavior: iterations, progress display, compression, session reset."""
    print_header('Agent Settings')
    print_info(f'   Guide: {_DOCS_BASE}/user-guide/configuration')
    print()
    current_max = str(cfg_get(config, 'agent', 'max_turns', default=90))
    print_info('Maximum tool-calling iterations per conversation.')
    print_info('Higher = more complex tasks, but costs more tokens.')
    print_info(f'Press Enter to keep {current_max}. Use 90 for most tasks or 150+ for open exploration.')
    max_iter_str = prompt('Max iterations', current_max)
    try:
        max_iter = int(max_iter_str)
        if max_iter > 0:
            config.setdefault('agent', {})['max_turns'] = max_iter
            config.pop('max_turns', None)
            remove_env_value('HERMES_MAX_ITERATIONS')
            print_success(f'Max iterations set to {max_iter}')
    except ValueError:
        print_warning('Invalid number, keeping current value')
    print_info('')
    print_info('Tool Progress Display')
    print_info('Controls how much tool activity is shown (CLI and messaging).')
    print_info('  off     — Silent, just the final response')
    print_info('  new     — Show tool name only when it changes (less noise)')
    print_info('  all     — Show every tool call with a short preview')
    print_info('  verbose — Full args, results, and debug logs')
    print_info('  log     — Silent in chat; write every tool call to ~/.duck-agent/logs/tool_calls.log (gateway only)')
    current_mode = cfg_get(config, 'display', 'tool_progress', default='all')
    mode = prompt('Tool progress mode', current_mode)
    if mode.lower() in {'off', 'new', 'all', 'verbose', 'log'}:
        if 'display' not in config:
            config['display'] = {}
        config['display']['tool_progress'] = mode.lower()
        save_config(config)
        print_success(f'Tool progress set to: {mode.lower()}')
    else:
        print_warning(f"Unknown mode '{mode}', keeping '{current_mode}'")
    print_header('Context Compression')
    print_info('Automatically summarizes old messages when context gets too long.')
    print_info('Higher threshold = compress later (use more context). Lower = compress sooner.')
    config.setdefault('compression', {})['enabled'] = True
    current_threshold = cfg_get(config, 'compression', 'threshold', default=0.5)
    threshold_str = prompt('Compression threshold (0.5-0.95)', str(current_threshold))
    try:
        threshold = float(threshold_str)
        if 0.5 <= threshold <= 0.95:
            config['compression']['threshold'] = threshold
    except ValueError:
        pass
    print_success(f"Context compression threshold set to {config['compression'].get('threshold', 0.5)}")
    print_header('Session Reset Policy')
    print_info('Messaging sessions (Telegram, Discord, etc.) accumulate context over time.')
    print_info('Each message adds to the conversation history, which means growing API costs.')
    print_info('')
    print_info('To manage this, sessions can automatically reset after a period of inactivity')
    print_info('or at a fixed time each day. When a reset happens, the agent saves important')
    print_info('things to its persistent memory first — but the conversation context is cleared.')
    print_info('')
    print_info('You can also manually reset anytime by typing /reset in chat.')
    print_info('')
    reset_choices = ['Inactivity + daily reset (reset whichever comes first)', 'Inactivity only (reset after N minutes of no messages)', 'Daily only (reset at a fixed hour each day)', 'Never auto-reset (recommended - context lives until /reset or context compression)', 'Keep current settings']
    current_policy = config.get('session_reset', {})
    current_mode = current_policy.get('mode', 'none')
    current_idle = current_policy.get('idle_minutes', 1440)
    current_hour = current_policy.get('at_hour', 4)
    default_reset = {'both': 0, 'idle': 1, 'daily': 2, 'none': 3}.get(current_mode, 3)
    reset_idx = prompt_choice('Session reset mode:', reset_choices, default_reset)
    config.setdefault('session_reset', {})
    if reset_idx == 0:
        config['session_reset']['mode'] = 'both'
        idle_str = prompt('  Inactivity timeout (minutes)', str(current_idle))
        try:
            idle_val = int(idle_str)
            if idle_val > 0:
                config['session_reset']['idle_minutes'] = idle_val
        except ValueError:
            pass
        hour_str = prompt('  Daily reset hour (0-23, local time)', str(current_hour))
        try:
            hour_val = int(hour_str)
            if 0 <= hour_val <= 23:
                config['session_reset']['at_hour'] = hour_val
        except ValueError:
            pass
        print_success(f"Sessions reset after {config['session_reset'].get('idle_minutes', 1440)} min idle or daily at {config['session_reset'].get('at_hour', 4)}:00")
    elif reset_idx == 1:
        config['session_reset']['mode'] = 'idle'
        idle_str = prompt('  Inactivity timeout (minutes)', str(current_idle))
        try:
            idle_val = int(idle_str)
            if idle_val > 0:
                config['session_reset']['idle_minutes'] = idle_val
        except ValueError:
            pass
        print_success(f"Sessions reset after {config['session_reset'].get('idle_minutes', 1440)} min of inactivity")
    elif reset_idx == 2:
        config['session_reset']['mode'] = 'daily'
        hour_str = prompt('  Daily reset hour (0-23, local time)', str(current_hour))
        try:
            hour_val = int(hour_str)
            if 0 <= hour_val <= 23:
                config['session_reset']['at_hour'] = hour_val
        except ValueError:
            pass
        print_success(f"Sessions reset daily at {config['session_reset'].get('at_hour', 4)}:00")
    elif reset_idx == 3:
        config['session_reset']['mode'] = 'none'
        print_info('Sessions will never auto-reset. Context is managed only by compression.')
        print_warning('Long conversations will grow in cost. Use /reset manually when needed.')
    save_config(config)
_TELEGRAM_BOT_TOKEN_RE = re.compile('^\\d+:[A-Za-z0-9_-]{30,}$')

def _is_valid_telegram_bot_token(token: str) -> bool:
    return bool(_TELEGRAM_BOT_TOKEN_RE.match(token))

def _setup_telegram_auto_result():
    """Attempt automatic Telegram bot creation via managed QR onboarding."""
    try:
        from hermes_cli.telegram_managed_bot import auto_setup_telegram_bot_result
    except ImportError:
        return None
    profile_name: str | None = None
    try:
        profile_name = _profile_name_from_hermes_home(Path(get_hermes_home()))
    except Exception:
        pass
    return auto_setup_telegram_bot_result(profile_name=profile_name)

def _profile_name_from_hermes_home(hermes_home) -> str | None:
    """Return the active profile name when DUCK_AGENT_HOME is a profile dir."""
    if hermes_home.parent.name == 'profiles':
        return hermes_home.name
    return None

def _setup_telegram_auto() -> str | None:
    """Attempt automatic Telegram bot creation and return only the token."""
    result = _setup_telegram_auto_result()
    return result.token if result else None

def _prompt_telegram_bot_token() -> str | None:
    print_info('Create a bot via @BotFather on Telegram')
    while True:
        token = prompt('Telegram bot token', password=True)
        if not token:
            return None
        if not _is_valid_telegram_bot_token(token):
            print_error('Invalid token format. Expected: <numeric_id>:<alphanumeric_hash> (e.g., 123456789:ABCdefGHI-jklMNOpqrSTUvwxYZ)')
            continue
        return token

def _setup_telegram():
    """Configure Telegram bot credentials and allowlist."""
    print_header('Telegram')
    existing = get_env_value('TELEGRAM_BOT_TOKEN')
    if existing:
        print_info('Telegram: already configured')
        if not prompt_yes_no('Reconfigure Telegram?', False):
            if not get_env_value('TELEGRAM_ALLOWED_USERS'):
                print_info('⚠️  Telegram has no user allowlist - anyone can use your bot!')
                if prompt_yes_no('Add allowed users now?', True):
                    print_info('   To find your Telegram user ID: message @userinfobot')
                    allowed_users = prompt('Allowed user IDs (comma-separated)')
                    if allowed_users:
                        save_env_value('TELEGRAM_ALLOWED_USERS', allowed_users.replace(' ', ''))
                        print_success('Telegram allowlist configured')
            return
    print_info('How would you like to create your Telegram bot?')
    print()
    print_info('  [1] Automatic (recommended)')
    print_info('      Scan a QR code → confirm in Telegram → done.')
    print_info('      No token copy-paste needed.')
    print()
    print_info('  [2] Manual')
    print_info('      Create a bot via @BotFather yourself and paste the token.')
    print()
    choice = prompt('Choice [1/2]', default='1')
    token = None
    setup_result = None
    if choice.strip() == '1':
        setup_result = _setup_telegram_auto_result()
        if setup_result:
            token = setup_result.token
            if not _is_valid_telegram_bot_token(token):
                print_error('Automatic setup returned an invalid Telegram bot token.')
                token = None
                setup_result = None
        else:
            token = None
        if not token:
            print()
            print_info('Falling back to manual setup...')
            print()
    if not token:
        token = _prompt_telegram_bot_token()
    if not token:
        return
    save_env_value('TELEGRAM_BOT_TOKEN', token)
    print_success('Telegram token saved')
    print()
    print_info('🔒 Security: Restrict who can use your bot')
    print_info('   To find your Telegram user ID:')
    print_info('   1. Message @userinfobot on Telegram')
    print_info('   2. It will reply with your numeric ID (e.g., 123456789)')
    print()
    detected_user_id = getattr(setup_result, 'owner_user_id', None)
    if detected_user_id:
        detected_id = str(detected_user_id)
        print_success(f'Detected your Telegram user ID: {detected_id}')
        if prompt_yes_no('Allow this Telegram account to use the bot?', True):
            extra = prompt('Additional allowed user IDs (comma-separated, optional)')
            ids = [detected_id]
            for uid in extra.replace(' ', '').split(','):
                if uid and uid not in ids:
                    ids.append(uid)
            allowed_users = ','.join(ids)
        else:
            allowed_users = prompt('Allowed user IDs (comma-separated, leave empty for open access)')
    else:
        allowed_users = prompt('Allowed user IDs (comma-separated, leave empty for open access)')
    if allowed_users:
        allowed_users = allowed_users.replace(' ', '')
        save_env_value('TELEGRAM_ALLOWED_USERS', allowed_users)
        print_success('Telegram allowlist configured - only listed users can use the bot')
    else:
        print_info('⚠️  No allowlist set - anyone who finds your bot can use it!')
    print()
    print_info('📬 Home Channel: where Duck Agent delivers cron job results,')
    print_info('   cross-platform messages, and notifications.')
    print_info('   For Telegram DMs, this is your user ID (same as above).')
    first_user_id = allowed_users.split(',')[0].strip() if allowed_users else ''
    if first_user_id:
        if prompt_yes_no(f'Use your user ID ({first_user_id}) as the home channel?', True):
            save_env_value('TELEGRAM_HOME_CHANNEL', first_user_id)
            print_success(f'Telegram home channel set to {first_user_id}')
        else:
            home_channel = prompt('Home channel ID (or leave empty to set later with /set-home in Telegram)')
            if home_channel:
                save_env_value('TELEGRAM_HOME_CHANNEL', home_channel)
    else:
        print_info('   You can also set this later by typing /set-home in your Telegram chat.')
        home_channel = prompt('Home channel ID (leave empty to set later)')
        if home_channel:
            save_env_value('TELEGRAM_HOME_CHANNEL', home_channel)

def _setup_bluebubbles():
    """Configure BlueBubbles iMessage gateway."""
    print_header('BlueBubbles (iMessage)')
    existing = get_env_value('BLUEBUBBLES_SERVER_URL')
    if existing:
        print_info('BlueBubbles: already configured')
        if not prompt_yes_no('Reconfigure BlueBubbles?', False):
            return
    print_info('Connects Duck Agent to iMessage via BlueBubbles — a free, open-source')
    print_info('macOS server that bridges iMessage to any device.')
    print_info('   Requires a Mac running BlueBubbles Server v1.0.0+')
    print_info('   Download: https://bluebubbles.app/')
    print()
    print_info('In BlueBubbles Server → Settings → API, note your Server URL and Password.')
    print()
    server_url = prompt('BlueBubbles server URL (e.g. http://192.168.1.10:1234)')
    if not server_url:
        print_warning('Server URL is required — skipping BlueBubbles setup')
        return
    save_env_value('BLUEBUBBLES_SERVER_URL', server_url.rstrip('/'))
    password = prompt('BlueBubbles server password', password=True)
    if not password:
        print_warning('Password is required — skipping BlueBubbles setup')
        return
    save_env_value('BLUEBUBBLES_PASSWORD', password)
    print_success('BlueBubbles credentials saved')
    print()
    print_info('🔒 Security: Restrict who can message your bot')
    print_info('   Use iMessage addresses: email (user@icloud.com) or phone (+15551234567)')
    print()
    allowed_users = prompt('Allowed iMessage addresses (comma-separated, leave empty for open access)')
    if allowed_users:
        save_env_value('BLUEBUBBLES_ALLOWED_USERS', allowed_users.replace(' ', ''))
        print_success('BlueBubbles allowlist configured')
    else:
        print_info('⚠️  No allowlist set — anyone who can iMessage you can use the bot!')
    print()
    print_info('📬 Home Channel: phone or email for cron job delivery and notifications.')
    print_info('   You can also set this later with /set-home in your iMessage chat.')
    home_channel = prompt('Home channel address (leave empty to set later)')
    if home_channel:
        save_env_value('BLUEBUBBLES_HOME_CHANNEL', home_channel)
    print()
    print_info('Advanced settings (defaults are fine for most setups):')
    if prompt_yes_no('Configure webhook listener settings?', False):
        webhook_port = prompt('Webhook listener port (default: 8645)')
        if webhook_port:
            try:
                save_env_value('BLUEBUBBLES_WEBHOOK_PORT', str(int(webhook_port)))
                print_success(f'Webhook port set to {webhook_port}')
            except ValueError:
                print_warning('Invalid port number, using default 8645')
    print()
    print_info('Requires the BlueBubbles Private API helper for typing indicators,')
    print_info('read receipts, and tapback reactions. Basic messaging works without it.')
    print_info('   Install: https://docs.bluebubbles.app/helper-bundle/installation')

def _setup_qqbot():
    """Configure QQ Bot (Official API v2) via gateway setup."""
    from hermes_cli.gateway import _setup_qqbot as _gateway_setup_qqbot
    _gateway_setup_qqbot()

def _setup_webhooks():
    """Configure webhook integration."""
    print_header('Webhooks')
    existing = get_env_value('WEBHOOK_ENABLED')
    if existing:
        print_info('Webhooks: already configured')
        if not prompt_yes_no('Reconfigure webhooks?', False):
            return
    print()
    print_warning('⚠  Webhook and SMS platforms require exposing gateway ports to the')
    print_warning('   internet. For security, run the gateway in a sandboxed environment')
    print_warning('   (Docker, VM, etc.) to limit blast radius from prompt injection.')
    print()
    print_info('   Full guide: https://duck-agent.nousresearch.com/docs/user-guide/messaging/webhooks/')
    print()
    port = prompt('Webhook port (default 8644)')
    if port:
        try:
            save_env_value('WEBHOOK_PORT', str(int(port)))
            print_success(f'Webhook port set to {port}')
        except ValueError:
            print_warning('Invalid port number, using default 8644')
    secret = prompt('Global HMAC secret (shared across all routes)', password=True)
    if secret:
        save_env_value('WEBHOOK_SECRET', secret)
        print_success('Webhook secret saved')
    else:
        print_warning('No secret set — you must configure per-route secrets in config.yaml')
    save_env_value('WEBHOOK_ENABLED', 'true')
    print()
    print_success('Webhooks enabled! Next steps:')
    from hermes_constants import display_hermes_home as _dhh
    print_info(f'   1. Define webhook routes in {_dhh()}/config.yaml')
    print_info('   2. Point your service (GitHub, GitLab, etc.) at:')
    print_info('      http://your-server:8644/webhooks/<route-name>')
    print()
    print_info('   Route configuration guide:')
    print_info('   https://duck-agent.nousresearch.com/docs/user-guide/messaging/webhooks/#configuring-routes')
    print()
    print_info('   Open config in your editor:  duck-agent config edit')
    print_info('   Open config in your editor:  duck-agent config edit')

def setup_gateway(config: dict):
    """Configure messaging platform integrations."""
    from hermes_cli.gateway import _all_platforms, _platform_status, _configure_platform
    print_header('Messaging Platforms')
    print_info('Connect to messaging platforms to chat with Duck Agent from anywhere.')
    print_info('Toggle with Space, confirm with Enter.')
    print()
    platforms = _all_platforms()
    items = []
    pre_selected = []
    for i, plat in enumerate(platforms):
        status = _platform_status(plat)
        items.append(f"{plat['emoji']} {plat['label']}  ({status})")
        if status == 'configured':
            pre_selected.append(i)
    selected = prompt_checklist('Select platforms to configure:', items, pre_selected)
    if not selected:
        print_info("No platforms selected. Run 'duck-agent setup gateway' later to configure.")
        return
    for idx in selected:
        _configure_platform(platforms[idx])

    def _is_progress(status: str) -> bool:
        s = status.lower()
        return not (s == 'not configured' or s.startswith('partially') or s.startswith('plugin disabled'))
    any_messaging = any((_is_progress(_platform_status(p)) for p in _all_platforms()))
    if any_messaging:
        print()
        print_info('━' * 50)
        print_success('Messaging platforms configured!')
        missing_home = []
        if get_env_value('TELEGRAM_BOT_TOKEN') and (not get_env_value('TELEGRAM_HOME_CHANNEL')):
            missing_home.append('Telegram')
        if get_env_value('DISCORD_BOT_TOKEN') and (not get_env_value('DISCORD_HOME_CHANNEL')):
            missing_home.append('Discord')
        if get_env_value('SLACK_BOT_TOKEN') and (not get_env_value('SLACK_HOME_CHANNEL')):
            missing_home.append('Slack')
        if get_env_value('BLUEBUBBLES_SERVER_URL') and (not get_env_value('BLUEBUBBLES_HOME_CHANNEL')):
            missing_home.append('BlueBubbles')
        if get_env_value('QQ_APP_ID') and (not (get_env_value('QQBOT_HOME_CHANNEL') or get_env_value('QQ_HOME_CHANNEL'))):
            missing_home.append('QQBot')
        if missing_home:
            print()
            print_warning(f"No home channel set for: {', '.join(missing_home)}")
            print_info('   Without a home channel, cron jobs and cross-platform')
            print_info("   messages can't be delivered to those platforms.")
            print_info('   Set one later with /set-home in your chat, or:')
            for plat in missing_home:
                print_info(f'     duck-agent config set {plat.upper()}_HOME_CHANNEL <channel_id>')
        import platform as _platform
        _is_linux = _platform.system() == 'Linux'
        _is_macos = _platform.system() == 'Darwin'
        _is_windows = _platform.system() == 'Windows'
        from hermes_cli.gateway import _is_service_installed, _is_service_running, supports_systemd_services, has_conflicting_systemd_units, has_legacy_hermes_units, install_linux_gateway_from_setup, print_systemd_scope_conflict_warning, print_legacy_unit_warning, systemd_start, systemd_restart, launchd_install, launchd_start, launchd_restart, UserSystemdUnavailableError, SystemScopeRequiresRootError, _system_scope_wizard_would_need_root, _print_system_scope_remediation
        service_installed = _is_service_installed()
        service_running = _is_service_running()
        supports_systemd = supports_systemd_services()
        supports_service_manager = supports_systemd or _is_macos or _is_windows
        print()
        if supports_systemd and has_conflicting_systemd_units():
            print_systemd_scope_conflict_warning()
            print()
        if supports_systemd and has_legacy_hermes_units():
            print_legacy_unit_warning()
            print()
        if service_running:
            if supports_systemd and _system_scope_wizard_would_need_root():
                _print_system_scope_remediation('restart')
            elif prompt_yes_no('  Restart the gateway to pick up changes?', True):
                try:
                    if supports_systemd:
                        systemd_restart()
                    elif _is_macos:
                        launchd_restart()
                    elif _is_windows:
                        from hermes_cli import gateway_windows
                        gateway_windows.restart()
                except UserSystemdUnavailableError as e:
                    print_error('  Restart failed — user systemd not reachable:')
                    for line in str(e).splitlines():
                        print(f'  {line}')
                except SystemScopeRequiresRootError as e:
                    print_error(f'  Restart failed: {e}')
                    _print_system_scope_remediation('restart')
                except Exception as e:
                    print_error(f'  Restart failed: {e}')
        elif service_installed:
            if supports_systemd and _system_scope_wizard_would_need_root():
                _print_system_scope_remediation('start')
            elif prompt_yes_no('  Start the gateway service?', True):
                try:
                    if supports_systemd:
                        systemd_start()
                    elif _is_macos:
                        launchd_start()
                    elif _is_windows:
                        from hermes_cli import gateway_windows
                        gateway_windows.start()
                except UserSystemdUnavailableError as e:
                    print_error('  Start failed — user systemd not reachable:')
                    for line in str(e).splitlines():
                        print(f'  {line}')
                except SystemScopeRequiresRootError as e:
                    print_error(f'  Start failed: {e}')
                    _print_system_scope_remediation('start')
                except Exception as e:
                    print_error(f'  Start failed: {e}')
        elif supports_service_manager:
            if supports_systemd:
                svc_name = 'systemd'
            elif _is_macos:
                svc_name = 'launchd'
            else:
                svc_name = 'Scheduled Task'
            if prompt_yes_no(f'  Install the gateway as a {svc_name} service? (runs in background, starts on boot)', True):
                try:
                    installed_scope = None
                    did_install = False
                    started_inline = False
                    if supports_systemd:
                        installed_scope, did_install = install_linux_gateway_from_setup(force=False)
                    elif _is_macos:
                        launchd_install(force=False)
                        did_install = True
                    else:
                        from hermes_cli import gateway_windows
                        gateway_windows.install(force=False)
                        did_install = True
                        started_inline = True
                    print()
                    if did_install and (not started_inline) and prompt_yes_no('  Start the service now?', True):
                        try:
                            if supports_systemd:
                                systemd_start(system=installed_scope == 'system')
                            elif _is_macos:
                                launchd_start()
                        except UserSystemdUnavailableError as e:
                            print_error('  Start failed — user systemd not reachable:')
                            for line in str(e).splitlines():
                                print(f'  {line}')
                        except SystemScopeRequiresRootError as e:
                            print_error(f'  Start failed: {e}')
                            _print_system_scope_remediation('start')
                        except Exception as e:
                            print_error(f'  Start failed: {e}')
                except Exception as e:
                    print_error(f'  Install failed: {e}')
                    print_info('  You can try manually: duck-agent gateway install')
            else:
                print_info('  You can install later: duck-agent gateway install')
                if supports_systemd and os.geteuid() == 0:
                    print_info('  Or as a boot-time service: duck-agent gateway install --system')
                print_info('  Or run in foreground:  duck-agent gateway')
        else:
            from hermes_constants import is_container
            if is_container():
                print_info('Start the gateway to bring your bots online:')
                print_info('   duck-agent gateway run          # Run as container main process')
                print_info('')
                print_info('For automatic restarts, use a Docker restart policy:')
                print_info('   docker run --restart unless-stopped ...')
                print_info('   docker restart <container>  # Manual restart')
            else:
                print_info('Start the gateway to bring your bots online:')
                print_info('   duck-agent gateway              # Run in foreground')
        print_info('━' * 50)

def setup_tools(config: dict, first_install: bool=False):
    """Configure tools — delegates to the unified tools_command() in tools_config.py.

    Both `duck-agent setup tools` and `duck-agent tools` use the same flow:
    platform selection → toolset toggles → provider/API key configuration.

    Args:
        first_install: When True, uses the simplified first-install flow
            (no platform menu, prompts for all unconfigured API keys).
    """
    from hermes_cli.tools_config import tools_command
    tools_command(first_install=first_install, config=config)

def setup_telemetry(config: dict):
    """Configure the local, privacy-safe shared-metrics subscriber."""
    print_header('Shared Metrics')
    print_info('Shared metrics contain only bounded counters and histograms.')
    print_info('Packages stay under this Duck Agent profile and are not uploaded.')
    telemetry = config.get('telemetry')
    if not isinstance(telemetry, dict):
        telemetry = {}
        config['telemetry'] = telemetry
    shared_metrics = telemetry.get('shared_metrics')
    if not isinstance(shared_metrics, dict):
        shared_metrics = {}
        telemetry['shared_metrics'] = shared_metrics
    current = shared_metrics.get('enabled') is True
    shared_metrics['enabled'] = prompt_yes_no('Enable local shared metrics?', default=current)
    if shared_metrics['enabled']:
        print_success('Local shared metrics enabled.')
    else:
        print_info('Local shared metrics disabled.')

def _model_section_has_credentials(config: dict) -> bool:
    """Return True when any known inference provider has usable credentials.

    Sources of truth:
      * ``PROVIDER_REGISTRY`` in ``hermes_cli.auth`` — lists every supported
        provider along with its ``api_key_env_vars``.
      * ``active_provider`` in the auth store — covers OAuth device-code /
        external-OAuth providers (Nous, Codex, Qwen, Gemini CLI, ...).
      * The legacy OpenRouter aggregator env vars, which route generic
        ``OPENAI_API_KEY`` / ``OPENROUTER_API_KEY`` values through OpenRouter.
    """
    try:
        from hermes_cli.auth import get_active_provider
        if get_active_provider():
            return True
    except Exception:
        pass
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY
    except Exception:
        PROVIDER_REGISTRY = {}

    def _has_key(pconfig) -> bool:
        for env_var in pconfig.api_key_env_vars:
            if env_var == 'CLAUDE_CODE_OAUTH_TOKEN':
                continue
            if get_env_value(env_var):
                return True
        return False
    model_cfg = config.get('model') if isinstance(config, dict) else None
    if isinstance(model_cfg, dict):
        provider_id = (model_cfg.get('provider') or '').strip().lower()
        if provider_id in PROVIDER_REGISTRY:
            if _has_key(PROVIDER_REGISTRY[provider_id]):
                return True
        if provider_id == 'openrouter':
            for env_var in ('OPENROUTER_API_KEY', 'OPENAI_API_KEY'):
                if get_env_value(env_var):
                    return True
    for env_var in ('OPENROUTER_API_KEY', 'OPENAI_API_KEY'):
        if get_env_value(env_var):
            return True
    for pid, pconfig in PROVIDER_REGISTRY.items():
        if pid == 'copilot':
            continue
        if _has_key(pconfig):
            return True
    return False

def _gateway_platform_short_label(label: str) -> str:
    """Strip trailing parenthetical qualifiers from a gateway platform label."""
    base = label.split('(', 1)[0].strip()
    return base or label

def _get_section_config_summary(config: dict, section_key: str) -> Optional[str]:
    """Return a short summary if a setup section is already configured, else None.

    Used after OpenClaw migration to detect which sections can be skipped.
    ``get_env_value`` is the module-level import from hermes_cli.config
    so that test patches on ``setup_mod.get_env_value`` take effect.
    """
    if section_key == 'model':
        if not _model_section_has_credentials(config):
            return None
        model = config.get('model')
        if isinstance(model, str) and model.strip():
            return model.strip()
        if isinstance(model, dict):
            return str(model.get('default') or model.get('model') or 'configured')
        return 'configured'
    elif section_key == 'terminal':
        backend = cfg_get(config, 'terminal', 'backend', default='local')
        return f'backend: {backend}'
    elif section_key == 'agent':
        max_turns = cfg_get(config, 'agent', 'max_turns', default=90)
        return f'max turns: {max_turns}'
    elif section_key == 'gateway':
        from hermes_cli.gateway import _all_platforms, _platform_status
        configured = [_gateway_platform_short_label(plat['label']) for plat in _all_platforms() if _platform_status(plat) and _platform_status(plat) != 'not configured']
        if configured:
            return ', '.join(configured)
        return None
    elif section_key == 'tools':
        tools = []
        if get_env_value('ELEVENLABS_API_KEY'):
            tools.append('TTS/ElevenLabs')
        if get_env_value('BROWSERBASE_API_KEY'):
            tools.append('Browser')
        if get_env_value('FIRECRAWL_API_KEY'):
            tools.append('Firecrawl')
        if tools:
            return ', '.join(tools)
        return None
    return None

def _skip_configured_section(config: dict, section_key: str, label: str) -> bool:
    """Show an already-configured section summary and offer to skip.

    Returns True if the user chose to skip, False if the section should run.
    """
    summary = _get_section_config_summary(config, section_key)
    if not summary:
        return False
    print()
    print_success(f'  {label}: {summary}')
    return not prompt_yes_no(f'  Reconfigure {label.lower()}?', default=False)
_OPENCLAW_SCRIPT = get_optional_skills_dir(PROJECT_ROOT / 'optional-skills') / 'migration' / 'openclaw-migration' / 'scripts' / 'openclaw_to_hermes.py'

def _load_openclaw_migration_module():
    """Load the openclaw_to_hermes migration script as a module.

    Returns the loaded module, or None if the script can't be loaded.
    """
    if not _OPENCLAW_SCRIPT.exists():
        return None
    spec = importlib.util.spec_from_file_location('openclaw_to_hermes', _OPENCLAW_SCRIPT)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    import sys as _sys
    _sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        _sys.modules.pop(spec.name, None)
        raise
    return mod
_HIGH_IMPACT_KIND_KEYWORDS = {'gateway': '⚠ Gateway/messaging — this will configure Duck Agent to use your OpenClaw messaging channels', 'telegram': '⚠ Telegram — this will point Duck Agent at your OpenClaw Telegram bot', 'slack': '⚠ Slack — this will point Duck Agent at your OpenClaw Slack workspace', 'discord': '⚠ Discord — this will point Duck Agent at your OpenClaw Discord bot', 'whatsapp': '⚠ WhatsApp — this will point Duck Agent at your OpenClaw WhatsApp connection', 'config': '⚠ Config values — OpenClaw settings may not map 1:1 to Duck Agent equivalents', 'soul': '⚠ Instruction file — may contain OpenClaw-specific setup/restart procedures', 'memory': '⚠ Memory/context file — may reference OpenClaw-specific infrastructure', 'context': '⚠ Context file — may contain OpenClaw-specific instructions'}

def _print_migration_preview(report: dict):
    """Print a detailed dry-run preview of what migration would do.

    Groups items by category and adds explicit warnings for high-impact
    changes like gateway token takeover and config value differences.
    """
    items = report.get('items', [])
    if not items:
        print_info('Nothing to migrate.')
        return
    migrated_items = [i for i in items if i.get('status') == 'migrated']
    conflict_items = [i for i in items if i.get('status') == 'conflict']
    skipped_items = [i for i in items if i.get('status') == 'skipped']
    warnings_shown = set()
    if migrated_items:
        print(color('  Would import:', Colors.GREEN))
        for item in migrated_items:
            kind = item.get('kind', 'unknown')
            dest = item.get('destination', '')
            if dest:
                dest_short = str(dest).replace(str(Path.home()), '~')
                print(f'      {kind:<22s} → {dest_short}')
            else:
                print(f'      {kind}')
            kind_lower = kind.lower()
            dest_lower = str(dest).lower()
            for keyword, warning in _HIGH_IMPACT_KIND_KEYWORDS.items():
                if keyword in kind_lower or keyword in dest_lower:
                    warnings_shown.add(warning)
        print()
    if conflict_items:
        print(color('  Would overwrite (conflicts with existing Duck Agent config):', Colors.YELLOW))
        for item in conflict_items:
            kind = item.get('kind', 'unknown')
            reason = item.get('reason', 'already exists')
            print(f'      {kind:<22s}  {reason}')
        print()
    if skipped_items:
        print(color('  Would skip:', Colors.DIM))
        for item in skipped_items:
            kind = item.get('kind', 'unknown')
            reason = item.get('reason', '')
            print(f'      {kind:<22s}  {reason}')
        print()
    if warnings_shown:
        print(color('  ── Warnings ──', Colors.YELLOW))
        for warning in sorted(warnings_shown):
            print(color(f'    {warning}', Colors.YELLOW))
        print()
        print(color('  Note: OpenClaw config values may have different semantics in Duck Agent.', Colors.YELLOW))
        print(color('  For example, OpenClaw\'s tool_call_execution: "auto" ≠ Duck Agent\'ss yolo mode.', Colors.YELLOW))
        print(color('  Instruction files (.md) from OpenClaw may contain incompatible procedures.', Colors.YELLOW))
        print()

def _offer_openclaw_migration(hermes_home: Path) -> bool:
    """Detect ~/.openclaw and offer to migrate during first-time setup.

    Runs a dry-run first to show the user exactly what would be imported,
    overwritten, or taken over. Only executes after explicit confirmation.

    Returns True if migration ran successfully, False otherwise.
    """
    openclaw_dir = Path.home() / '.openclaw'
    if not openclaw_dir.is_dir():
        return False
    if not _OPENCLAW_SCRIPT.exists():
        return False
    print()
    print_header('OpenClaw Installation Detected')
    print_info(f'Found OpenClaw data at {openclaw_dir}')
    print_info('Duck Agent can preview what would be imported before making any changes.')
    print()
    if not prompt_yes_no('Would you like to see what can be imported?', default=True):
        print_info('Skipping migration. You can run it later with: duck-agent claw migrate --dry-run')
        return False
    config_path = get_config_path()
    if not config_path.exists():
        save_config(load_config())
    try:
        mod = _load_openclaw_migration_module()
        if mod is None:
            print_warning('Could not load migration script.')
            return False
    except Exception as e:
        print_warning(f'Could not load migration script: {e}')
        logger.debug('OpenClaw migration module load error', exc_info=True)
        return False
    try:
        selected = mod.resolve_selected_options(None, None, preset='full')
        dry_migrator = mod.Migrator(source_root=openclaw_dir.resolve(), target_root=hermes_home.resolve(), execute=False, workspace_target=None, overwrite=True, migrate_secrets=True, output_dir=None, selected_options=selected, preset_name='full')
        preview_report = dry_migrator.migrate()
    except Exception as e:
        print_warning(f'Migration preview failed: {e}')
        logger.debug('OpenClaw migration preview error', exc_info=True)
        return False
    preview_summary = preview_report.get('summary', {})
    preview_count = preview_summary.get('migrated', 0)
    if preview_count == 0:
        print()
        print_info('Nothing to import from OpenClaw.')
        return False
    print()
    print_header(f'Migration Preview — {preview_count} item(s) would be imported')
    print_info('No changes have been made yet. Review the list below:')
    print()
    _print_migration_preview(preview_report)
    if not prompt_yes_no('Proceed with migration?', default=False):
        print_info('Migration cancelled. You can run it later with: duck-agent claw migrate')
        print_info('Use --dry-run to preview again, or --preset minimal for a lighter import.')
        return False
    try:
        migrator = mod.Migrator(source_root=openclaw_dir.resolve(), target_root=hermes_home.resolve(), execute=True, workspace_target=None, overwrite=False, migrate_secrets=True, output_dir=None, selected_options=selected, preset_name='full')
        report = migrator.migrate()
    except Exception as e:
        print_warning(f'Migration failed: {e}')
        logger.debug('OpenClaw migration error', exc_info=True)
        return False
    summary = report.get('summary', {})
    migrated = summary.get('migrated', 0)
    skipped = summary.get('skipped', 0)
    conflicts = summary.get('conflict', 0)
    errors = summary.get('error', 0)
    print()
    if migrated:
        print_success(f'Imported {migrated} item(s) from OpenClaw.')
    if conflicts:
        print_info(f'Skipped {conflicts} item(s) that already exist in Duck Agent (use duck-agent claw migrate --overwrite to force).')
    if skipped:
        print_info(f'Skipped {skipped} item(s) (not found or unchanged).')
    if errors:
        print_warning(f'{errors} item(s) had errors — check the migration report.')
    output_dir = report.get('output_dir')
    if output_dir:
        print_info(f'Full report saved to: {output_dir}')
    print_success('Migration complete! Continuing with setup...')
    return True
SETUP_SECTIONS = [('model', 'Model & Provider', setup_model_provider), ('tts', 'Text-to-Speech', setup_tts), ('terminal', 'Terminal Backend', setup_terminal_backend), ('gateway', 'Messaging Platforms (Gateway)', setup_gateway), ('tools', 'Tools', setup_tools), ('telemetry', 'Shared Metrics', setup_telemetry), ('agent', 'Agent Settings', setup_agent_settings)]

def _run_portal_one_shot(config: dict) -> None:
    """One-shot Nous Portal setup — OAuth + model pick + provider + Tool Gateway.

    Wired into ``duck-agent setup --portal`` and ``duck-agent portal``. This is the
    Nous-Portal slice of the first-time quick setup, collapsed into a single
    shareable command so a brand-new user goes from zero to a fully working
    Duck Agent session — model selected, provider set, and web/image/tts/browser
    tools routed via their Portal sub — without being told to run
    ``duck-agent setup`` and hunt for the quick-setup option.

    The login + model selection + provider switch + Tool Gateway opt-in are all
    delegated to ``_model_flow_nous`` — the exact same flow quick setup uses
    (``_run_first_time_quick_setup``) and the same one ``duck-agent model`` runs
    when you pick Nous. Routing through it (instead of hand-rolling the auth +
    provider write here) means ``duck-agent portal`` always offers a model picker,
    and there is a single source of truth for the Nous onboarding steps.
    """
    from hermes_cli.config import load_config
    print()
    print(color('┌─────────────────────────────────────────────────────────┐', Colors.MAGENTA))
    print(color('│     ⚕ Duck Agent Setup — Nous Portal (one-shot)             │', Colors.MAGENTA))
    print(color('└─────────────────────────────────────────────────────────┘', Colors.MAGENTA))
    print()
    print_info('  One subscription, 300+ models, plus the Tool Gateway:')
    print_info('    web search, image generation, TTS, browser automation')
    print_info('    — all routed through your Nous Portal sub.')
    print()
    print_info('  Sign up: https://portal.nousresearch.com/manage-subscription')
    print()
    try:
        from hermes_cli.main import _model_flow_nous
        _model_flow_nous(config)
    except (KeyboardInterrupt, EOFError, SystemExit):
        print()
        print_info('  Setup cancelled.')
        print_info('  You can retry later with `duck-agent portal`.')
        return
    except Exception as exc:
        logger.debug('_model_flow_nous error during `duck-agent portal`: %s', exc)
        print()
        print_error(f'  Nous Portal setup encountered an error: {exc}')
        print_info('  You can retry later with `duck-agent portal`.')
        return
    try:
        _refreshed = load_config()
        if isinstance(_refreshed, dict):
            config.clear()
            config.update(_refreshed)
    except Exception:
        pass
    print()
    print_success('Portal setup complete.')
    print_info('  Run `duck-agent portal info` to inspect routing.')
    print_info('  Run `duck-agent` to start chatting.')

def run_setup_wizard(args):
    """Run the interactive setup wizard.

    Supports full, quick, and section-specific setup:
      duck-agent setup           — full or quick (auto-detected)
      duck-agent setup model     — just model/provider
      duck-agent setup tts       — just text-to-speech
      duck-agent setup terminal  — just terminal backend
      duck-agent setup gateway   — just messaging platforms
      duck-agent setup tools     — just tool configuration
      duck-agent setup telemetry — just local shared metrics
      duck-agent setup agent     — just agent settings
    """
    from hermes_cli.config import is_managed, managed_error
    if is_managed():
        managed_error('run setup wizard')
        return
    ensure_hermes_home()
    reset_requested = bool(getattr(args, 'reset', False))
    if reset_requested:
        save_config(copy.deepcopy(DEFAULT_CONFIG))
        print_success('Configuration reset to defaults.')
    reconfigure_requested = bool(getattr(args, 'reconfigure', False))
    quick_requested = bool(getattr(args, 'quick', False))
    config = load_config()
    hermes_home = get_hermes_home()
    config_path = get_config_path()
    if config_path.exists():
        from datetime import datetime as _dt
        _backup_path = config_path.with_suffix(f".yaml.bak.{_dt.now().strftime('%Y%m%d_%H%M%S')}")
        try:
            import shutil
            shutil.copy2(config_path, _backup_path)
        except Exception:
            _backup_path = None
    else:
        _backup_path = None
    non_interactive = getattr(args, 'non_interactive', False)
    if not non_interactive and (not is_interactive_stdin()):
        non_interactive = True
    if non_interactive:
        print_noninteractive_setup_guidance('Running in a non-interactive environment (no TTY detected).')
        return
    if bool(getattr(args, 'portal', False)):
        _run_portal_one_shot(config)
        return
    section = getattr(args, 'section', None)
    if section:
        for key, label, func in SETUP_SECTIONS:
            if key == section:
                print()
                print(color('┌─────────────────────────────────────────────────────────┐', Colors.MAGENTA))
                print(color(f'│     ⚕ Duck Agent Setup — {label:<34s} │', Colors.MAGENTA))
                print(color('└─────────────────────────────────────────────────────────┘', Colors.MAGENTA))
                func(config)
                save_config(config)
                print()
                print_success(f'{label} configuration complete!')
                return
        print_error(f'Unknown setup section: {section}')
        print_info(f"Available sections: {', '.join((k for k, _, _ in SETUP_SECTIONS))}")
        return
    from hermes_cli.auth import get_active_provider
    active_provider = get_active_provider()
    is_existing = bool(get_env_value('OPENROUTER_API_KEY')) or bool(get_env_value('OPENAI_BASE_URL')) or active_provider is not None
    print()
    print(color('┌─────────────────────────────────────────────────────────┐', Colors.MAGENTA))
    print(color('│             ⚕ Duck Agent Setup Wizard                │', Colors.MAGENTA))
    print(color('├─────────────────────────────────────────────────────────┤', Colors.MAGENTA))
    print(color("│  Let's configure your Duck Agent installation.       │", Colors.MAGENTA))
    print(color('│  Press Ctrl+C at any time to exit.                     │', Colors.MAGENTA))
    print(color('└─────────────────────────────────────────────────────────┘', Colors.MAGENTA))
    migration_ran = False
    if is_existing:
        if quick_requested:
            _run_quick_setup(config, hermes_home)
            return
        print()
        print_header('Reconfigure')
        print_success('You already have Duck Agent configured.')
        print_info('Running the full wizard — each prompt shows your current value.')
        print_info('Press Enter to keep it, or type a new value to change it.')
        print_info('')
        print_info("Tip: jump straight to a section with 'duck-agent setup model|terminal|")
        print_info("     gateway|tools|agent', or fill only missing items with --quick.")
    else:
        print()
        if reconfigure_requested or quick_requested:
            print_info('No existing configuration found — running first-time setup.')
            print()
        migration_ran = _offer_openclaw_migration(hermes_home)
        if migration_ran:
            config = load_config()
        setup_mode = prompt_choice('How would you like to set up Duck Agent?', ['Quick Setup (Nous Portal) — free OAuth login, no API keys, model + tools (recommended)', 'Full setup — configure every provider, tool & option yourself (bring your own keys)', 'Blank Slate — everything off except the bare minimum; opt in to each capability'], 0)
        if setup_mode == 0:
            _run_first_time_quick_setup(config, hermes_home, is_existing)
            return
        if setup_mode == 2:
            _run_blank_slate_setup(config, hermes_home, is_existing)
            return
    print_header('Configuration Location')
    print_info(f'Config file:  {get_config_path()}')
    print_info(f'Secrets file: {get_env_path()}')
    print_info(f'Data folder:  {hermes_home}')
    print_info(f'Install dir:  {PROJECT_ROOT}')
    print()
    print_info("You can edit these files directly or use 'duck-agent config edit'")
    if migration_ran:
        print()
        print_info('Settings were imported from OpenClaw.')
        print_info('Each section below will show what was imported — press Enter to keep,')
        print_info('or choose to reconfigure if needed.')
    if not (migration_ran and _skip_configured_section(config, 'model', 'Model & Provider')):
        setup_model_provider(config)
    if not (migration_ran and _skip_configured_section(config, 'terminal', 'Terminal Backend')):
        setup_terminal_backend(config)
    if not is_existing:
        _apply_default_agent_settings(config)
    if not (migration_ran and _skip_configured_section(config, 'gateway', 'Messaging Platforms')):
        setup_gateway(config)
    if not (migration_ran and _skip_configured_section(config, 'tools', 'Tools')):
        setup_tools(config, first_install=not is_existing)
    save_config(config)
    if _backup_path and _backup_path.exists():
        print_info(f'Previous config backed up to: {_backup_path}')
        print_info('If setup changed a value you customized, restore it with:')
        print_info(f'  cp {_backup_path} {config_path}')
    _print_setup_summary(config, hermes_home)

def _run_first_time_quick_setup(config: dict, hermes_home, is_existing: bool):
    """Streamlined first-time setup via Nous Portal: OAuth, model, terminal & messaging.

    Routes straight to the Nous Portal provider — runs the device-code OAuth
    login, picks a Nous model, then configures the terminal backend and (optionally)
    a messaging platform. Applies sensible defaults for everything else (agent
    settings, tools); the user can customize later via ``duck-agent setup <section>``
    or switch providers with ``duck-agent model``.
    """
    from hermes_cli.config import load_config
    print()
    print_header('Nous Portal')
    print_info('One subscription, 300+ models, plus the Tool Gateway:')
    print_info('  web search, image generation, TTS, browser automation.')
    print_info('Sign up: https://portal.nousresearch.com/manage-subscription')
    print()
    try:
        from hermes_cli.main import _model_flow_nous
        _model_flow_nous(config)
    except (KeyboardInterrupt, EOFError):
        print()
        print_info('Nous Portal setup cancelled.')
    except Exception as exc:
        logger.debug('_model_flow_nous error during quick setup: %s', exc)
        print_warning(f'Nous Portal setup encountered an error: {exc}')
        print_info('You can try again later with: duck-agent model')
    _refreshed = load_config()
    config.clear()
    config.update(_refreshed)
    setup_terminal_backend(config)
    _apply_default_agent_settings(config)
    save_config(config)
    print()
    gateway_choice = prompt_choice('Connect a messaging platform? (Telegram, Discord, etc.)', ['Set up messaging now (recommended)', "Skip — set up later with 'duck-agent setup gateway'"], 0)
    if gateway_choice == 0:
        setup_gateway(config)
        save_config(config)
    print()
    print_success("Setup complete! You're ready to go.")
    print()
    print_info('  Configure all settings:    duck-agent setup')
    if gateway_choice != 0:
        print_info('  Connect Telegram/Discord:  duck-agent setup gateway')
    print()
    _print_setup_summary(config, hermes_home)

def _blank_slate_minimal_toolsets(config: dict):
    """Write the minimal toolset state for a Blank Slate install.

    Only ``file`` and ``terminal`` are enabled. Two layers enforce this:

    1. ``platform_toolsets["cli"] = ["file", "terminal"]`` — an explicit list of
       configurable keys, which the resolver treats as authoritative
       (``has_explicit_config``) so default toolsets aren't re-expanded.
    2. ``agent.disabled_toolsets`` — a global hard-suppression list (applied last
       in ``_get_platform_tools``, overriding every other path including the
       non-configurable platform-toolset recovery that would otherwise re-add
       toolsets like ``kanban``). We list every known toolset except the two we
       keep, guaranteeing a true blank slate regardless of platform/recovery
       quirks. The user re-enables any of them later via ``duck-agent tools`` (which
       rewrites ``platform_toolsets``) or by editing ``agent.disabled_toolsets``.
    """
    keep = {'file', 'terminal'}
    config.setdefault('platform_toolsets', {})['cli'] = sorted(keep)
    try:
        from toolsets import TOOLSETS
        from hermes_cli.tools_config import CONFIGURABLE_TOOLSETS, _get_plugin_toolset_keys
        all_keys = set()
        all_keys.update((k for k, _, _ in CONFIGURABLE_TOOLSETS))
        all_keys.update(_get_plugin_toolset_keys())
        for k, tdef in TOOLSETS.items():
            if k.startswith('duck-agent-'):
                continue
            if isinstance(tdef, dict) and tdef.get('includes'):
                continue
            if isinstance(tdef, dict) and tdef.get('posture'):
                continue
            all_keys.add(k)
        disabled = sorted(all_keys - keep)
        if disabled:
            config.setdefault('agent', {})['disabled_toolsets'] = disabled
    except Exception as exc:
        logger.debug('blank-slate disabled_toolsets computation skipped: %s', exc)

def _blank_slate_minimize_config(config: dict):
    """Turn OFF the optional config features for a Blank Slate install.

    Everything here is opt-in afterwards via ``duck-agent setup agent`` /
    ``duck-agent config set``. We keep only what's needed to run.
    """
    config.setdefault('agent', {})['max_turns'] = 90
    config.setdefault('compression', {})['enabled'] = False
    mem = config.setdefault('memory', {})
    mem['memory_enabled'] = False
    mem['user_profile_enabled'] = False
    config.setdefault('checkpoints', {})['enabled'] = False
    config.setdefault('smart_model_routing', {})['enabled'] = False
    config.setdefault('session_reset', {})['mode'] = 'none'
    config.setdefault('display', {})['tool_progress'] = 'all'

def _run_blank_slate_setup(config: dict, hermes_home, is_existing: bool):
    """Blank Slate setup — start with everything off except the bare minimum.

    Forces only the essentials to run an agent (provider + model, the file and
    terminal toolsets) and turns every other tool/skill/plugin/MCP/config
    feature OFF. After applying that minimal baseline, the user chooses one of
    two paths:

      1. Start with everything disabled — finish now with the minimal agent.
      2. Walk through every configuration — opt each capability back in.

    Either way nothing is enabled that the user did not explicitly choose.
    """
    print()
    print_header('Blank Slate Setup')
    print_info("Everything starts OFF. First we force-enable only what's required")
    print_info('to run an agent, then you choose whether to stop there or walk')
    print_info('through enabling more — opting in to exactly what you want.')
    print_info('')
    print_info('Forced on: Provider & Model, File Operations, Terminal.')
    print_info('Everything else (web, browser, code exec, vision, memory,')
    print_info('delegation, cron, skills, plugins, MCP, …) starts disabled.')
    print()
    print_header('Step 1 — Provider & Model (required)')
    setup_model_provider(config)
    save_config(config)
    print_header('Step 2 — Terminal Backend')
    setup_terminal_backend(config)
    _blank_slate_minimal_toolsets(config)
    _blank_slate_minimize_config(config)
    save_config(config)
    print()
    print_success('Minimal baseline applied:')
    print_info('  Toolsets: file, terminal (everything else off)')
    print_info('  Compression, memory, checkpoints, smart routing: off')
    print()
    print_header('How far do you want to go?')
    path = prompt_choice('Your minimal agent is ready. What next?', ['Start with everything disabled — finish now (most minimal)', 'Walk through all configurations — opt in to tools, skills, plugins, MCP'], 0)
    if path == 0:
        save_config(config)
        try:
            from tools.skills_sync import set_bundled_skills_opt_out
            set_bundled_skills_opt_out(True)
        except Exception as exc:
            logger.debug('blank-slate skill opt-out error: %s', exc)
        print()
        print_success('Blank Slate setup complete — minimal agent ready.')
        print_info('Enable anything later, on demand:')
        print_info('  Enable tools:        duck-agent tools')
        print_info('  Seed skills:         duck-agent skills opt-in --sync')
        print_info('  Add MCP servers:     duck-agent mcp add')
        print_info('  Enable plugins:      duck-agent plugins')
        print_info('  Tune agent settings: duck-agent setup agent')
        print()
        _print_setup_summary(config, hermes_home)
        return
    _blank_slate_walkthrough(config, hermes_home)

def _blank_slate_walkthrough(config: dict, hermes_home):
    """Opt-in walkthrough for Blank Slate: skills, tools, plugins, MCP, gateway."""
    from hermes_cli.config import load_config
    print()
    print_header('Bundled Skills')
    print_info('Blank Slate ships with NO bundled skills by default.')
    seed_skills = prompt_yes_no('Seed the full bundled skill catalog? (No = start with zero skills)', default=False)
    try:
        from tools.skills_sync import set_bundled_skills_opt_out, sync_skills
        if seed_skills:
            set_bundled_skills_opt_out(False)
            result = sync_skills(quiet=True)
            copied = len(result.get('copied', [])) if isinstance(result, dict) else 0
            print_success(f'Seeded {copied} bundled skills.')
        else:
            set_bundled_skills_opt_out(True)
            print_info('No skills seeded. A .no-bundled-skills marker keeps future')
            print_info('`duck-agent update` runs from re-injecting them. Opt back in any')
            print_info('time with `duck-agent skills opt-in --sync`.')
    except Exception as exc:
        logger.debug('blank-slate skill handling error: %s', exc)
        print_warning(f'Skill setup step encountered an error: {exc}')
    print()
    print_header('Tools')
    print_info('Pick exactly which additional toolsets to turn on.')
    print_info('(file and terminal are already on; leave the rest off if you want')
    print_info(' the most minimal agent.)')
    if prompt_yes_no('Open the tool selector to enable more tools?', default=False):
        try:
            from hermes_cli.tools_config import tools_command
            tools_command(first_install=False, config=config)
            _refreshed = load_config()
            config.clear()
            config.update(_refreshed)
        except Exception as exc:
            logger.debug('blank-slate tools_command error: %s', exc)
            print_warning(f'Tool selector encountered an error: {exc}')
    else:
        print_info('Keeping the minimal toolset. Add tools later with `duck-agent tools`.')
    print()
    print_header('Plugins')
    if prompt_yes_no('Review and enable built-in plugins now?', default=False):
        print_info('Manage plugins with `duck-agent plugins list` / `duck-agent plugins install`.')
    else:
        print_info('No plugins enabled. Add later with `duck-agent plugins`.')
    print()
    print_header('MCP Servers')
    if prompt_yes_no('Add an MCP server now?', default=False):
        print_info('Add servers with `duck-agent mcp add <name> --url ... | --command ...`.')
    else:
        print_info('No MCP servers configured. Add later with `duck-agent mcp add`.')
    print()
    if prompt_yes_no('Connect a messaging platform (Telegram, Discord, …)?', default=False):
        setup_gateway(config)
    save_config(config)
    print()
    print_success('Blank Slate setup complete — minimal agent ready.')
    print_info('  Enable more tools:   duck-agent tools')
    print_info('  Seed skills:         duck-agent skills opt-in --sync')
    print_info('  Add MCP servers:     duck-agent mcp add')
    print_info('  Tune agent settings: duck-agent setup agent')
    print()
    _print_setup_summary(config, hermes_home)

def _run_quick_setup(config: dict, hermes_home):
    """Quick setup — only configure items that are missing."""
    from hermes_cli.config import get_missing_env_vars, get_missing_config_fields, check_config_version
    print()
    print_header('Quick Setup — Missing Items Only')
    missing_required = [v for v in get_missing_env_vars(required_only=False) if v.get('is_required')]
    missing_optional = [v for v in get_missing_env_vars(required_only=False) if not v.get('is_required')]
    missing_config = get_missing_config_fields()
    current_ver, latest_ver = check_config_version()
    has_anything_missing = missing_required or missing_optional or missing_config or (current_ver < latest_ver)
    if not has_anything_missing:
        print_success('Everything is configured! Nothing to do.')
        print()
        print_info("Run 'duck-agent setup' and choose 'Full Setup' to reconfigure,")
        print_info('or pick a specific section from the menu.')
        return
    if missing_required:
        print()
        print_info(f'{len(missing_required)} required setting(s) missing:')
        for var in missing_required:
            print(f"     • {var['name']}")
        print()
        for var in missing_required:
            print()
            print(color(f"  {var['name']}", Colors.CYAN))
            print_info(f"  {var.get('description', '')}")
            if var.get('url'):
                print_info(f"  Get key at: {var['url']}")
            if var.get('password'):
                value = prompt(f"  {var.get('prompt', var['name'])}", password=True)
            else:
                value = prompt(f"  {var.get('prompt', var['name'])}")
            if value:
                save_env_value(var['name'], value)
                print_success(f"  Saved {var['name']}")
            else:
                print_warning(f"  Skipped {var['name']}")
    missing_tools = [v for v in missing_optional if v.get('category') == 'tool']
    missing_messaging = [v for v in missing_optional if v.get('category') == 'messaging' and (not v.get('advanced'))]
    if missing_tools:
        print()
        print_header('Tool API Keys')
        checklist_labels = []
        for var in missing_tools:
            tools = var.get('tools', [])
            tools_str = f" → {', '.join(tools[:2])}" if tools else ''
            checklist_labels.append(f"{var.get('description', var['name'])}{tools_str}")
        selected_indices = prompt_checklist('Which tools would you like to configure?', checklist_labels)
        for idx in selected_indices:
            var = missing_tools[idx]
            _prompt_api_key(var)
    if missing_messaging:
        print()
        print_header('Messaging Platforms')
        print_info('Connect Duck Agent to messaging apps to chat from anywhere.')
        print_info("You can configure these later with 'duck-agent setup gateway'.")
        platform_order = []
        platforms = {}
        for var in missing_messaging:
            name = var['name']
            if 'TELEGRAM' in name:
                plat = 'Telegram'
            elif 'DISCORD' in name:
                plat = 'Discord'
            elif 'SLACK' in name:
                plat = 'Slack'
            else:
                continue
            if plat not in platforms:
                platform_order.append(plat)
            platforms.setdefault(plat, []).append(var)
        platform_labels = [{'Telegram': '📱 Telegram', 'Discord': '💬 Discord', 'Slack': '💼 Slack'}.get(p, p) for p in platform_order]
        selected_indices = prompt_checklist('Which platforms would you like to set up?', platform_labels)
        for idx in selected_indices:
            plat = platform_order[idx]
            vars_list = platforms[plat]
            emoji = {'Telegram': '📱', 'Discord': '💬', 'Slack': '💼'}.get(plat, '')
            print()
            print(color(f'  ─── {emoji} {plat} ───', Colors.CYAN))
            print()
            for var in vars_list:
                print_info(f"  {var.get('description', '')}")
                if var.get('url'):
                    print_info(f"  {var['url']}")
                if var.get('password'):
                    value = prompt(f"  {var.get('prompt', var['name'])}", password=True)
                else:
                    value = prompt(f"  {var.get('prompt', var['name'])}")
                if value:
                    save_env_value(var['name'], value)
                    print_success('  ✓ Saved')
                else:
                    print_warning('  Skipped')
                print()
    if missing_config:
        print()
        print_info(f'Adding {len(missing_config)} new config option(s) with defaults...')
        for field in missing_config:
            print_success(f"  Added {field['key']} = {field['default']}")
        config['_config_version'] = latest_ver
        save_config(config)
    _print_setup_summary(config, hermes_home)