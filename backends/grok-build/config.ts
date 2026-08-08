/** Duck Agent - Grok Build Backend Configuration */

export interface GrokBuildConfig {
  apiEndpoint?: string
  apiKey?: string
  defaultModel?: string
  enableMcpTools?: boolean
  streamingEnabled?: boolean
  maxConcurrentTools?: number
  maxAgentSteps?: number
  timeoutMs?: number
  /** Retries for transient HTTP/network failures. */
  requestRetries?: number
  /** Base delay for exponential retry backoff. */
  retryBaseDelayMs?: number
}

export const DEFAULT_CONFIG: GrokBuildConfig = {
  apiEndpoint: process.env.GROK_API_ENDPOINT || 'https://api.grok.com',
  apiKey: process.env.GROK_API_KEY,
  defaultModel: process.env.GROK_MODEL || 'grok-3',
  enableMcpTools: true,
  streamingEnabled: true,
  maxConcurrentTools: 5,
  maxAgentSteps: Number(process.env.DUCK_AGENT_MAX_STEPS || 24),
  timeoutMs: Number(process.env.GROK_REQUEST_TIMEOUT_MS || 60_000),
  requestRetries: Number(process.env.GROK_REQUEST_RETRIES || 2),
  retryBaseDelayMs: Number(process.env.GROK_RETRY_BASE_DELAY_MS || 350),
}

export function loadConfig(): GrokBuildConfig {
  return {
    ...DEFAULT_CONFIG,
    apiEndpoint: process.env.GROK_API_ENDPOINT || DEFAULT_CONFIG.apiEndpoint,
    apiKey: process.env.GROK_API_KEY || DEFAULT_CONFIG.apiKey,
    defaultModel: process.env.GROK_MODEL || DEFAULT_CONFIG.defaultModel,
    maxAgentSteps: Number(process.env.DUCK_AGENT_MAX_STEPS || DEFAULT_CONFIG.maxAgentSteps),
    timeoutMs: Number(process.env.GROK_REQUEST_TIMEOUT_MS || DEFAULT_CONFIG.timeoutMs),
    requestRetries: Number(process.env.GROK_REQUEST_RETRIES || DEFAULT_CONFIG.requestRetries),
    retryBaseDelayMs: Number(process.env.GROK_RETRY_BASE_DELAY_MS || DEFAULT_CONFIG.retryBaseDelayMs),
  }
}
