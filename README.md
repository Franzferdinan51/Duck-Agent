# Duck Agent

Duck Agent is a desktop-first autonomous-agent project built on the Hermes desktop foundation, with **Grok Build as its primary agent harness**.

The project is under active development. The long-term goal is an agent that can work toward a user goal through an iterative loop of reasoning, tool use, observation, recovery, and completion—not just return a single chat response.

## Current status

The repository currently includes:

- A `duck-agent` launcher and backend-selection layer.
- A Grok Build backend as the primary path.
- Hermes-compatible and Prime Agent compatibility/experimental paths.
- Backend managers for sessions, skills, workflows, and MCP metadata.
- Focused Python and TypeScript backend tests.
- A Hermes-derived Electron desktop application under `apps/desktop/`.

The complete autonomous runtime is still being built. Durable task state, resumable long-running work, approval policies, full desktop runtime integration, and production-ready tool feedback remain active development areas.

## Architecture

```text
┌──────────────────────────────┐
│ Hermes-derived desktop app  │
│ conversations, workspace,   │
│ settings, terminal, status  │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ Duck Agent runtime            │
│ sessions, tasks, tools, MCP, │
│ skills, workflows, state     │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ Grok Build harness           │
│ reasoning, planning, actions │
│ and iterative execution      │
└──────────────────────────────┘
```

Grok Build is the primary product direction. The other backend paths provide compatibility or experimentation without defining the product around the lowest common denominator.

## Quick start

Clone the repository and inspect the launcher help:

```bash
git clone https://github.com/Franzferdinan51/Duck-Agent.git
cd Duck-Agent
chmod +x ./duck-agent
./duck-agent --help
```

To select the primary backend explicitly:

```bash
DUCK_AGENT_BACKEND=grok-build ./duck-agent
```

Backend names currently exposed by the compatibility layer are:

- `grok-build` — primary backend.
- `hermes-compatible` — compatibility backend.
- `prime-agent` — experimental backend.

Provider configuration depends on the selected backend and is supplied through the environment or local configuration used by that backend. Do not commit credentials to the repository.

## Repository layout

```text
Duck-Agent/
├── apps/desktop/       # Electron/React desktop application
├── backends/            # Backend and harness code
├── scripts/             # Installer, CI, and project scripts
├── tests/               # Python backend and integration tests
├── duck-agent           # Command-line launcher
├── run_all_tests.sh     # Aggregate local test runner
└── README.md
```

`AGENTS.md` contains development invariants and repository guidance for contributors and coding agents. It is contributor documentation, not a command reference for end users.

## Development and testing

Python dependency and CI setup is still being stabilized. Use the repository's current test files and workflow definitions as the source of truth while that work proceeds.

The tracked Python E2E/backend test can be run directly when `pytest` is available:

```bash
python3 -m pytest tests/test_e2e_backend.py -v --tb=short
```

The repository also contains `run_all_tests.sh`, which is intended to provide an aggregate local test run. Its coverage and environment assumptions are under active repair; inspect the script and current CI status before relying on it as a release gate.

After installing the project's TypeScript test dependencies, backend suites can also be run individually with `tsx`, for example:

```bash
npx tsx backends/tests/test-index.ts
```

Desktop development uses the package configuration in `apps/desktop/package.json`. See that file for the available scripts and required Node.js version before installing dependencies or running desktop checks.

The test suite is evolving alongside the runtime. A passing focused test suite does not yet mean that every desktop, packaging, or long-running-agent workflow is complete.

## Roadmap

Planned runtime and desktop capabilities include:

- A complete reason → act → observe → recover loop.
- Durable sessions, goals, task state, and resumability.
- Real tool execution and MCP server transport.
- Skills and working/durable memory integration.
- Retries, cancellation, timeouts, and approval gates.
- Subtask decomposition and delegation.
- Structured progress and observability in the desktop UI.
- Broader provider and local-model support while keeping Grok Build primary.

## Contributing

Contributions should preserve useful existing desktop functionality, keep integration tests deterministic where possible, and add behavioral coverage for new agent semantics. Changes that affect the runtime should explain how state, tools, failures, and user-visible progress are handled.

Please read `AGENTS.md` before making repository changes. It contains the detailed architecture and development invariants that do not belong in the public README.

## Links

- Repository: <https://github.com/Franzferdinan51/Duck-Agent>
- Development guidance: [`AGENTS.md`](AGENTS.md)
- Desktop application: [`apps/desktop/`](apps/desktop/)
