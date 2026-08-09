"""Shell completion script generation for duck-agent CLI.

Walks the live argparse parser tree to generate accurate, always-up-to-date
completion scripts — no hardcoded subcommand lists, no extra dependencies.

Supports bash, zsh, and fish.
"""
from __future__ import annotations
import argparse
from typing import Any

def _walk(parser: argparse.ArgumentParser) -> dict[str, Any]:
    """Recursively extract subcommands and flags from a parser.

    Uses _SubParsersAction._choices_actions to get canonical names (no aliases)
    along with their help text.
    """
    flags: list[str] = []
    subcommands: dict[str, Any] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            seen: set[str] = set()
            for pseudo in action._choices_actions:
                name = pseudo.dest
                if name in seen:
                    continue
                seen.add(name)
                subparser = action.choices.get(name)
                if subparser is None:
                    continue
                info = _walk(subparser)
                info['help'] = _clean(pseudo.help or '')
                subcommands[name] = info
        elif action.option_strings:
            flags.extend((o for o in action.option_strings if o.startswith('-')))
    return {'flags': flags, 'subcommands': subcommands}

def _clean(text: str, maxlen: int=60) -> str:
    """Strip shell-unsafe characters and truncate."""
    return text.replace("'", '').replace('"', '').replace('\\', '')[:maxlen]

def generate_bash(parser: argparse.ArgumentParser) -> str:
    tree = _walk(parser)
    top_cmds = ' '.join(sorted(tree['subcommands']))
    cases: list[str] = []
    for cmd in sorted(tree['subcommands']):
        info = tree['subcommands'][cmd]
        if cmd == 'profile' and info['subcommands']:
            subcmds = ' '.join(sorted(info['subcommands']))
            profile_actions = 'use delete show alias rename export'
            cases.append(f'''        profile)\n            case "$prev" in\n                profile)\n                    COMPREPLY=($(compgen -W "{subcmds}" -- "$cur"))\n                    return\n                    ;;\n                {profile_actions.replace(' ', '|')})\n                    COMPREPLY=($(compgen -W "$(_hermes_profiles)" -- "$cur"))\n                    return\n                    ;;\n            esac\n            ;;''')
        elif info['subcommands']:
            subcmds = ' '.join(sorted(info['subcommands']))
            cases.append(f'        {cmd})\n            COMPREPLY=($(compgen -W "{subcmds}" -- "$cur"))\n            return\n            ;;')
        elif info['flags']:
            flags = ' '.join(info['flags'])
            cases.append(f'        {cmd})\n            COMPREPLY=($(compgen -W "{flags}" -- "$cur"))\n            return\n            ;;')
    cases_str = '\n'.join(cases)
    return f'# Duck Agent bash completion\n# Add to ~/.bashrc:\n#   eval "$(duck-agent completion bash)"\n\n_hermes_profiles() {{\n    local profiles_dir="$HOME/.duck-agent/profiles"\n    local profiles="default"\n    if [ -d "$profiles_dir" ]; then\n        for f in "$profiles_dir"/*/; do\n            [ -d "$f" ] && profiles="$profiles $(basename "$f")"\n        done\n    fi\n    echo "$profiles"\n}}\n\n_hermes_completion() {{\n    local cur prev\n    COMPREPLY=()\n    cur="${{COMP_WORDS[COMP_CWORD]}}"\n    prev="${{COMP_WORDS[COMP_CWORD-1]}}"\n\n    # Complete profile names after -p / --profile\n    if [[ "$prev" == "-p" || "$prev" == "--profile" ]]; then\n        COMPREPLY=($(compgen -W "$(_hermes_profiles)" -- "$cur"))\n        return\n    fi\n\n    if [[ $COMP_CWORD -ge 2 ]]; then\n        case "${{COMP_WORDS[1]}}" in\n{cases_str}\n        esac\n    fi\n\n    if [[ $COMP_CWORD -eq 1 ]]; then\n        COMPREPLY=($(compgen -W "{top_cmds}" -- "$cur"))\n    fi\n}}\n\ncomplete -F _hermes_completion duck-agent\n'

def generate_zsh(parser: argparse.ArgumentParser) -> str:
    tree = _walk(parser)
    top_cmds_lines: list[str] = []
    for cmd in sorted(tree['subcommands']):
        help_text = _clean(tree['subcommands'][cmd].get('help', ''))
        top_cmds_lines.append(f"                '{cmd}:{help_text}'")
    top_cmds_str = '\n'.join(top_cmds_lines)
    sub_cases: list[str] = []
    for cmd in sorted(tree['subcommands']):
        info = tree['subcommands'][cmd]
        if not info['subcommands']:
            continue
        if cmd == 'profile':
            sub_lines: list[str] = []
            for sc in sorted(info['subcommands']):
                sh = _clean(info['subcommands'][sc].get('help', ''))
                sub_lines.append(f"                        '{sc}:{sh}'")
            sub_str = '\n'.join(sub_lines)
            sub_cases.append(f"                profile)\n                    case ${{line[2]}} in\n                        use|delete|show|alias|rename|export)\n                            _hermes_profiles\n                            ;;\n                        *)\n                            local -a profile_cmds\n                            profile_cmds=(\n{sub_str}\n                            )\n                            _describe 'profile command' profile_cmds\n                            ;;\n                    esac\n                    ;;")
        else:
            sub_lines = []
            for sc in sorted(info['subcommands']):
                sh = _clean(info['subcommands'][sc].get('help', ''))
                sub_lines.append(f"                    '{sc}:{sh}'")
            sub_str = '\n'.join(sub_lines)
            safe = cmd.replace('-', '_')
            sub_cases.append(f"                {cmd})\n                    local -a {safe}_cmds\n                    {safe}_cmds=(\n{sub_str}\n                    )\n                    _describe '{cmd} command' {safe}_cmds\n                    ;;")
    sub_cases_str = '\n'.join(sub_cases)
    return f"""#compdef duck-agent\n# Duck Agent zsh completion\n# Add to ~/.zshrc:\n#   eval "$(duck-agent completion zsh)"\n\n_hermes_profiles() {{\n    local -a profiles\n    profiles=(default)\n    if [[ -d "$HOME/.duck-agent/profiles" ]]; then\n        profiles+=($HOME/.duck-agent/profiles/*(N/:t))\n    fi\n    _describe 'profile' profiles\n}}\n\n_hermes() {{\n    local context state line\n    typeset -A opt_args\n\n    _arguments -C \\\n        '(-)'{{-h,--help}}'[Show help and exit]' \\\n        '(-)'{{-V,--version}}'[Show version and exit]' \\\n        '(-)'{{-p,--profile}}'[Profile name]:profile:_hermes_profiles' \\\n        '1:command:->commands' \\\n        '*::arg:->args'\n\n    case $state in\n        commands)\n            local -a subcmds\n            subcmds=(\n{top_cmds_str}\n            )\n            _describe 'duck-agent command' subcmds\n            ;;\n        args)\n            case ${{line[1]}} in\n{sub_cases_str}\n            esac\n            ;;\n    esac\n}}\n\ncompdef _hermes duck-agent\n"""

def generate_fish(parser: argparse.ArgumentParser) -> str:
    tree = _walk(parser)
    top_cmds = sorted(tree['subcommands'])
    top_cmds_str = ' '.join(top_cmds)
    lines: list[str] = ['# Duck Agent fish completion', '# Add to your config:', '#   duck-agent completion fish | source', '', '# Helper: list available profiles', 'function __hermes_profiles', '    echo default', '    if test -d $HOME/.duck-agent/profiles', '        for d in $HOME/.duck-agent/profiles/*/', '            basename $d', '        end', '    end', 'end', '', '# Disable file completion by default', 'complete -c duck-agent -f', '', '# Complete profile names after -p / --profile', "complete -c duck-agent -f -s p -l profile -d 'Profile name' -xa '(__hermes_profiles)'", '', '# Top-level subcommands']
    for cmd in top_cmds:
        info = tree['subcommands'][cmd]
        help_text = _clean(info.get('help', ''))
        lines.append(f"complete -c duck-agent -f -n 'not __fish_seen_subcommand_from {top_cmds_str}' -a {cmd} -d '{help_text}'")
    lines.append('')
    lines.append('# Subcommand completions')
    profile_name_actions = {'use', 'delete', 'show', 'alias', 'rename', 'export'}
    for cmd in top_cmds:
        info = tree['subcommands'][cmd]
        if not info['subcommands']:
            continue
        lines.append(f'# {cmd}')
        for sc in sorted(info['subcommands']):
            sinfo = info['subcommands'][sc]
            sh = _clean(sinfo.get('help', ''))
            lines.append(f"complete -c duck-agent -f -n '__fish_seen_subcommand_from {cmd}' -a {sc} -d '{sh}'")
        if cmd == 'profile':
            for action in sorted(profile_name_actions):
                lines.append(f"complete -c duck-agent -f -n '__fish_seen_subcommand_from {action}; and __fish_seen_subcommand_from profile' -a '(__hermes_profiles)' -d 'Profile name'")
    lines.append('')
    return '\n'.join(lines)