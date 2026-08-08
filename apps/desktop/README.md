# Duck Agent Desktop

Duck Agent Desktop is the native control surface for the Duck Agent autonomous runtime. It is derived from the Hermes desktop application and is being completed rather than replaced.

## Product rule

**Preserve first. Finish second. Remove only when a complete replacement exists.**

Useful Hermes-derived UI, Electron, terminal, workspace, session, settings, tool, and agent surfaces should be retained and adapted behind Duck Agent runtime interfaces. An unfinished feature is not a reason to delete it.

## Primary runtime

Grok Build is Duck Agent's primary harness. The desktop app should expose the runtime as a long-running agent, not a chat-completions wrapper:

```text
Goal
  -> plan / reason
  -> act with tools
  -> observe results
  -> recover / continue
  -> verify
  -> complete
```

## Desktop surfaces

The restored control surface includes first-class navigation for:

- Agent conversations
- Goals and long-running tasks
- Tool and MCP activity
- Memory
- Skills
- Workspace / repository context
- Terminal access
- Runtime and approval settings
- Sessions and resumable work

The active-run UI is intentionally structured around steps and tool observations so the user can see what Duck Agent is doing while it works.

## Branding

Duck Agent uses its own vector mark in `public/duck-agent-mark.svg` and the reusable React `BrandMark` component. Keep branding assets vector-first where practical so the app stays crisp across desktop scale factors.

## Hermes port strategy

The upstream Hermes desktop application remains an implementation reference while Duck Agent is restored. Port missing components incrementally, preserving Duck-specific runtime work already present in this repository.

For overlapping files:

1. Keep Duck Agent runtime behavior.
2. Reintroduce missing Hermes capabilities around it.
3. Rename product-facing Hermes branding to Duck Agent.
4. Do not mechanically overwrite Duck Agent-specific settings or runtime adapters.
5. Add tests before replacing an existing working implementation.

The repository intentionally keeps its Hermes-derived desktop dependencies and packaging surface so terminal, workspace, agent, artifact, cron, settings and related capabilities can be reintroduced without flattening the application into a simple chat shell.

## Development

The renderer entry is `src/main.tsx` and the Vite configuration is `vite.config.ts`.

The package also contains Electron packaging/test scripts inherited from the desktop foundation. Those paths should be restored and adapted as the port progresses rather than removed merely because a portion is currently incomplete.

## Current restoration phase

The first restoration pass establishes a functional Duck Agent renderer shell with:

- navigation and sessions
- an observable goal/run timeline
- structured tool activity
- runtime inspector
- autonomy/approval visibility
- tools, memory, skills, workspace and settings surfaces
- responsive desktop layout
- Duck Agent branding

Next integration work should connect these surfaces to live runtime events and restore the remaining Hermes-derived Electron/terminal/workspace components behind Duck Agent interfaces.
