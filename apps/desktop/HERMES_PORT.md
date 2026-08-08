# Hermes Desktop Restoration Tracker

Duck Agent's desktop application is built on top of the Hermes desktop foundation. This tracker exists so missing upstream functionality is restored deliberately instead of silently deleted or forgotten.

## Rules

- Never remove an unfinished Hermes-derived feature only because its dependencies are not wired yet.
- Finish or adapt existing Duck Agent files before replacing them.
- Grok Build remains the primary harness.
- Runtime-specific logic belongs behind Duck Agent interfaces; React components should consume structured state/events.
- Preserve Electron-native, terminal, workspace, session, artifact, cron, command-center, messaging, settings, tool and agent UX where useful.

## Upstream areas to restore/adapt

The current Hermes desktop reference contains substantial application areas under `apps/desktop/src/app`, including:

- `agents/`
- `artifacts/`
- `chat/`
- `command-center/`
- `command-palette/`
- `cron/`
- `gateway/`
- `learning/`
- `messaging/`
- `settings/`
- shared hooks, components, stores, themes, utilities, terminal/workspace surfaces and Electron integration

Duck Agent currently has only a subset of that tree. The renderer shell added in this restoration pass is a stable Duck Agent landing surface while those mature capabilities are reintroduced.

## Restoration phases

### Phase 1 — renderer/control surface

- [x] renderer entry point
- [x] Vite configuration
- [x] responsive Duck Agent shell
- [x] sessions/navigation
- [x] goal/run timeline
- [x] tool activity surface
- [x] runtime inspector
- [x] memory/skills/workspace/settings surfaces
- [x] Duck Agent vector brand mark
- [x] compatibility primitives required by existing backend settings

### Phase 2 — runtime event bridge

- [ ] define renderer-safe `RunEvent` schema
- [ ] wire `run.started`, `plan.updated`, `tool.started`, `tool.result`, `approval.requested`, `run.completed`, `run.failed`, and `run.cancelled`
- [ ] connect cancellation and retry actions
- [ ] persist active run/task identity outside component-local state
- [ ] resume interrupted runs

### Phase 3 — Hermes feature restoration

- [ ] restore/adapt chat thread and rich message rendering
- [ ] restore command center and command palette
- [ ] restore terminal/PTTY surface
- [ ] restore file/workspace browser and editor surfaces
- [ ] restore artifacts/preview workflow
- [ ] restore cron/scheduled-agent UX
- [ ] restore messaging/integration surfaces
- [ ] restore model/provider controls while keeping Grok Build primary
- [ ] restore notifications and native menus
- [ ] restore themes/i18n/accessibility utilities

### Phase 4 — Electron/native integration

- [ ] restore Electron main/preload process
- [ ] restore secure IPC boundary
- [ ] restore native menus/tray/notifications
- [ ] restore filesystem and terminal bridges
- [ ] restore packaging scripts and platform targets
- [ ] verify macOS, Windows and Linux builds

### Phase 5 — production hardening

- [ ] renderer unit tests
- [ ] agent-run behavioral tests
- [ ] approval flow tests
- [ ] E2E goal execution
- [ ] crash/restart/resume tests
- [ ] packaging smoke tests
- [ ] accessibility and keyboard navigation audit
- [ ] performance pass for long tool/run histories

## Definition of done

Duck Agent Desktop is not complete merely because it can display chat. A finished desktop build must make long-running autonomous work observable and controllable while retaining the mature desktop capabilities inherited from Hermes.
