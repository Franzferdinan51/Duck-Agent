export type GatewayAuthMode = 'oauth' | 'token' | (string & {})

export interface GatewayWsConnection {
  authMode?: GatewayAuthMode | null
  profile?: null | string
  wsUrl: string
}

export type GatewayWsUrlResult =
  | string
  | { ok: true; wsUrl: string }
  | { error: string; needsOauthLogin?: boolean; ok: false }

export interface ResolveGatewayWsUrlDeps {
  /** Mint a fresh single-use URL immediately before opening the socket. */
  getGatewayWsUrl?: (profile?: null | string) => Promise<GatewayWsUrlResult>
}

export class GatewayReauthRequiredError extends Error {
  readonly needsOauthLogin = true

  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options)
    this.name = 'GatewayReauthRequiredError'
  }
}

export function isGatewayReauthRequired(error: unknown): error is GatewayReauthRequiredError {
  return (
    error instanceof GatewayReauthRequiredError ||
    (typeof error === 'object' && error !== null && (error as { needsOauthLogin?: unknown }).needsOauthLogin === true)
  )
}

/**
 * Adapted from the Hermes Desktop gateway helper. OAuth gateways get a fresh
 * ticket for each socket while token/local gateways may reuse their URL.
 */
export async function resolveGatewayWsUrl(
  deps: ResolveGatewayWsUrlDeps,
  connection: GatewayWsConnection,
): Promise<string> {
  const mint = deps.getGatewayWsUrl
  const profile = connection.profile ?? null

  if (connection.authMode === 'oauth') {
    if (!mint) {
      throw new Error('This Duck Agent build cannot refresh OAuth WebSocket tickets.')
    }

    try {
      const result = await mint(profile)
      if (typeof result === 'string') return result
      if (result.ok) return result.wsUrl
      if (result.needsOauthLogin) {
        throw new GatewayReauthRequiredError(
          'The remote gateway session expired. Sign in to the gateway again.',
          { cause: new Error(result.error) },
        )
      }
      throw new Error(result.error || 'Could not refresh the gateway WebSocket ticket.')
    } catch (error) {
      if (isGatewayReauthRequired(error)) {
        throw error instanceof GatewayReauthRequiredError
          ? error
          : new GatewayReauthRequiredError('The remote gateway session expired.', { cause: error })
      }
      throw error
    }
  }

  if (mint) {
    const fresh = await mint(profile).catch(() => null)
    if (typeof fresh === 'string') return fresh
    if (fresh?.ok) return fresh.wsUrl
  }

  return connection.wsUrl
}

export type WebSocketAuthParam = readonly [name: string, value: string]

export interface DuckAgentWebSocketUrlOptions {
  path: string
  basePath?: string
  authParam?: WebSocketAuthParam
  params?: Record<string, string>
  protocol?: string
  host?: string
}

function readWindowLocation(): { host: string; protocol: string } {
  if (typeof window === 'undefined') return { host: '', protocol: 'http:' }
  return { host: window.location.host, protocol: window.location.protocol }
}

function normalizeBasePath(basePath?: string): string {
  if (!basePath) return ''
  return (basePath.startsWith('/') ? basePath : `/${basePath}`).replace(/\/+$/, '')
}

function normalizeEndpointPath(path: string): string {
  return path.startsWith('/') ? path : `/${path}`
}

export function buildDuckAgentWebSocketUrl(options: DuckAgentWebSocketUrlOptions): string {
  const location = readWindowLocation()
  const protocol = options.protocol ?? location.protocol
  const host = options.host ?? location.host
  const wsScheme = protocol === 'https:' || protocol === 'wss:' ? 'wss:' : 'ws:'
  const query = new URLSearchParams(options.params ?? {})

  if (options.authParam) query.set(options.authParam[0], options.authParam[1])
  const suffix = query.size ? `?${query.toString()}` : ''

  return `${wsScheme}//${host}${normalizeBasePath(options.basePath)}${normalizeEndpointPath(options.path)}${suffix}`
}

/** Compatibility alias for Hermes-derived callers during the preserve-first port. */
export const buildHermesWebSocketUrl = buildDuckAgentWebSocketUrl
export type HermesWebSocketUrlOptions = DuckAgentWebSocketUrlOptions
