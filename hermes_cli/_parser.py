"""
Top-level argparse construction for the duck-agent CLI.

Lives in its own module so other modules (e.g. ``relaunch.py``) can
introspect the parser to discover which flags exist without running the
``main`` fn.

Only the top-level parser and the ``chat`` subparser live here. Every other
subparser (model, gateway, sessions, …) is built inline in ``main.py``
because its dispatch is tightly coupled to module-level ``cmd_*`` functions.
"""
import argparse
PRE_ARGPARSE_INHERITED_FLAGS: list[tuple[str, bool]] = [('--profile', True), ('-p', True)]

def _inherited_flag(parser, *args, **kwargs):
    """Register a flag that ``hermes_cli.relaunch`` should carry over when
    the CLI re-execs itself (e.g. after ``sessions browse`` picks a session,
    or after the setup wizard launches chat).

    Equivalent to ``parser.add_argument(...)`` plus tagging the resulting
    Action with ``inherit_on_relaunch = True`` so the relaunch table builder
    can find it via introspection.
    """
    action = parser.add_argument(*args, **kwargs)
    action.inherit_on_relaunch = True
    return action
_EPILOGUE = '\nExamples:\n    duck-agent                        Start interactive chat\n    duck-agent chat -q "Hello"        Single query mode\n    duck-agent --tui                  Launch the modern TUI (or set display.interface: tui)\n    duck-agent --cli                  Force the classic REPL (overrides display.interface: tui)\n    duck-agent -c                     Resume the most recent session\n    duck-agent -c "my project"        Resume a session by name (latest in lineage)\n    duck-agent --resume <session_id>  Resume a specific session by ID\n    duck-agent setup                  Run setup wizard\n    duck-agent logout                 Clear stored authentication\n    duck-agent auth add <provider>    Add a pooled credential\n    duck-agent auth list              List pooled credentials\n    duck-agent auth remove <p> <t>    Remove pooled credential by index, id, or label\n    duck-agent auth reset <provider>  Clear exhaustion status for a provider\n    duck-agent model                  Select default model\n    duck-agent fallback [list]        Show fallback provider chain\n    duck-agent fallback add           Add a fallback provider (same picker as `duck-agent model`)\n    duck-agent fallback remove        Remove a fallback provider from the chain\n    duck-agent config                 View configuration\n    duck-agent config edit            Edit config in $EDITOR\n    duck-agent config set model gpt-4 Set a config value\n    duck-agent gateway                Run messaging gateway\n    duck-agent -s duck-agent-dev,github-auth\n    duck-agent -w                     Start in isolated git worktree\n    duck-agent gateway install        Install gateway background service\n    duck-agent sessions list          List past sessions\n    duck-agent sessions browse        Interactive session picker\n    duck-agent sessions rename ID T   Rename/title a session\n    duck-agent logs                   View agent.log (last 50 lines)\n    duck-agent logs -f                Follow agent.log in real time\n    duck-agent logs errors            View errors.log\n    duck-agent logs --since 1h        Lines from the last hour\n    duck-agent debug share             Upload debug report for support\n    duck-agent console                Open the safe Duck Agent command console\n    duck-agent update                 Update to latest version\n    duck-agent dashboard              Start web UI dashboard (port 9119)\n    duck-agent dashboard --stop       Stop running dashboard processes\n    duck-agent dashboard --status     List running dashboard processes\n\nFor more help on a command:\n    duck-agent <command> --help\n'

def build_top_level_parser():
    """Build the top-level parser, the subparsers action, and the ``chat`` subparser.

    Returns ``(parser, subparsers, chat_parser)``. The caller wires
    ``chat_parser.set_defaults(func=cmd_chat)`` and continues registering
    other subparsers via ``subparsers.add_parser(...)``.
    """
    parser = argparse.ArgumentParser(prog='duck-agent', description='Duck Agent - AI assistant with tool-calling capabilities', formatter_class=argparse.RawDescriptionHelpFormatter, epilog=_EPILOGUE)
    parser.add_argument('--version', '-V', action='store_true', help='Show version and exit')
    parser.add_argument('-z', '--oneshot', metavar='PROMPT', default=None, help='One-shot mode: send a single prompt and print ONLY the final response text to stdout. No banner, no spinner, no tool previews, no session_id line. Tools, memory, rules, and AGENTS.md in the CWD are loaded as normal; approvals are auto-bypassed. Intended for scripts / pipes.')
    parser.add_argument('--usage-file', metavar='PATH', default=None, help='One-shot mode only: after the run, write a JSON usage report (estimated cost, token counts, model, api_calls) to PATH. The report is written even when the run fails, so pipelines can always account for spend. No effect outside -z/--oneshot.')
    _inherited_flag(parser, '-m', '--model', default=None, help='Model override for this invocation (e.g. anthropic/claude-sonnet-4.6). Applies to -z/--oneshot and --tui. Also settable via HERMES_INFERENCE_MODEL env var.')
    _inherited_flag(parser, '--provider', default=None, help='Provider override for this invocation (e.g. openrouter, anthropic). Applies to -z/--oneshot and --tui. The persistent provider lives in config.yaml under model.provider — use `duck-agent setup` or edit the file to change it.')
    _inherited_flag(parser, '--reasoning', default=None, metavar='LEVEL', help='Reasoning effort for this invocation: none, minimal, low, medium, high, xhigh, max, or ultra. Overrides agent.reasoning_effort in config.yaml for this run only; the persistent level lives there (or per-model under agent.reasoning_overrides).')
    parser.add_argument('-t', '--toolsets', default=None, help='Comma-separated toolsets to enable for this invocation. Applies to -z/--oneshot and --tui.')
    parser.add_argument('--resume', '-r', metavar='SESSION', default=None, help='Resume a previous session by ID or title')
    parser.add_argument('--no-restore-cwd', action='store_true', default=False, help="Don't cd into a resumed session's recorded working directory.")
    parser.add_argument('--continue', '-c', dest='continue_last', nargs='?', const=True, default=None, metavar='SESSION_NAME', help='Resume a session by name, or the most recent if no name given')
    parser.add_argument('--worktree', '-w', action='store_true', default=False, help='Run in an isolated git worktree (for parallel agents)')
    _inherited_flag(parser, '--accept-hooks', action='store_true', default=False, help="Auto-approve any unseen shell hooks declared in config.yaml without a TTY prompt.  Equivalent to HERMES_ACCEPT_HOOKS=1 or hooks_auto_accept: true in config.yaml.  Use on CI / headless runs that can't prompt.")
    _inherited_flag(parser, '--skills', '-s', action='append', default=None, help='Preload one or more skills for the session (repeat flag or comma-separate)')
    _inherited_flag(parser, '--yolo', action='store_true', default=False, help='Bypass all dangerous command approval prompts (use at your own risk)')
    _inherited_flag(parser, '--pass-session-id', action='store_true', default=False, help="Include the session ID in the agent's system prompt")
    _inherited_flag(parser, '--ignore-user-config', action='store_true', default=False, help='Ignore ~/.duck-agent/config.yaml and fall back to built-in defaults (credentials in .env are still loaded)')
    _inherited_flag(parser, '--ignore-rules', action='store_true', default=False, help='Skip auto-injection of AGENTS.md, SOUL.md, .cursorrules, memory, and preloaded skills')
    _inherited_flag(parser, '--safe-mode', action='store_true', default=False, help='Troubleshooting mode: disable ALL customizations — user config, AGENTS.md/memory injection, plugins, and MCP servers (implies --ignore-user-config and --ignore-rules)')
    _inherited_flag(parser, '--tui', action='store_true', default=False, help='Launch the modern TUI instead of the classic REPL')
    _inherited_flag(parser, '--cli', action='store_true', default=False, help='Force the classic prompt_toolkit REPL (overrides display.interface=tui)')
    _inherited_flag(parser, '--dev', dest='tui_dev', action='store_true', default=False, help='With --tui: run TypeScript sources via tsx (skip dist build)')
    subparsers = parser.add_subparsers(dest='command', help='Command to run')
    chat_parser = subparsers.add_parser('chat', help='Interactive chat with the agent', description='Start an interactive chat session with Duck Agent')
    chat_parser.add_argument('-q', '--query', help='Single query (non-interactive mode)')
    chat_parser.add_argument('--image', help='Optional local image path to attach to a single query')
    _inherited_flag(chat_parser, '-m', '--model', default=argparse.SUPPRESS, help='Model to use (e.g., anthropic/claude-sonnet-4)')
    chat_parser.add_argument('-t', '--toolsets', default=argparse.SUPPRESS, help='Comma-separated toolsets to enable')
    _inherited_flag(chat_parser, '--reasoning', default=argparse.SUPPRESS, metavar='LEVEL', help='Reasoning effort for this session: none, minimal, low, medium, high, xhigh, max, or ultra. Overrides agent.reasoning_effort for this run only (same levels as the /reasoning slash command).')
    _inherited_flag(chat_parser, '-s', '--skills', action='append', default=argparse.SUPPRESS, help='Preload one or more skills for the session (repeat flag or comma-separate)')
    _inherited_flag(chat_parser, '--provider', default=argparse.SUPPRESS, help='Inference provider (default: auto). Built-in or a user-defined name from `providers:` in config.yaml.')
    chat_parser.add_argument('-v', '--verbose', action='store_true', default=argparse.SUPPRESS, help='Verbose output')
    chat_parser.add_argument('-Q', '--quiet', action='store_true', help='Quiet mode for programmatic use: suppress banner, spinner, and tool previews. Only output the final response and session info.')
    chat_parser.add_argument('--resume', '-r', metavar='SESSION_ID', default=argparse.SUPPRESS, help='Resume a previous session by ID (shown on exit)')
    chat_parser.add_argument('--no-restore-cwd', action='store_true', default=argparse.SUPPRESS, help="Don't cd into a resumed session's recorded working directory.")
    chat_parser.add_argument('--continue', '-c', dest='continue_last', nargs='?', const=True, default=argparse.SUPPRESS, metavar='SESSION_NAME', help='Resume a session by name, or the most recent if no name given')
    chat_parser.add_argument('--worktree', '-w', action='store_true', default=argparse.SUPPRESS, help='Run in an isolated git worktree (for parallel agents on the same repo)')
    _inherited_flag(chat_parser, '--accept-hooks', action='store_true', default=argparse.SUPPRESS, help='Auto-approve any unseen shell hooks declared in config.yaml without a TTY prompt (see also HERMES_ACCEPT_HOOKS env var and hooks_auto_accept: in config.yaml).')
    chat_parser.add_argument('--checkpoints', action='store_true', default=False, help='Enable filesystem checkpoints before destructive file operations (use /rollback to restore)')
    chat_parser.add_argument('--max-turns', type=int, default=None, metavar='N', help='Maximum tool-calling iterations per conversation turn (default: 500, or agent.max_turns in config)')
    _inherited_flag(chat_parser, '--yolo', action='store_true', default=argparse.SUPPRESS, help='Bypass all dangerous command approval prompts (use at your own risk)')
    _inherited_flag(chat_parser, '--pass-session-id', action='store_true', default=argparse.SUPPRESS, help="Include the session ID in the agent's system prompt")
    _inherited_flag(chat_parser, '--ignore-user-config', action='store_true', default=argparse.SUPPRESS, help='Ignore ~/.duck-agent/config.yaml and fall back to built-in defaults (credentials in .env are still loaded). Useful for isolated CI runs, reproduction, and third-party integrations.')
    _inherited_flag(chat_parser, '--ignore-rules', action='store_true', default=argparse.SUPPRESS, help='Skip auto-injection of AGENTS.md, SOUL.md, .cursorrules, memory, and preloaded skills. Combine with --ignore-user-config for a fully isolated run.')
    _inherited_flag(chat_parser, '--safe-mode', action='store_true', default=argparse.SUPPRESS, help='Troubleshooting mode: disable ALL customizations — user config, AGENTS.md/memory injection, plugins, and MCP servers (implies --ignore-user-config and --ignore-rules). Use to isolate whether a problem comes from your setup or from Duck Agent itself.')
    chat_parser.add_argument('--source', default=None, help="Session source tag for filtering (default: cli). Use 'tool' for third-party integrations that should not appear in user session lists.")
    _inherited_flag(chat_parser, '--tui', action='store_true', default=argparse.SUPPRESS, help='Launch the modern TUI instead of the classic REPL')
    _inherited_flag(chat_parser, '--cli', action='store_true', default=argparse.SUPPRESS, help='Force the classic prompt_toolkit REPL (overrides display.interface=tui)')
    _inherited_flag(chat_parser, '--dev', dest='tui_dev', action='store_true', default=argparse.SUPPRESS, help='With --tui: run TypeScript sources via tsx (skip dist build)')
    return (parser, subparsers, chat_parser)