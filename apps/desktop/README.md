# Duck Agent Desktop

<p align="center">
  <img src="assets/icon.png" alt="Duck Agent" width="128">
</p>

<p align="center">
  <strong>Hermes-derived desktop control surface for the Duck Agent runtime</strong>
</p>

Duck Agent Desktop is built on top of the Hermes desktop application. The intent is to **keep the useful desktop functionality that already exists** and replace/adapt the underlying agent runtime so Duck Agent becomes a real autonomous agent powered primarily by **Grok Build**.

This is not intended to become a stripped-down chat client.

## Product direction

The desktop app is the control surface. The autonomous runtime belongs underneath it.

```text
Electron / React desktop UI (Hermes-derived)
                │
                ▼
          Duck Agent runtime
                │
       goals • sessions • tools
       MCP • skills • approvals
                │
                ▼
       Grok Build harness (primary)
```

When modifying the desktop app, preserve working Hermes features whenever possible and rewire them to Duck Agent interfaces rather than deleting them.

## Expected desktop capabilities

The finished desktop application should expose:

- conversations and session history
- long-running goals/tasks
- live agent-run status and progress
- tool calls and observations
- terminal / PTY access
- workspace and file interaction
- MCP server configuration
- skills
- provider/model settings
- permissions and approval prompts
- cancellation and resumability
- notifications for background work

## Primary backend

**Grok Build is the primary and default harness.**

Compatibility paths may exist for Hermes or experimental backends, but the product should not present all backends as equal architectural targets.

Set the current backend with:

```bash
DUCK_AGENT_BACKEND=grok-build
```

## Preserve the Hermes foundation

The application started from the Hermes desktop app. That existing work is an asset.

Development rule of thumb:

1. Keep working UI/features.
2. Identify Hermes-specific runtime coupling.
3. Introduce a Duck Agent adapter/interface.
4. Wire Grok Build and the Duck Agent runtime behind it.
5. Add tests before removing legacy paths.

Avoid large rewrites that throw away mature desktop behavior without a concrete benefit.

## Development

The desktop package requires Node.js 22.22+.

```bash
npm install
npm run dev
```

Build and run:

```bash
npm run build
npm start
```

Useful checks:

```bash
npm run typecheck
npm run lint
npm test
npm run test:e2e
```

## Architecture

```text
apps/desktop/
├── electron/         # Electron main process and native integration
├── src/              # React frontend
│   ├── app/          # application shell
│   ├── components/   # UI components
│   ├── hooks/        # React hooks
│   └── store/        # client state
├── scripts/          # build/test tooling
└── e2e/              # end-to-end tests
```

The desktop code should consume structured agent events rather than embedding model-specific orchestration directly inside React components.

## Agent-event direction

The runtime/UI boundary should eventually carry events such as:

```text
run.started
run.status
model.thinking
model.output
plan.updated
tool.started
tool.result
tool.failed
approval.requested
step.completed
run.completed
run.failed
run.cancelled
```

This gives the desktop app enough information to show what the agent is actually doing without coupling the UI to Grok-specific response objects.

## Releases

Pre-built releases, when available, are published on the repository releases page:

https://github.com/Franzferdinan51/Duck-Agent/releases
