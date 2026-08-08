import { randomUUID } from 'crypto'

/** Adapted from Franzferdinan51/GrokBot run-history logic. Storage is injected so desktop/CLI can share it. */
export type RunStatus = 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted'
export type RunRecord = {
  id: string
  cwd: string
  prompt: string
  model?: string
  backend: 'grok-build' | 'hermes-compatible' | 'prime-agent'
  startedAt: number
  finishedAt?: number
  status: RunStatus
  sessionId?: string
  latencyMs?: number
  tokensIn?: number
  tokensOut?: number
  costUsd?: number
  error?: string
  errorClass?: string
  advisorCount?: number
  advisorFailures?: number
}

export interface RunStore { get(): RunRecord[]; set(records: RunRecord[]): void }
const MAX_RUNS = 200
const MAX_STORED_PROMPT = 8_000

export function recoverInterruptedRuns(store: RunStore): RunRecord[] {
  const now = Date.now()
  const runs = store.get().map(record => record.status === 'running' ? { ...record, status: 'interrupted' as const, finishedAt: now, errorClass: 'interrupted' } : record)
  store.set(runs)
  return runs
}

export function startRun(store: RunStore, input: Omit<RunRecord, 'id' | 'startedAt' | 'status' | 'prompt'> & { prompt: string }): RunRecord {
  const record: RunRecord = {
    ...input,
    id: randomUUID(),
    prompt: input.prompt.length > MAX_STORED_PROMPT ? `${input.prompt.slice(0, MAX_STORED_PROMPT)}\n… [execution context omitted]` : input.prompt,
    startedAt: Date.now(),
    status: 'running',
  }
  store.set([record, ...store.get()].slice(0, MAX_RUNS))
  return record
}

export function finishRun(store: RunStore, id: string, patch: Partial<RunRecord> & Pick<RunRecord, 'status'>): RunRecord | undefined {
  let updated: RunRecord | undefined
  const runs = store.get().map(record => {
    if (record.id !== id) return record
    updated = { ...record, ...patch, finishedAt: Date.now() }
    return updated
  })
  store.set(runs)
  return updated
}

export function usageMetrics(usage: unknown): Pick<RunRecord, 'tokensIn' | 'tokensOut' | 'costUsd'> {
  if (!usage || typeof usage !== 'object') return {}
  const value = usage as Record<string, unknown>
  const number = (...keys: string[]) => {
    for (const key of keys) {
      const candidate = value[key]
      if (typeof candidate === 'number' && Number.isFinite(candidate)) return candidate
    }
    return undefined
  }
  return {
    tokensIn: number('tokens_in', 'input_tokens', 'prompt_tokens', 'inputTokens'),
    tokensOut: number('tokens_out', 'output_tokens', 'completion_tokens', 'outputTokens'),
    costUsd: number('cost_usd', 'costUsd', 'total_cost_usd'),
  }
}

export function classifyRunError(message: string): string {
  if (/no output|timed? ?out|timeout/i.test(message)) return 'timeout'
  if (/unauthorized|forbidden|auth/i.test(message)) return 'authentication'
  if (/rate.?limit|429/i.test(message)) return 'rate_limit'
  if (/network|connection|econn/i.test(message)) return 'network'
  if (/cancel|abort/i.test(message)) return 'cancelled'
  if (/context|token limit|too long/i.test(message)) return 'context_limit'
  return 'runtime'
}
