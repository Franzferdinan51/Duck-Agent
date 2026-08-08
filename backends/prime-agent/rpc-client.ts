import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { randomUUID } from 'node:crypto'

export interface PrimeRpcResponse<T = unknown> {
  id?: string
  type: 'response'
  command: string
  success: boolean
  data?: T
  error?: string
}

export interface PrimeRpcEvent {
  type: string
  [key: string]: unknown
}

export interface PrimeAgentState {
  isStreaming?: boolean
  isCompacting?: boolean
  sessionFile?: string
  sessionId?: string
  sessionName?: string
  messageCount?: number
  unfinishedActionCount?: number
  model?: unknown
}

export interface PrimeRpcClientOptions {
  binary?: string
  cwd?: string
  provider?: string
  model?: string
  sessionDir?: string
  noSession?: boolean
  requestTimeoutMs?: number
}

type PendingRequest = {
  resolve: (response: PrimeRpcResponse) => void
  reject: (error: Error) => void
  timer: ReturnType<typeof setTimeout>
}

/**
 * Persistent Prime Agent subprocess transport.
 *
 * Prime Agent's RPC mode is JSONL over stdin/stdout. The process is reused
 * across commands so Duck Agent does not pay startup cost for every turn and
 * Prime's session/kernel state can remain hot. Parsing intentionally splits on
 * LF only; Prime's protocol documentation notes generic line readers can split
 * valid JSON strings on Unicode separators.
 */
export class PrimeAgentRpcClient {
  private process: ChildProcessWithoutNullStreams | null = null
  private stdoutBuffer = ''
  private pending = new Map<string, PendingRequest>()
  private listeners = new Set<(event: PrimeRpcEvent) => void>()
  private options: Required<Pick<PrimeRpcClientOptions, 'binary' | 'requestTimeoutMs'>> & PrimeRpcClientOptions

  constructor(options: PrimeRpcClientOptions = {}) {
    this.options = {
      ...options,
      binary: options.binary ?? process.env.PRIME_AGENT_BINARY ?? 'prime-agent',
      requestTimeoutMs: options.requestTimeoutMs ?? 30_000,
    }
  }

  get running(): boolean {
    return Boolean(this.process && this.process.exitCode === null && !this.process.killed)
  }

  async start(): Promise<void> {
    if (this.running) return

    const args = ['--mode', 'rpc']
    if (this.options.provider) args.push('--provider', this.options.provider)
    if (this.options.model) args.push('--model', this.options.model)
    if (this.options.noSession) args.push('--no-session')
    if (this.options.sessionDir) args.push('--session-dir', this.options.sessionDir)

    const child = spawn(this.options.binary, args, {
      cwd: this.options.cwd,
      env: process.env,
      stdio: ['pipe', 'pipe', 'pipe'],
    })

    this.process = child
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', (chunk: string) => this.consumeStdout(chunk))
    child.stderr.on('data', (chunk: string) => {
      this.emit({ type: 'transport.stderr', text: chunk })
    })
    child.on('error', error => this.failAll(error))
    child.on('exit', (code, signal) => {
      this.process = null
      this.failAll(new Error(`Prime Agent RPC exited (code=${code ?? 'null'}, signal=${signal ?? 'null'})`))
      this.emit({ type: 'transport.exit', code, signal })
    })

    // A cheap state query doubles as a startup/readiness probe.
    await this.request<PrimeAgentState>({ type: 'get_state' }, 45_000)
  }

  async stop(): Promise<void> {
    const child = this.process
    if (!child) return
    this.process = null
    child.kill('SIGTERM')
    this.failAll(new Error('Prime Agent RPC client stopped'))
  }

  async abort(): Promise<void> {
    if (!this.running) return
    await this.request({ type: 'abort' })
  }

  async request<T = unknown>(
    command: Record<string, unknown>,
    timeoutMs = this.options.requestTimeoutMs,
  ): Promise<PrimeRpcResponse<T>> {
    if (!this.running) {
      if (command.type === 'get_state' && !this.process) {
        throw new Error('Prime Agent RPC process is not running')
      }
      await this.start()
    }

    const child = this.process
    if (!child) throw new Error('Prime Agent RPC process is unavailable')

    const id = typeof command.id === 'string' ? command.id : randomUUID()
    const payload = { ...command, id }

    return new Promise<PrimeRpcResponse<T>>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id)
        reject(new Error(`Prime Agent RPC request timed out: ${String(command.type ?? 'unknown')}`))
      }, timeoutMs)

      this.pending.set(id, {
        resolve: response => resolve(response as PrimeRpcResponse<T>),
        reject,
        timer,
      })

      child.stdin.write(`${JSON.stringify(payload)}\n`, error => {
        if (!error) return
        const pending = this.pending.get(id)
        if (!pending) return
        clearTimeout(pending.timer)
        this.pending.delete(id)
        pending.reject(error)
      })
    })
  }

  subscribe(listener: (event: PrimeRpcEvent) => void): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  private consumeStdout(chunk: string): void {
    this.stdoutBuffer += chunk

    while (true) {
      const newline = this.stdoutBuffer.indexOf('\n')
      if (newline < 0) break

      let line = this.stdoutBuffer.slice(0, newline)
      this.stdoutBuffer = this.stdoutBuffer.slice(newline + 1)
      if (line.endsWith('\r')) line = line.slice(0, -1)
      if (!line) continue

      try {
        const frame = JSON.parse(line) as PrimeRpcEvent
        if (frame.type === 'response' && typeof frame.id === 'string') {
          const pending = this.pending.get(frame.id)
          if (pending) {
            clearTimeout(pending.timer)
            this.pending.delete(frame.id)
            const response = frame as PrimeRpcResponse
            if (response.success) pending.resolve(response)
            else pending.reject(new Error(response.error || `Prime Agent command failed: ${response.command}`))
            continue
          }
        }
        this.emit(frame)
      } catch (error) {
        this.emit({
          type: 'transport.parse_error',
          line,
          error: error instanceof Error ? error.message : String(error),
        })
      }
    }
  }

  private emit(event: PrimeRpcEvent): void {
    for (const listener of this.listeners) listener(event)
  }

  private failAll(error: Error): void {
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer)
      pending.reject(error)
    }
    this.pending.clear()
  }
}
