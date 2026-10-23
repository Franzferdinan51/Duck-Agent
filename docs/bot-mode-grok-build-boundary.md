# Bot Mode / Grok Build runtime boundary

Duck-Agent uses **Grok Build CLI as its primary agent harness**. The vendored Hermes Bot Mode plugin is a coordination and UI layer; it is not the reasoning or tool-execution backend.

## Responsibility split

| Layer | Owns |
| --- | --- |
| Duck-Agent desktop | conversations, workspace, settings, status, user interaction |
| Bot Mode | bot roster, profile metadata, per-bot chat UX, avatars, routines, handoff intent |
| Duck-Agent compatibility gateway | `profiles.*`, `sessions.*`, `cron.*`, assets and plugin RPC transport |
| Grok Build CLI | reasoning, planning, tool execution, agent turns and autonomous work |

The compatibility gateway may still be started through the legacy Hermes `serve` command while those RPC surfaces are migrated. That process is infrastructure only. Code must not treat it as the product harness.

## Execution invariant

Any operation that asks an agent to **do work** must end at the Grok Build CLI path. Duck-Agent already provides an authenticated ACP driver in `backends/grok-build/acp-driver.ts`, which runs `grok ... agent stdio` using the user's Grok login.

Examples:

- A user message to a Bot Mode profile -> resolve profile/persona -> Grok Build turn.
- A bot-to-bot handoff -> resolve target profile -> Grok Build turn for the target.
- A scheduled routine becoming due -> cron/control plane resolves the job -> Grok Build turn.
- Profile create/edit/delete, avatar reads/writes and cron metadata changes -> gateway RPC only; these are not agent turns.

## Vendored Bot Mode source

`apps/desktop/vendor/hermes-bot-mode` is a git submodule sourced from:

`https://github.com/Franzferdinan51/Hermes-Bot-Mode`

The submodule is intentionally pinned. Update it explicitly and review Bot Mode changes before advancing the gitlink.

```bash
git submodule sync --recursive
git submodule update --init --recursive
```

## Compatibility rule

Hermes names may remain in compatibility shims, RPC implementations, migration paths and vendored source. They must not become the default execution path. New execution features should target the Grok Build harness interface/ACP driver first, with compatibility adapters layered around it as needed.

## Follow-on integration work

When wiring additional Bot Mode features, preserve this direction:

```text
Bot Mode intent / UI
        |
        +---- metadata / assets / cron config ----> compatibility gateway
        |
        `---- agent work --------------------------> Grok Build CLI / ACP
```

Avoid teaching Bot Mode profiles to shell out directly to `hermes -p ...` for delegation inside Duck-Agent. Handoffs should be translated by the host into a Grok Build-backed target-profile turn so the selected harness remains authoritative.
