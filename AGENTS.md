# Duck Agent Development Guide

## Overview

Duck Agent is a self-improving AI agent powered by **Grok Build** as the primary harness, with support for alternative backends including Hermes-compatible mode and Prime Agent integration.

## Architecture

Duck Agent combines three major technologies:

1. **Grok Build** (Primary Harness): The main agent orchestration system
2. **Hermes Desktop**: Electron-based desktop shell for platform integration
3. **Prime Agent** (Optional Backend): RLM-based recursive language model agent

## Backend Selection

Duck Agent supports multiple backend modes:

- `grok-build` (default): Full Grok Build harness integration
- `hermes-compatible`: Hermes Agent compatibility mode  
- `prime-agent`: Prime Intellect's RLM-based agent

## Development Rules

### Code Style
- TypeScript with strict mode enabled
- No `any` types unless absolutely necessary
- Top-level imports only (no dynamic imports for types)
- All keybindings must be configurable

### File Organization
```
apps/desktop/     # Electron desktop application
  ├── src/        # React frontend
  ├── electron/   # Electron main process
  └── scripts/    # Build scripts
packages/         # Shared packages
prime-agent-packages/  # Prime Agent integration
```

### Testing
- Run `npm run check` before committing
- All TypeScript errors must be fixed
- Tests must pass for new functionality

## Agent Capabilities

Duck Agent extends Grok Build with:
- Desktop platform integration via Electron
- Multi-backend support (Grok, Hermes, Prime)
- Persistent session management
- Skill creation and learning
- Cross-platform deployment (macOS, Windows, Linux)

## Contributing

See CONTRIBUTING.md for detailed guidelines.
