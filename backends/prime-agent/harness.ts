import { PrimeAgentRpcClient, type PrimeAgentState, type PrimeRpcClientOptions, type PrimeRpcEvent } from './rpc-client'

export type PrimeBackendStatus = 'idle' | 'starting' | 'ready' | 'running' | 'error' | 'stopped'

export interface PrimeAgentResponse {
  content: string
  status: 'complete' | 'error'
  metadata?: Record<string, unknown>
}

export interface PrimeRunOptions {
  timeoutMs?: number
  streamingBehavior?: 'steer' | 'followUp'
}

export interface PrimeAgentHarnessOptions extends PrimeRpcClientOptions {
  pollIntervalMs?: number
  runTimeoutMs?: number
}

/**
 * Duck Agent compatibility adapter for Prime Intellect's native RPC mode.
 *
 * This intentionally uses Prime's own long-running session implementation
 * rather than translating Prime into Grok-style chat completions. That keeps
 * its persistent REPL, compaction, goals, subagents and daemon/session model
 * available while Duck Agent provides the shared desktop control surface.
 */
export class PrimeAgentHarness {
  private readonly client: PrimeAgentRpcClient
  private readonly pollIntervalMs: number
  private readonly runTimeoutMs: number
  private status: PrimeBackendStatus = 'idle'
  private cancelled = false

  constructor(options: PrimeAgentHarnessOptions = {}) {
    this.client = new PrimeAgentRpcClient(options)
    this.pollIntervalMs = options.pollIntervalMs ?? 150
    this.runTimeoutMs = options.runTimeoutMs ?? 30 * 60_000
  }

  async start(): Promise<void> {
    if (this.status === 'ready' || this.status === 'running') return
    this.status = 'starting'
    try {
      await this.client.start()
      this.status = 'ready'
    } catch (error) {
      this.status = 'error'
      throw error
    }
  }

  async stop(): Promise<void> {
    this.cancelled = true
    await this.client.stop()
    this.status = 'stopped'
  }

  async cancel(): Promise<void> {
    this.cancelled = true
    await this.client.abort()
    if (this.status !== 'stopped') this.status = 'ready'
  }

  async sendMessage(content: string): Promise<PrimeAgentResponse> {
    return this.runGoal(content)
  }

  async runGoal(goal: string, options: PrimeRunOptions = {}): Promise<PrimeAgentResponse> {
    if (this.status === 'idle' || this.status === 'error' || this.status === 'stopped') {
      await this.start()
    }
    if (this.status !== 'ready') throw new Error(`Prime Agent harness not ready: ${this.status}`)

    this.cancelled = false
    this.status = 'running'
    const startedAt = Date.now()
    const timeoutMs = options.timeoutMs ?? this.runTimeoutMs

    try {
      const before = await this.getState()
      const beforeCount = before.messageCount ?? 0

      await this.client.request({
        type: 'prompt',
        message: goal,
        ...(options.streamingBehavior ? { streamingBehavior: options.streamingBehavior } : {}),
      })

      let latest = before
      while (Date.now() - startedAt < timeoutMs) {
        if (this.cancelled) {
          this.status = 'ready'
          return { content: 'Prime Agent run cancelled.', status: 'error', metadata: { backend: 'prime-agent', cancelled: true } }
        }

        await delay(this.pollIntervalMs)
        latest = await this.getState()
        const progressed = (latest.messageCount ?? 0) > beforeCount
        if (progressed && !latest.isStreaming && !latest.isCompacting && (latest.unfinishedActionCount ?? 0) === 0) {
          const final = await this.client.request<{ text: string | null }>({ type: 'get_last_assistant_text' })
          this.status = 'ready'
          return {
            content: final.data?.text ?? '',
            status: 'complete',
            metadata: {
              backend: 'prime-agent',
              sessionId: latest.sessionId,
              sessionFile: latest.sessionFile,
              durationMs: Date.now() - startedAt,
            },
          }
        }
      }

      await this.client.abort().catch(() => undefined)
      this.status = 'ready'
      return {
        content: `Prime Agent run exceeded the ${timeoutMs}ms Duck Agent timeout and was aborted.`,
        status: 'error',
        metadata: { backend: 'prime-agent', timeout: true },
      }
    } catch (error) {
      this.status = 'error'
      return {
        content: error instanceof Error ? error.message : String(error),
        status: 'error',
        metadata: { backend: 'prime-agent' },
      }
    }
  }

  getStatus(): PrimeBackendStatus {
    return this.status
  }

  subscribe(listener: (event: PrimeRpcEvent) => void): () => void {
    return this.client.subscribe(listener)
  }

  async getState(): Promise<PrimeAgentState> {
    const response = await this.client.request<PrimeAgentState>({ type: 'get_state' })
    return response.data ?? {}
  }

  async steer(message: string): Promise<void> {
    await this.client.request({ type: 'steer', message })
  }

  async followUp(message: string): Promise<void> {
    await this.client.request({ type: 'follow_up', message })
  }
}

function delay(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

let primeHarness: PrimeAgentHarness | null = null

export function getPrimeAgentHarness(options?: PrimeAgentHarnessOptions): PrimeAgentHarness {
  if (!primeHarness) primeHarness = new PrimeAgentHarness(options)
  return primeHarness
}
