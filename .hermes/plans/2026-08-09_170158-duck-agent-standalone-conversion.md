# Duck-Agent Standalone Conversion Plan

> **For Duck-Agent:** Execute this plan task-by-task with tests and evidence gates.

**Goal:** Remove the remaining Hermes-local-install assumptions so Duck-Agent is a standalone agent with its own runtime, state, CLI, and desktop onboarding.

**Architecture:** Keep explicitly supported remote-Hermes compatibility separate from local Duck-Agent behavior. Local setup, bootstrap, state, paths, copy, labels, and CLI must use Duck-Agent names and `~/.duck-agent`; remote compatibility may retain Hermes terminology only where it describes a remote target.

**Tech Stack:** Electron/React/TypeScript desktop, Python runtime/CLI, Vite, Vitest, unittest, uv.

---

## `/goal`

A fresh Duck-Agent checkout launches its own agent setup without offering to install Hermes, without deriving local paths under `~/.hermes`, and with a working `duck-agent` CLI and desktop runtime.

## `/subgoal 1` — Fix local desktop onboarding

**Files:**
- Modify: `apps/desktop/src/i18n/en.ts`
- Modify: `apps/desktop/src/i18n/zh.ts`
- Modify: `apps/desktop/src/i18n/zh-hant.ts`
- Modify: `apps/desktop/src/i18n/ja.ts`
- Modify: `apps/desktop/src/components/desktop-install-overlay.test.tsx`
- Inspect/modify: `apps/desktop/electron/bootstrap-runner.ts`
- Inspect/modify: `apps/desktop/electron/main.ts`

Replace the local choice with `Install Duck-Agent locally`, use `~/.duck-agent`, and ensure the local bootstrap invokes Duck-Agent’s own checkout/runtime rather than a Hermes installer or `NousResearch/duck-agent-agent`.

**Validation:** focused desktop overlay tests, string search for local Hermes-install wording, and a fresh renderer build.

## `/subgoal 2` — Separate compatibility from local runtime

Audit all `apps/desktop/electron` and `apps/desktop/src` references. Classify each as:

1. Local Duck-Agent state/runtime — must use Duck-Agent naming and `~/.duck-agent`.
2. Remote Hermes compatibility — retain only with explicit remote-target naming/comments.
3. Stale conversion residue — remove or rename.

Add regression tests asserting local defaults never resolve to `~/.hermes`.

## `/subgoal 3` — Make the standalone CLI installable

Keep `python -m duck_agent.cli` working, repair setuptools/uv packaging so the `duck-agent` console command works from outside the checkout, and add CLI smoke coverage for `status`, `doctor`, `workflows`, and `capabilities`.

## `/subgoal 4` — Make desktop startup reproducible

Fix `apps/desktop/scripts/assert-root-install.mjs` so the documented desktop install model matches the actual dependency layout. Ensure `npm run build` and `npm run start` work without requiring an unrelated root `npm ci`. Generate `install-stamp.json` as part of every supported build path.

## `/subgoal 5` — Verify and publish

Run:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
cd apps/desktop
npm run typecheck
npm run build
npm run start
```

Verify Electron main/renderer processes, setup-screen text, no local Hermes install prompt, no new local writes under `~/.hermes`, and remote `main` SHA. Commit each coherent subgoal and push normally.

## Risks and boundaries

- Do not delete, migrate, or overwrite the user’s existing `~/.hermes` data.
- Do not remove remote-Hermes compatibility paths without proving they are local paths.
- Do not copy upstream repositories wholesale; adapt behavior and preserve license/provenance boundaries.
- Do not claim standalone completion until a fresh launch and installed CLI are independently verified.
