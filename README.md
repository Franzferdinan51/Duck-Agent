# Duck Agent

Duck Agent is a desktop-first autonomous AI agent built on top of the Hermes desktop application architecture, with **Grok Build as the primary agent harness**.

The goal is not to make another chat UI. Duck Agent should behave like a real agent in the same class as Hermes Agent or OpenClaw: it should be able to reason over a goal, use tools, work across multiple steps, keep session state, recover from failures, and continue until the task is actually complete.

## Core direction

Duck Agent keeps the existing Hermes-derived desktop experience and builds a new agent runtime underneath it.

**Do not strip out working Hermes desktop functionality just to make Grok Build fit.** The existing application is the foundation. Grok Build should become the primary orchestration/model harness that powers the agent capabilities behind that UI.

The intended architecture is:

```text
Hermes-derived Desktop App
        │
        ▼
Duck Agent Runtime
        │
        ├── sessions / memory
        ├── goals / task state
        ├── tool execution
        ├── MCP servers
        ├── skills
        ├── workflows / sub-tasks
        └── permissions / approvals
        │
        ▼
Grok Build Harness (PRIMARY)
        │
        ├── model reasoning
        ├── planning
        ├── tool selection
        └── iterative agent loop
```

Hermes compatibility may remain useful, but **Hermes is not the primary harness for Duck Agent**. Grok Build is.

## What makes Duck Agent a real agent

A completed Duck Agent runtime should support all of the following:

- **Goal-oriented execution** — the user gives an objective, not just a single prompt.
- **Iterative agent loop** — reason → act → observe → continue until complete.
- **Tool use** — files, shell, Git, web, browser, MCP tools, APIs and app actions.
- **Persistent sessions** — preserve task state and conversation state across steps.
- **Memory** — short-term working context plus durable user/project memory where enabled.
- **Multi-step planning** — break large tasks into steps and track progress.
- **Retries and recovery** — recover from failed tools, bad outputs and transient errors.
- **Long-running work** — jobs should not be limited to one request/response round trip.
- **Subtasks / delegation** — the runtime should be able to split work when appropriate.
- **Human approval gates** — destructive or sensitive actions can require confirmation.
- **MCP support** — external tools should be first-class runtime capabilities.
- **Skills** — reusable instructions/workflows should be loadable by the agent.
- **Observable execution** — the UI should expose what the agent is doing, its current task, tools and results.

A model response by itself is not an agent. The runtime must own the loop.

## Primary harness: Grok Build

Grok Build is the default and primary harness for Duck Agent.

```bash
DUCK_AGENT_BACKEND=grok-build ./duck-agent
```

The Grok Build integration should eventually provide:

- model requests and streaming
- native tool/function calling
- iterative execution until completion
- tool-result feedback into the model
- context/session management
- cancellation and timeout handling
- structured events for the desktop UI
- model configuration and provider credentials

The current implementation is still being connected to the full runtime, so some pieces remain scaffolding.

## Hermes-derived desktop app

The desktop application under [`apps/desktop`](apps/desktop) comes from the Hermes desktop application and is being evolved into Duck Agent.

**Preserve existing useful functionality.** When replacing Hermes-specific runtime wiring, prefer adapting it behind stable UI interfaces instead of deleting features.

The desktop app should become the control surface for the agent:

- conversations
- goals and tasks
- agent run status
- tool activity
- terminal
- files/workspace
- MCP servers
- skills
- model/provider settings
- permissions and approvals
- session history

## Repository layout

```text
Duck-Agent/
├── apps/
│   └── desktop/          # Hermes-derived Electron/React desktop application
├── backends/
│   ├── grok-build/       # Primary Grok Build harness
│   ├── mcp/              # MCP integration
│   ├── orchestration/    # Workflow/task orchestration
│   ├── sessions/         # Session state
│   └── skills/           # Reusable agent skills
├── duck_agent/           # Python launcher/backend-selection compatibility layer
├── tests/                # Test suite
├── AGENTS.md             # Development invariants for coding agents
├── duck-agent            # CLI launcher
└── README.md
```

## Quick start

```bash
git clone https://github.com/Franzferdinan51/Duck-Agent.git
cd Duck-Agent
chmod +x ./duck-agent
./duck-agent --help
```

Run the primary backend:

```bash
DUCK_AGENT_BACKEND=grok-build ./duck-agent
```

## Backend policy

The backend names currently exposed by the compatibility layer are:

- `grok-build` — **primary / default**
- `hermes-compatible` — compatibility path
- `prime-agent` — experimental path

These should not be treated as three equal product directions. Duck Agent should be designed around the Grok Build runtime while keeping the backend boundary clean enough that other harnesses can be supported later.

## Development priorities

1. Make the Grok Build harness perform real model requests instead of placeholder responses.
2. Implement the persistent reason/action/observation loop.
3. Wire actual MCP server processes and tool calls into the loop.
4. Connect the agent runtime to the existing Hermes-derived desktop UI.
5. Preserve and rewire existing desktop features rather than removing them.
6. Add durable sessions, task state and recovery.
7. Add approval/permission boundaries for dangerous actions.
8. Stabilize CI and integration tests around real agent behavior.
9. Add long-running `/goal`-style execution with progress and resumability.
10. Treat local-model support as a future first-class provider path without changing the Grok Build primary-harness direction today.

## Testing

```bash
python3 run_tests.py
./run_all_tests.sh
```

The important integration tests should increasingly verify agent behavior, not just module loading. A useful test should prove that Duck Agent can accept a goal, invoke one or more tools, consume the observations and finish with a correct result.

## Status

Duck Agent is under active development. Parts of the runtime are still scaffolding, and the current Grok Build harness is not yet the complete autonomous loop described above.

That gap is intentional development work — **the target is a real autonomous desktop agent, not a renamed chatbot.**

## Repository

https://github.com/Franzferdinan51/Duck-Agent
