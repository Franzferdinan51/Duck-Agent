# Duck Agent Desktop Development Guide

## Overview

Duck Agent Desktop is the Electron-based desktop shell for Duck Agent, providing native platform integration and a polished user interface.

## Architecture

The desktop app consists of:

- **Frontend** (`src/`): React-based UI with TypeScript
- **Electron Main** (`electron/`): Native platform integration
- **Shared** (`../shared/`): Cross-component utilities

## Development

```bash
# Install dependencies
npm install

# Start development mode
npm run dev

# Build for production
npm run build

# Package for distribution
npm run dist
```

## Backend Selection

The desktop app supports multiple agent backends:

1. **Grok Build** (default): Primary harness
2. **Hermes-Compatible**: Hermes agent compatibility
3. **Prime Agent**: RLM-based agent

Select backend via settings or `DUCK_AGENT_BACKEND` environment variable.

## Key Files

- `electron/main.ts` - Main process entry
- `src/main.tsx` - React entry point
- `src/app/` - Main application shell
- `src/components/` - Reusable UI components

## Building

```bash
# macOS
npm run dist:mac

# Windows  
npm run dist:win

# Linux
npm run dist:linux
```

## Testing

```bash
# Run all tests
npm test

# E2E tests
npm run test:e2e
```
