# Duck Agent Backends

Duck Agent is being built around a multi-backend architecture so the user-facing shell does not have to be permanently tied to one agent harness.

> **Development status:** backend support is actively evolving. The repository currently contains backend-selection and orchestration scaffolding alongside in-progress integrations. The names below describe the intended backend modes; they should not be interpreted as a guarantee that every backend is feature-complete.

## Backend selection

The launcher reads `DUCK_AGENT_BACKEND` and currently recognizes:

```text
grok-build
hermes-compatible
prime-agent
```

Example:

```bash
DUCK_AGENT_BACKEND=grok-build ./duck-agent
```

You can inspect the launcher configuration with:

```bash
./duck-agent --backends
./duck-agent --status
```

## Current backend modes

### Grok Build

The default backend name and primary integration target in the current launcher.

```bash
DUCK_AGENT_BACKEND=grok-build ./duck-agent
```

Grok-related configuration currently exposed by the launcher includes:

```bash
GROK_API_KEY=...
GROK_MODEL=...
```

### Hermes-Compatible

A compatibility mode intended for Hermes-style agent workflows.

```bash
DUCK_AGENT_BACKEND=hermes-compatible ./duck-agent
```

### Prime Agent

An experimental backend mode intended for Prime Intellect / RLM-oriented agent work.

```bash
DUCK_AGENT_BACKEND=prime-agent ./duck-agent
```

## Where the backend code lives

There are currently two relevant layers:

```text
Duck-Agent/
├── backends/                 # TypeScript backend/orchestration implementation work
│   ├── grok-build/
│   ├── mcp/
│   ├── orchestration/
│   ├── sessions/
│   ├── skills/
│   └── tests/
└── duck_agent/
    └── backends.py           # Python launcher-facing backend selector
```

The root `duck-agent` shell script provides the CLI-facing backend switch and delegates to `duck_agent/backends.py`.

## Adding or extending a backend

Because this architecture is still under development, keep new backend-specific behavior isolated from the common shell/orchestration layer where possible.

A backend integration will generally need to provide:

1. A stable backend identifier.
2. Initialization and configuration handling.
3. Message/task execution.
4. Status and error reporting.
5. Tool/MCP integration where supported.
6. Tests covering selection and runtime behavior.
7. Documentation describing required credentials and limitations.

## Design direction

The goal is for Duck Agent to provide a common interface over different agent runtimes while allowing each backend to expose its strengths without forcing backend-specific assumptions throughout the application.

This area of the repository is changing quickly. Treat interfaces and backend names as experimental until the project reaches a stable release.
