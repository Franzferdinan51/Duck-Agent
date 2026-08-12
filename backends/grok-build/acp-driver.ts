/**
 * Duck Agent - Grok Build ACP CLI driver
 *
 * Drives the AUTHENTICATED `grok` CLI over ACP (Agent Client Protocol) stdio
 * (`grok --permission-mode ... agent stdio`). This is the grok.com SUBSCRIPTION
 * path (login in ~/.grok/auth.json) and needs NO api key at all — unlike
 * api-client.ts which requires GROK_API_KEY. Adapted from the MIT-licensed
 * OpenMausBot acp/core.ts reference implementation, reduced to a compact
 * single-turn runner with streaming events + cancellation.
 *
 * Primary reason this exists: Grok Build is Duck-Agent's primary harness
 * (AGENTS.md), and driving the already-logged-in grok CLI is the zero-config
 * way to run real agent turns.
 */

import { spawn, type ChildProcess } from 'node:child_process'
import { existsSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

export interface AcpTurnEvents {
  onDelta?: (text: string) => void
  onReasoning?: (text: string) => void
  onToolStart?: (title: string) => void
  onToolEnd?: (id: string, ok: boolean) => void
  onSessionStarted?: (sessionId: string) => void
}

export interface AcpTurnOptions {
  /** Absolute path to the grok binary. Defaults to `grok` on PATH. */
  cli?: string
  /** Force-allow every permission request (bypassPermissions). Default false. */
  fullAuto?: boolean
  /** Working directory for the agent session. */
  cwd?: string
  /** Optional system/persona prompt prepended to the goal. */
  systemPrompt?: string
}

export interface AcpTurnResult {
  ok: boolean
  text: string
  stopReason: string | null
  signedIn: boolean
}

const INIT_TIMEOUT = 20_000
const NEW_SESSION_TIMEOUT = 30_000
const PROMPT_TIMEOUT = 120_000

function isSignedIn(): boolean {
  return existsSync(join(homedir(), '.grok', 'auth.json'))
}

/**
 * Run a single proxy over the authenticated grok CLI via ACP stdio.
 * Resolves when the turn completes or errors/aborts.
 */
export async function runGrokAcpTurn(
  goal: string,
  options: AcpTurnOptions = {},
  events: AcpTurnEvents = {},
): Promise<AcpTurnResult> {
  const cli = options.cli ?? 'grok'
  const fullAuto = options.fullAuto ?? false
  const cwd = options.cwd ?? homedir()

  const text = (options.systemPrompt ? `${options.systemPrompt}\n\n` : '') + goal

  return new Promise<AcpTurnResult>((resolve, reject) => {
    let child: ChildProcess
    try {
      child = spawn(cli, ['--permission-mode', fullAuto ? 'bypassPermissions' : 'default', 'agent', 'stdio'], {
        cwd,
        env: {
          ...process.env,
          // The CLI owns the grok.com login; never leak a subscription-billing key.
          XAI_API_KEY: undefined,
        } as NodeJS.ProcessEnv,
        stdio: ['pipe', 'pipe', 'pipe'],
      })
    } catch (e) {
      reject(new Error(`spawn ${cli} failed: ${e instanceof Error ? e.message : String(e)}`))
      return
    }

    let settled = false
    let textAccum = ''
    let nextId = 1
    let sessionId: string | null = null
    let promptSent = false

    const pending = new Map<number, (m: any) => void>()

    const finish = (ok: boolean, stopReason: string | null) => {
      if (settled) return
      settled = true
      try {
        child.kill('SIGTERM')
      } catch {
        /* ignore */
      }
      resolve({ ok, text: textAccum, stopReason, signedIn: isSignedIn() })
    }

    const send = (obj: unknown) => {
      try {
        child.stdin?.write(JSON.stringify(obj) + '\n')
      } catch {
        /* closed */
      }
    }

    const request = (method: string, params: unknown, timeoutMs: number): Promise<any> =>
      new Promise((resolveReq, rejectReq) => {
        const id = nextId++
        const timer: ReturnType<typeof setTimeout> = setTimeout(() => {
          pending.delete(id)
          rejectReq(new Error(`${method} timed out`))
        }, timeoutMs)
        timer.unref?.()
        pending.set(id, (m) => {
          clearTimeout(timer)
          m.error ? rejectReq(new Error(String(m.error?.message ?? JSON.stringify(m.error)))) : resolveReq(m.result)
        })
        send({ jsonrpc: '2.0', id, method, params })
      })

    let buf = ''
    child.stdout?.on('data', (chunk: Buffer) => {
      buf += chunk
      let nl: number
      while ((nl = buf.indexOf('\n')) !== -1) {
        const line = buf.slice(0, nl)
        buf = buf.slice(nl + 1)
        if (!line.trim()) continue
        let m: any
        try {
          m = JSON.parse(line)
        } catch {
          continue
        }
        if (m.id !== undefined && (m.result !== undefined || m.error !== undefined)) {
          const h = pending.get(m.id)
          if (h) {
            pending.delete(m.id)
            h(m)
          }
          continue
        }
        // server -> client request: permission. Fail closed: never approve
        // unless fullAuto, in which case select the first "allow" option.
        if (m.id !== undefined && m.method) {
          if (m.method === 'session/request_permission') {
            const params = m.params ?? {}
            const optionsList: Array<{ optionId?: string; kind?: string }> = Array.isArray(params.options)
              ? params.options
              : []
            const allow = optionsList.find((o) => String(o.kind ?? '').startsWith('allow') && o.optionId)
            const outcome = allow ? { outcome: { outcome: 'selected' as const, optionId: allow.optionId } } : { outcome: { outcome: 'cancelled' as const } }
            send({ jsonrpc: '2.0', id: m.id, result: { outcome: outcome.outcome } })
          } else {
            send({ jsonrpc: '2.0', id: m.id, error: { code: -32601, message: 'method not found' } })
          }
          continue
        }
        // notification: session/update
        if (m.method === 'session/update') {
          const u = m.params?.update ?? {}
          if (!promptSent || m.params?._meta?.isReplay === true) continue
          switch (u.sessionUpdate) {
            case 'agent_message_chunk': {
              const d = u.content?.text
              if (typeof d === 'string' && d) {
                textAccum += d
                events.onDelta?.(d)
              }
              break
            }
            case 'agent_thought_chunk': {
              const d = u.content?.text
              if (typeof d === 'string' && d) events.onReasoning?.(d)
              break
            }
            case 'tool_call':
              events.onToolStart?.(String(u.rawInput?.command ?? u.title ?? 'tool').slice(0, 80))
              break
            case 'tool_call_update':
              if (u.status === 'completed' || u.status === 'failed') {
                events.onToolEnd?.(String(u.toolCallId ?? ''), u.status !== 'failed')
              }
              break
          }
        }
      }
    })

    let stderr = ''
    child.stderr?.on('data', (c: Buffer) => {
      stderr = (stderr + c).slice(-4096)
    })
    child.on('error', (e) => reject(new Error(`spawn failed: ${e.message}`)))
    child.on('close', (code) => {
      if (!settled) {
        reject(new Error(`grok exited ${code} before turn result${stderr ? `: ${stderr.trim().slice(-200)}` : ''}`))
      }
    })

    ;(async () => {
      try {
        const init = await request(
          'initialize',
          { protocolVersion: 1, clientCapabilities: { fs: { readTextFile: false, writeTextFile: false } } },
          INIT_TIMEOUT,
        )
        const methods: Array<{ id?: string }> = Array.isArray(init?.authMethods) ? init.authMethods : []
        if (!methods.some((m) => m.id === 'cached_token')) {
          throw new Error('grok CLI is not signed in — run `grok login` to enable the agent-stdio path')
        }
        await request('authenticate', { methodId: 'cached_token' }, INIT_TIMEOUT)

        const started = await request('session/new', { cwd, mcpServers: [] }, NEW_SESSION_TIMEOUT)
        sessionId = typeof started?.sessionId === 'string' ? started.sessionId : null
        if (!sessionId) throw new Error('session/new returned no sessionId')
        events.onSessionStarted?.(sessionId)

        promptSent = true
        const result = await request('session/prompt', { sessionId, prompt: [{ type: 'text', text }] }, PROMPT_TIMEOUT)
        const reason: string | null = result?.stopReason ?? null
        if (reason === 'end_turn') finish(true, null)
        else if (reason === 'cancelled') finish(true, 'cancelled')
        else finish(false, reason ?? 'failed')
      } catch (e) {
        if (!settled) {
          settled = true
          try {
            child.kill('SIGTERM')
          } catch {
            /* ignore */
          }
          const err = e instanceof Error ? e.message : String(e)
          reject(new Error(err))
        }
      }
    })()
  })
}

/** Resolver helper for runGrokAcpTurn options. */
export { isSignedIn }
export const acpHelpers = { isSignedIn, DEFAULT_SYSTEM_PROMPT: `You are Duck Agent, an autonomous desktop AI agent powered by the Grok Build harness. Work toward the user's goal until it is actually complete. Use tools when needed. Do not claim an action happened unless a tool result confirms it. When the goal is complete, return a concise final result.` }