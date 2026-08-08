# Duck Agent Backends

Duck Agent supports multiple agent backends, allowing you to choose the best harness for your use case.

## Supported Backends

### Grok Build (Default)
**Primary harness** - Full Grok Build integration with:
- Advanced agent orchestration
- MCP tool support
- Multi-model support
- Desktop integration

```bash
DUCK_AGENT_BACKEND=grok-build ./duck-agent
```

### Hermes-Compatible
Hermes Agent compatibility mode for users who want to use Hermes features while running Duck Agent as the shell.

```bash
DUCK_AGENT_BACKEND=hermes-compatible ./duck-agent
```

### Prime Agent
Prime Intellect's RLM (Recursive Language Model) agent integration.

```bash
DUCK_AGENT_BACKEND=prime-agent ./duck-agent
```

## Architecture

```
Duck-Agent/
├── backends/
│   ├── grok-build/     # Grok Build harness integration
│   ├── hermes/         # Hermes compatibility layer
│   └── prime-agent/    # Prime Agent integration
```

## Adding a New Backend

1. Create a new directory under `backends/`
2. Implement the backend interface
3. Update the launcher script
4. Add backend selection UI

## Backend Interface

Each backend must implement:

```typescript
interface DuckAgentBackend {
  name: string
  start(): Promise<void>
  stop(): Promise<void>
  sendMessage(message: string): Promise<string>
  getStatus(): BackendStatus
}
```
