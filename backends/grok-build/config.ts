/**
 * Duck Agent - Grok Build Backend Configuration
 */

export interface GrokBuildConfig {
  /** Grok Build-compatible API endpoint */
  apiEndpoint?: string
  /** API key */
  apiKey?: string
  /** Default model */
  defaultModel?: string
  /** Expose registered MCP/local tools to the model */
  enableMcpTools?: boolean
  /** Enable streaming when the runtime has a streaming transport */
  streamingEnabled?: boolean
  /** Maximum concurrent tool executions */
  maxConcurrentTools?: number
  /** Maximum reason/action/observation iterations in one run */
  maxAgentSteps?: number
  /** Request timeout in milliseconds */
  timeoutMs?: number
}

export const DEFAULT_CONFIG: GrokBuildConfig = {
  apiEndpoint: process.env.GROK_API_ENDPOINT || 'https://api.grok.com',
  apiKey: process.env.GROK_API_KEY,
  defaultModel: process.env.GROK_MODEL || 'grok-3',
  enableMcpTools: true,
  streamingEnabled: true,
  maxConcurrentTools: 5,
  maxAgentSteps: Number(process.env.DUCK_AGENT_MAX_STEPS || 24),
  timeoutMs: 60000,
}

export function loadConfig(): GrokBuildConfig {
  return {
    ...DEFAULT_CONFIG,
    apiEndpoint: process.env.GROK_API_ENDPOINT || DEFAULT_CONFIG.apiEndpoint,
    apiKey: process.env.GROK_API_KEY || DEFAULT_CONFIG.apiKey,
    defaultModel: process.env.GROK_MODEL || DEFAULT_CONFIG.defaultModel,
    maxAgentSteps: Number(process.env.DUCK_AGENT_MAX_STEPS || DEFAULT_CONFIG.maxAgentSteps),
  }
}
