"""``duck-agent logs`` subcommand parser.

Extracted verbatim from ``hermes_cli/main.py:main()`` (god-file Phase 2).
Handler injected to avoid importing ``main``.
"""
from __future__ import annotations
import argparse
from typing import Callable

def build_logs_parser(subparsers, *, cmd_logs: Callable) -> None:
    """Attach the ``logs`` subcommand to ``subparsers``."""
    logs_parser = subparsers.add_parser('logs', help='View and filter Duck Agent log files', description='View, tail, and filter agent.log / errors.log / gateway.log / gui.log / desktop.log', formatter_class=argparse.RawDescriptionHelpFormatter, epilog='Examples:\n    duck-agent logs                    Show last 50 lines of agent.log\n    duck-agent logs -f                 Follow agent.log in real time\n    duck-agent logs errors             Show last 50 lines of errors.log\n    duck-agent logs gateway -n 100     Show last 100 lines of gateway.log\n    duck-agent logs gui -f             Follow gui.log in real time\n    duck-agent logs desktop -f         Follow desktop.log (Electron app boot/backend)\n    duck-agent logs --level WARNING    Only show WARNING and above\n    duck-agent logs --session abc123   Filter by session ID\n    duck-agent logs --component tools  Only show tool-related lines\n    duck-agent logs --since 1h         Lines from the last hour\n    duck-agent logs --since 30m -f     Follow, starting from 30 min ago\n    duck-agent logs list               List available log files with sizes\n')
    logs_parser.add_argument('log_name', nargs='?', default='agent', help="Log to view: agent (default), errors, gateway, gui, or 'list' to show available files")
    logs_parser.add_argument('-n', '--lines', type=int, default=50, help='Number of lines to show (default: 50)')
    logs_parser.add_argument('-f', '--follow', action='store_true', help='Follow the log in real time (like tail -f)')
    logs_parser.add_argument('--level', metavar='LEVEL', help='Minimum log level to show (DEBUG, INFO, WARNING, ERROR)')
    logs_parser.add_argument('--session', metavar='ID', help='Filter lines containing this session ID substring')
    logs_parser.add_argument('--since', metavar='TIME', help='Show lines since TIME ago (e.g. 1h, 30m, 2d)')
    logs_parser.add_argument('--component', metavar='NAME', help='Filter by component: gateway, agent, tools, cli, cron, gui')
    logs_parser.set_defaults(func=cmd_logs)