# Contributing to Duck Agent

Thank you for your interest in contributing to Duck Agent!

## Development Setup

1. Clone the repository
```bash
git clone https://github.com/Franzferdinan51/Duck-Agent.git
cd Duck-Agent
```

2. Install dependencies
```bash
cd apps/desktop
npm install
```

3. Run development mode
```bash
npm run dev
```

## Backend Modes

Duck Agent supports three backend modes:

### Grok Build (Default)
Primary harness with full Grok Build capabilities.
```bash
duck-agent --backend grok-build
```

### Hermes-Compatible
Full Hermes Agent compatibility mode.
```bash
duck-agent --backend hermes-compatible
```

### Prime Agent
Prime Intellect's RLM-based agent integration.
```bash
duck-agent --backend prime-agent
```

## Code Standards

- Run `npm run check` before committing
- All TypeScript errors must be resolved
- Tests must pass for new functionality
- No breaking changes without deprecation warnings

## Pull Requests

1. Create a feature branch from `main`
2. Make your changes
3. Run tests and type checks
4. Submit a PR to the `main` branch

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
