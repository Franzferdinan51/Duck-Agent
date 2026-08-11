# Duck Agent

Duck Agent is a desktop-first autonomous-agent project built on the Duck Agent desktop foundation, with **Grok Build as its primary agent harness**.

The project is under active development. The long-term goal is an agent that can work toward a user goal through an iterative loop of reasoning, tool use, observation, recovery, and completion—not just return a single chat response.

## Current status

The repository currently includes:

- A `duck-agent` launcher and backend-selection layer.
- A Grok Build backend as the primary path.
- Duck Agent-compatible and Prime Agent compatibility/experimental paths.
- Backend managers for sessions, skills, workflows, and MCP metadata.
- A Python dependency baseline (`pyproject.toml` + `uv.lock`) using `uv`.
- Python and TypeScript backend test suites, plus an aggregate `run_all_tests.sh` runner.
- Recovered CI helpers (parallel test slices, lint-diff, Windows foot-gun checks).
- Windows installer scripts and their PowerShell tests.
- A Duck Agent-derived Electron desktop application under `apps/desktop/`.

The complete autonomous runtime is still being built. Durable task state, resumable
long-running work, approval policies, full desktop runtime integration, and production-ready
tool feedback remain active development areas.

## Architecture

```text
┌──────────────────────────────┐
│ Duck Agent-derived desktop app  │
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
- `duck-agent-compatible` — compatibility backend.
- `prime-agent` — experimental backend.

Provider configuration depends on the selected backend and is supplied through the environment or local configuration used by that backend. Do not commit credentials to the repository.

### Duck Agent-style Duck-Agent CLI

Duck-Agent provides a local-first command surface inspired by the Duck Agent CLI
and the governed workflow model of oh-my-duck-agent. **Grok Build is the primary
harness**: the CLI hands off real agent work to the installed `grok` binary
rather than a stub. Status is reported honestly (planned / running / reported),
never upgraded to "verified" unless an execution actually completed.

Use the `duck-agent` launcher (or `python3 -m duck_agent.cli`) from the repo root:

```bash
./duck-agent --help
./duck-agent status          # backend, Grok Build version, isolation
./duck-agent version         # Duck-Agent + Grok Build version
./duck-agent doctor          # verifies grok is present, runnable, and keyed
./duck-agent backends        # list backends (grok-build = primary)
./duck-agent workflows       # plan / research / code / operate
./duck-agent capabilities    # governed capability map
./duck-agent chat            # launch the Grok Build TUI (primary harness)
./duck-agent work "<goal>"   # run a governed workflow via grok (headless turn)
./duck-agent setup            # guide configuring the Grok Build API key
./duck-agent update           # update Duck-Agent from Franzferdinan51/Duck-Agent
```

Available workflows are `plan`, `research`, `code`, and `operate`. Duck-Agent's
local state defaults to `~/.duck-agent`; it does not use or overwrite an
existing Duck Agent `~/.duck-agent` home. `DUCK_AGENT_HOME` or `--home` can
override the state location.

### Desktop application

The Electron application lives under `apps/desktop/` and requires Node.js 22.22 or newer. From the desktop directory:

```bash
cd apps/desktop
npm install
npm run build
npm run start
```

For renderer hot reload during development, use `npm run dev`. The desktop build and runtime use Duck-Agent's isolated local home by default.

## Repository layout

```text
Duck-Agent/
├── apps/desktop/       # Electron/React desktop application
├── backends/            # Backend and harness code
├── duck_agent/          # Python launcher/backend-selection layer
├── scripts/             # Installer, CI, and Windows scripts
├── tests/               # Python backend and integration tests
├── pyproject.toml       # Python dependency baseline
├── uv.lock              # Locked Python dependency versions
├── duck-agent           # Command-line launcher
├── run_all_tests.sh     # Aggregate local test runner
└── README.md
```

`AGENTS.md` contains development invariants and repository guidance for contributors and coding agents. It is contributor documentation, not a command reference for end users.

## Development and testing

The Python dependency baseline lives in `pyproject.toml` with a lockfile at `uv.lock`, and the repository uses **`uv`** for dependency management. Install and run the Python tests with:

```bash
uv sync --locked --python 3.11 --extra all --extra dev
uv run python -m unittest discover -s tests -p 'test_*.py'
```

Ruff and the Windows foot-gun checker are pinned in the project configuration:

```bash
uv run ruff check .
python3 scripts/check-windows-footguns.py --all
```

The tracked Python E2E/backend test can be run directly when `pytest` is available:

```bash
python3 -m pytest tests/test_e2e_backend.py -v --tb=short
```

The repository also contains `run_all_tests.sh`, which provides an aggregate local test
run across the Python and TypeScript suites. Use it as the broadest local gate:

```bash
./run_all_tests.sh
```

After installing the project's TypeScript test dependencies, backend suites can also be run
individually with `tsx`, for example:

```bash
npx tsx backends/tests/test-index.ts
```

Desktop development uses the package configuration in `apps/desktop/package.json`. See that
file for the available scripts and the required Node.js version before installing
dependencies or running desktop checks.

CI is defined under `.github/workflows/` and is orchestrated by `ci.yml`. Some lanes
(JS workspace checks, the Docusaurus docs site, and Hadolint Docker linting) are gated on
their required repository inputs being present; on a PR they skip when those assets are
absent. Python tests, linters, the lockfile check, the installer tests, and OSV scanning
run on every relevant change.

The test suite is evolving alongside the runtime. A passing focused test suite does not yet
mean that every desktop, packaging, or long-running-agent workflow is complete.

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
