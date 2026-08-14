// Compatibility gateway subcommand routing for the desktop-managed Hermes service.
//
// IMPORTANT: this process is control-plane infrastructure for Duck-Agent's
// profile/session/cron/plugin RPC surfaces. It is NOT Duck-Agent's agent harness.
// Grok Build CLI remains the primary execution harness and owns reasoning/tool
// turns. Bot Mode is layered on these gateway surfaces while agent execution is
// routed through Grok Build.
//
// The desktop app launches its own headless compatibility gateway via
// `hermes serve` and must NEVER depend on or launch the browser `dashboard`.
// `serve` is a newer subcommand: a runtime that predates it (an older managed
// install the app hasn't updated yet, or an older `hermes` resolved from PATH)
// only knows `dashboard --no-open`. To avoid bricking those users mid-upgrade
// we detect whether the resolved runtime understands `serve` and, only when it
// does not, fall back to the legacy `dashboard --no-open` invocation. Both
// produce the same headless compatibility gateway; `serve` is just the
// decoupled name.
//
// These helpers are pure so they can be unit-tested without Electron.

/**
 * Build the canonical headless compatibility-gateway argv (always `serve`).
 * @param {string} [profile] optional service profile to pin via `--profile`.
 */
export function serveBackendArgs(profile?: string) {
  const head = profile ? ['--profile', profile] : []

  return [...head, 'serve', '--host', '127.0.0.1', '--port', '0']
}

/**
 * Rewrite a resolved gateway argv from `serve` to the legacy
 * `dashboard --no-open` form, preserving every other argument (incl. a leading
 * `-m hermes_cli.main` and any `--profile <name>`). Returns a copy; if there is
 * no `serve` token the argv is returned unchanged.
 */
export function dashboardFallbackArgs(args) {
  const i = args.indexOf('serve')

  if (i === -1) {
    return args.slice()
  }

  return [...args.slice(0, i), 'dashboard', '--no-open', ...args.slice(i + 1)]
}

/**
 * True when a runtime's `hermes_cli/subcommands/dashboard.py` source registers
 * the `serve` subcommand. Matches `add_parser("serve"` / `add_parser('serve'`
 * specifically so the substring "server" (e.g. "start_server", "web server")
 * never produces a false positive.
 */
export function sourceDeclaresServe(dashboardPySource) {
  return /add_parser\(\s*["']serve["']/.test(String(dashboardPySource || ''))
}
