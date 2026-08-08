# Duck Agent

Duck Agent is an experimental multi-backend AI agent project focused on giving one desktop/CLI shell access to different agent harnesses.

The repository is currently under active development. The backend-selection layer and test infrastructure are present, while parts of the desktop application and individual backend integrations are still being built out.

## What it is

Duck Agent is designed around a simple idea: keep the user-facing agent shell independent from any single model provider or agent harness.

Current backend names exposed by the launcher are:

- **Grok Build** — the default backend
- **Hermes-Compatible** — compatibility mode for Hermes-style agent workflows
- **Prime Agent** — Prime Intellect / RLM-oriented backend mode

Backend selection is handled through the `DUCK_AGENT_BACKEND` environment variable.

## Repository layout

```text
Duck-Agent/
├── apps/
│   └── desktop/          # Electron/React desktop application work
├── backends/             # TypeScript backend/orchestration work
├── duck_agent/           # Python backend-selection module
├── tests/                # Test suite
├── duck-agent            # Shell launcher
├── run_all_tests.sh      # Test runner
├── run_tests.py          # Python test entry point
└── README.md
```

## Quick start

Clone the repository and enter it:

```bash
git clone https://github.com/Franzferdinan51/Duck-Agent.git
cd Duck-Agent
```

Make the launcher executable if needed:

```bash
chmod +x ./duck-agent
```

See the available launcher options:

```bash
./duck-agent --help
./duck-agent --backends
./duck-agent --status
```

Launch with the default backend:

```bash
./duck-agent
```

Or select a backend explicitly:

```bash
DUCK_AGENT_BACKEND=grok-build ./duck-agent
DUCK_AGENT_BACKEND=hermes-compatible ./duck-agent
DUCK_AGENT_BACKEND=prime-agent ./duck-agent
```

## Environment variables

| Variable | Purpose |
| --- | --- |
| `DUCK_AGENT_BACKEND` | Selects the active backend. Defaults to `grok-build`. |
| `GROK_API_KEY` | API credential used by Grok-related backend work when required. |
| `GROK_MODEL` | Grok model selection used by the launcher/status output. |

> **Note:** Backend integration is still evolving. Some backend modes currently provide selection/initialization scaffolding rather than a complete production agent runtime.

## Desktop app

Desktop work lives under [`apps/desktop`](apps/desktop).

The desktop package currently targets:

- Electron
- React
- TypeScript
- Vite
- Node.js 22.22+

See [`apps/desktop/README.md`](apps/desktop/README.md) for desktop-specific development notes.

## Backends

Backend/orchestration code lives under [`backends`](backends), while the Python launcher-facing selector is implemented in [`duck_agent/backends.py`](duck_agent/backends.py).

See [`backends/README.md`](backends/README.md) for the backend architecture and current status.

## Testing

The repository includes several test entry points. Depending on what you are changing, useful commands include:

```bash
python3 run_tests.py
```

```bash
./run_all_tests.sh
```

There are also backend/integration tests in the repository root and under `tests/` / `backends/tests/`.

The project is under active development, so CI may temporarily fail while backend and desktop work is being integrated.

## Project status

Duck Agent is **experimental / work in progress**. Interfaces, directory structure, backend behavior, and setup steps may change quickly.

Current priorities include:

- stabilizing backend selection and orchestration
- improving Grok Build integration
- Hermes-compatible workflows
- MCP/tool integration
- desktop application integration
- test and CI reliability

## Contributing

Issues, experiments, fixes, documentation improvements, and pull requests are welcome. For larger changes, keep backend-specific logic isolated so Duck Agent can remain provider/harness agnostic.

## Repository

https://github.com/Franzferdinan51/Duck-Agent
