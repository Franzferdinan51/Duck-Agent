# Duck Agent Desktop

<p align="center">
  <img src="assets/icon.png" alt="Duck Agent" width="128">
</p>

<p align="center">
  <strong>Desktop shell for Duck Agent</strong>
</p>

Duck Agent Desktop is the native desktop application for Duck Agent, powered by Grok Build. It provides a full-featured Electron-based interface with support for multiple agent backends.

## Features

- **Native Desktop Experience**: Full Electron integration with system tray, notifications, and native menus
- **Multiple Backends**: Choose between Grok Build, Hermes-Compatible, and Prime Agent backends
- **Terminal Integration**: Built-in terminal with PTY support
- **Cross-Platform**: macOS, Windows, and Linux support

## Installation

### From Source

```bash
npm install
npm run build
npm start
```

### Pre-built Releases

Download from the [Duck Agent releases page](https://github.com/Franzferdinan51/Duck-Agent/releases).

## Development

```bash
# Start development mode with hot reload
npm run dev

# Run tests
npm test

# Run E2E tests
npm run test:e2e
```

## Backend Selection

Duck Agent Desktop supports multiple agent backends:

| Backend | Description |
|---------|-------------|
| Grok Build (default) | Primary harness with full capabilities |
| Hermes-Compatible | Hermes agent compatibility mode |
| Prime Agent | Prime Intellect's RLM agent |

Set via `DUCK_AGENT_BACKEND` environment variable or in-app settings.

## Architecture

```
apps/desktop/
├── electron/     # Electron main process
├── src/          # React frontend
│   ├── app/     # Application shell
│   ├── components/  # UI components
│   ├── hooks/   # React hooks
│   └── store/   # State management
├── scripts/      # Build scripts
└── e2e/         # End-to-end tests
```

## License

MIT License
