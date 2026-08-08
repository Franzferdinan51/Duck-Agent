/**
 * Adapted from NousResearch/hermes-agent (MIT).
 * Preserves the resilient JSON-RPC/WebSocket behavior while using Duck Agent naming.
 */
export type GatewayEventName =
  | 'gateway.ready' | 'session.info' | 'message.start' | 'message.delta' | 'message.interim'
  | 'message.complete' | 'thinking.delta' | 'reasoning.delta' | 'reasoning.available'
  | 'status.update' | 'tool.start' | 'tool.progress' | 'tool.complete' | 'tool.generating'
  | 'clarify.request' | 'approval.request' | 'sudo.request' | 'secret.request'
  | 'background.complete' | 'error' | 'skin.changed' | (string & {})

export interface GatewayEvent<P = unknown> { payload?: P; profile?: string; session_id?: string; type: GatewayEventName }
export type ConnectionState = 'idle' | 'connecting' | 'open' | 'closed' | 'error'
export type GatewayRequestId = number | string
export interface JsonRpcFrame { error?: { message?: string }; id?: GatewayRequestId | null; method?: string; params?: GatewayEvent; result?: unknown }
export type WebSocketLike = WebSocket

type PendingCall = { reject: (error: Error) => void; resolve: (value: unknown) => void; timer?: ReturnType<typeof setTimeout> }
export interface GatewayClientOptions {
  closedErrorMessage?: string; connectErrorMessage?: string; connectTimeoutMs?: number
  createRequestId?: (nextId: number) => GatewayRequestId; onSocketClose?: (event: CloseEvent) => boolean | void
  requestIdPrefix?: string; requestTimeoutMs?: number; socketFactory?: (url: string) => WebSocketLike
  notConnectedErrorMessage?: string
}
const ANY = '*'
const DEFAULT_REQUEST_TIMEOUT_MS = 120_000
const DEFAULT_CONNECT_TIMEOUT_MS = 15_000

export class JsonRpcGatewayClient {
  private nextId = 0
  private pending = new Map<GatewayRequestId, PendingCall>()
  private socket: WebSocketLike | null = null
  private state: ConnectionState = 'idle'
  private readonly eventHandlers = new Map<string, Set<(event: GatewayEvent) => void>>()
  private readonly stateHandlers = new Set<(state: ConnectionState) => void>()
  private readonly options: Required<Omit<GatewayClientOptions, 'socketFactory'>> & Pick<GatewayClientOptions, 'socketFactory'>

  constructor(options: GatewayClientOptions = {}) {
    this.options = {
      closedErrorMessage: options.closedErrorMessage ?? 'WebSocket closed',
      connectErrorMessage: options.connectErrorMessage ?? 'WebSocket connection failed',
      connectTimeoutMs: options.connectTimeoutMs ?? DEFAULT_CONNECT_TIMEOUT_MS,
      createRequestId: options.createRequestId ?? ((nextId) => `${options.requestIdPrefix ?? 'r'}${nextId}`),
      notConnectedErrorMessage: options.notConnectedErrorMessage ?? 'gateway not connected',
      onSocketClose: options.onSocketClose ?? (() => false),
      requestIdPrefix: options.requestIdPrefix ?? 'r',
      requestTimeoutMs: options.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS,
      socketFactory: options.socketFactory,
    }
  }

  get connectionState(): ConnectionState { return this.state }

  async connect(wsUrl: string): Promise<void> {
    let url: URL
    try { url = new URL(wsUrl) } catch { throw new Error(`gateway connect() requires a ws:// or wss:// URL string, got ${JSON.stringify(wsUrl)}`) }
    if (url.protocol !== 'ws:' && url.protocol !== 'wss:') throw new Error(`gateway connect() requires ws:// or wss://, got ${url.protocol}`)
    if (this.socket?.readyState === WebSocket.OPEN || this.state === 'connecting') return
    this.setState('connecting')
    const socket = this.options.socketFactory?.(wsUrl) ?? new WebSocket(wsUrl)
    this.socket = socket
    socket.addEventListener('message', message => { if (this.socket === socket) this.handleMessage(message.data) })
    socket.addEventListener('close', event => {
      if (this.socket !== socket || this.options.onSocketClose(event)) return
      this.socket = null; this.setState('closed'); this.rejectAllPending(new Error(this.options.closedErrorMessage))
    })
    await new Promise<void>((resolve, reject) => {
      let settled = false
      const finish = (ok: boolean) => {
        if (settled) return; settled = true; clearTimeout(timer)
        socket.removeEventListener('open', onOpen); socket.removeEventListener('error', onError)
        this.setState(ok ? 'open' : 'error'); ok ? resolve() : reject(new Error(this.options.connectErrorMessage))
      }
      const onOpen = () => finish(true)
      const onError = () => finish(false)
      socket.addEventListener('open', onOpen, { once: true }); socket.addEventListener('error', onError, { once: true })
      const timer = setTimeout(() => { try { socket.close() } catch {} if (this.socket === socket) this.socket = null; finish(false) }, this.options.connectTimeoutMs)
    })
  }

  close(): void {
    const socket = this.socket; if (!socket) return
    try { socket.close() } finally { this.socket = null; this.setState('closed'); this.rejectAllPending(new Error(this.options.closedErrorMessage)) }
  }

  on<P = unknown>(type: GatewayEventName, handler: (event: GatewayEvent<P>) => void): () => void {
    let handlers = this.eventHandlers.get(type); if (!handlers) { handlers = new Set(); this.eventHandlers.set(type, handlers) }
    handlers.add(handler as (event: GatewayEvent) => void); return () => handlers?.delete(handler as (event: GatewayEvent) => void)
  }
  onAny(handler: (event: GatewayEvent) => void): () => void { return this.on(ANY as GatewayEventName, handler) }
  onState(handler: (state: ConnectionState) => void): () => void { this.stateHandlers.add(handler); handler(this.state); return () => this.stateHandlers.delete(handler) }

  request<T>(method: string, params: Record<string, unknown> = {}, timeoutMs = this.options.requestTimeoutMs, signal?: AbortSignal): Promise<T> {
    const socket = this.socket
    if (!socket || socket.readyState !== WebSocket.OPEN) return Promise.reject(new Error(this.options.notConnectedErrorMessage))
    if (signal?.aborted) return Promise.reject(new DOMException('Aborted', 'AbortError'))
    const id = this.options.createRequestId(++this.nextId)
    return new Promise<T>((resolve, reject) => {
      let onAbort: (() => void) | undefined
      const detach = () => { if (onAbort && signal) signal.removeEventListener('abort', onAbort) }
      const pending: PendingCall = { resolve: value => { detach(); resolve(value as T) }, reject: error => { detach(); reject(error) } }
      if (timeoutMs > 0) pending.timer = setTimeout(() => { if (this.pending.delete(id)) { detach(); reject(new Error(`request timed out after ${Math.round(timeoutMs / 1000)}s: ${method}`)) } }, timeoutMs)
      if (signal) {
        onAbort = () => { const call = this.pending.get(id); if (call?.timer) clearTimeout(call.timer); this.pending.delete(id); detach(); reject(new DOMException('Aborted', 'AbortError')) }
        signal.addEventListener('abort', onAbort, { once: true })
      }
      this.pending.set(id, pending)
      try { socket.send(JSON.stringify({ jsonrpc: '2.0', id, method, params })) }
      catch (error) { this.clearPending(id); detach(); reject(error instanceof Error ? error : new Error(String(error))) }
    })
  }

  private handleMessage(raw: unknown): void {
    let frame: JsonRpcFrame
    try { frame = JSON.parse(typeof raw === 'string' ? raw : String(raw)) as JsonRpcFrame } catch { return }
    if (frame.id !== undefined && frame.id !== null) {
      const call = this.pending.get(frame.id); if (!call) return; this.clearPending(frame.id)
      frame.error ? call.reject(new Error(frame.error.message || 'Duck Agent RPC failed')) : call.resolve(frame.result); return
    }
    if (frame.method === 'event' && frame.params?.type) this.dispatchEvent(frame.params)
  }
  private clearPending(id: GatewayRequestId): void { const call = this.pending.get(id); if (call?.timer) clearTimeout(call.timer); this.pending.delete(id) }
  private dispatchEvent(event: GatewayEvent): void { for (const h of this.eventHandlers.get(event.type) ?? []) h(event); for (const h of this.eventHandlers.get(ANY) ?? []) h(event) }
  private rejectAllPending(error: Error): void { for (const [id, call] of this.pending) { if (call.timer) clearTimeout(call.timer); call.reject(error); this.pending.delete(id) } }
  private setState(state: ConnectionState): void { if (this.state === state) return; this.state = state; for (const h of this.stateHandlers) h(state) }
}
