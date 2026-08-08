# Duck Agent Runtime / Backends

Duck Agent is built around **Grok Build as the primary agent harness**. The backend boundary exists so the runtime stays clean and future providers can be added, but the project should not be designed as three equal backend products.

The target behavior is similar in spirit to Hermes Agent or OpenClaw: accept a goal, reason, call tools, observe results, recover from failures, and continue until the goal is complete.

## Runtime layers

```text
Desktop / CLI
    │
    ▼
Duck Agent runtime
    ├── goal/task state
    ├── session state
    ├── tool registry
    ├── MCP transports
    ├── skills
    ├── workflow orchestration
    ├── approvals
    └── run events
    │
    ▼
Grok Build harness (PRIMARY)
    ├── reasoning/model calls
    ├── tool selection
    ├── iterative agent loop
    └── model context
```

## Grok Build

`backends/grok-build/` is the main harness implementation.

The harness now has a bounded autonomous loop that can:

1. send the current goal/context to the Grok Build-compatible API,
2. expose registered tools as function definitions,
3. receive tool calls,
4. execute them through the runtime tool manager,
5. append observations back into the model context,
6. repeat until the model returns a final response or the safety step limit is reached.

This is the core behavior Duck Agent needs to grow into a real agent rather than a one-shot chat wrapper.

### Configuration

```bash
GROK_API_KEY=...
GROK_API_ENDPOINT=...
GROK_MODEL=...
DUCK_AGENT_MAX_STEPS=24
DUCK_AGENT_BACKEND=grok-build
```

## MCP / tools

`backends/mcp/` is the runtime-facing tool registry.

Tools should only be exposed to the model when they have a real executable handler/transport. Metadata-only placeholder tools must not pretend that an action happened.

The next transport work should connect real MCP stdio/HTTP servers, discover their tools, and register executable handlers with the manager.

## Orchestration

`backends/orchestration/` manages multi-step workflows and dependency-aware execution. It should complement the model-driven agent loop rather than replace it.

Use deterministic workflows where dependencies are known; use the Grok Build loop where the agent must decide what action to take next.

## Sessions

Agent state must eventually be persisted outside the harness so runs can survive UI boundaries and process restarts. In-memory chat history is only a temporary implementation detail.

## Skills

Skills should provide reusable instructions/workflows that the runtime can load into a task without baking every behavior into the model system prompt.

## Compatibility modes

The launcher currently recognizes:

```text
grok-build
hermes-compatible
prime-agent
```

- `grok-build` — primary/default product path.
- `hermes-compatible` — compatibility path while preserving/reusing Hermes-derived behavior.
- `prime-agent` — experimental path.

Do not weaken the Grok Build architecture just to force every compatibility backend into the same lowest-common-denominator feature set.

## Definition of done for the runtime

A production-ready Duck Agent run should be able to:

- accept a goal,
- persist a run/session,
- plan or choose the next action,
- invoke real tools,
- feed tool results back to the model,
- retry/recover from failures,
- request approval when required,
- stream structured progress to the desktop UI,
- cancel cleanly,
- resume where appropriate,
- and stop only when the goal is complete or an explicit safety/resource limit is reached.

That is the standard new runtime work should be measured against.
