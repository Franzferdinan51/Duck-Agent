# Duck Agent Development Invariants

This file is guidance for coding agents and contributors working in this repository.

## Product identity

Duck Agent is a **real autonomous desktop agent**, not a simple chat wrapper.

The desktop application is derived from the Duck Agent desktop app. Preserve useful existing Duck Agent functionality while replacing or adapting the runtime underneath it.

The **primary harness is Grok Build**.

Do not accidentally redesign the project as:

- a generic multi-provider chat client
- a thin REST chat-completions wrapper
- a Duck Agent rebrand with no autonomous runtime
- a UI-only project

## Architecture invariant

The runtime owns the agent loop:

```text
user goal
  -> reason / plan
  -> choose action or tool
  -> execute
  -> observe result
  -> update state
  -> continue or recover
  -> final result
```

A single model response is never considered sufficient architecture for agent mode.

## Grok Build

Grok Build is the default and primary harness. New core-agent work should target Grok Build first unless a task explicitly concerns compatibility with another backend.

The Grok Build implementation must move toward:

- real requests
- streaming events
- native tool/function calling where available
- tool-result feedback
- iterative execution
- cancellation
- retries
- context management
- structured run events

Do not leave placeholder echo responses in a code path represented as production-ready.

## Duck Agent desktop foundation

The desktop app was taken from Duck Agent and is being built on top of.

When modifying `apps/desktop`:

1. Preserve working features unless there is a concrete reason to remove them.
2. Prefer adapters and stable interfaces over rewrites.
3. Replace Duck Agent-specific backend assumptions with Duck Agent runtime interfaces.
4. Keep terminal, workspace, session, settings, tool and related functionality intact where possible.
5. Make agent execution observable in the UI.

## Required agent capabilities

The architecture should make room for:

- goals / long-running tasks
- persistent sessions
- MCP tools
- local tools
- skills
- working memory
- durable memory where enabled
- retries and recovery
- subtask decomposition
- approval gates
- cancellation
- progress events
- resumability
- local-model providers in the future

## Tool safety

Tool execution should be explicit and typed. Destructive or high-impact operations should support approval policies rather than being silently executed.

## Sessions and state

Do not keep all meaningful agent state only in an in-memory array inside a model harness. The long-term design requires a session/task layer that can survive UI boundaries and eventually process restarts.

## Testing philosophy

Prefer behavioral integration tests that prove agent semantics.

Examples:

- a goal requires a tool call before completion
- a failed tool call is retried or recovered from
- tool output is fed back into the next reasoning step
- a multi-step goal completes in dependency order
- a cancelled run stops cleanly
- a session can be resumed with its task state intact

Module-import tests alone are not enough.

## Compatibility backends

`duck-agent-compatible` and `prime-agent` may remain as compatibility/experimental paths, but they must not blur the primary product direction. Shared interfaces are good; designing the whole product around the lowest common denominator is not.
