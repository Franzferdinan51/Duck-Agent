/**
 * Duck Agent - Grok Build Backend Configuration
 * 
 * This module configures the Grok Build harness as the primary backend
 * for Duck Agent's agent orchestration.
 */

export interface GrokBuildConfig {
  /** Grok Build API endpoint */
  apiEndpoint?: string
  
  /** Grok Build API key */
  apiKey?: string
  
  /** Default model to use */
  defaultModel?: string
  
  /** Enable MCP tools */
  enableMcpTools?: boolean
  
  /** Enable streaming responses */
  streamingEnabled?: boolean
  
  /** Maximum concurrent tools */
  maxConcurrentTools?: number
  
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
  timeoutMs: 60000
}

export function loadConfig(): GrokBuildConfig {
  return {
    ...DEFAULT_CONFIG,
    apiEndpoint: process.env.GROK_API_ENDPOINT || DEFAULT_CONFIG.apiEndpoint,
    apiKey: process.env.GROK_API_KEY || DEFAULT_CONFIG.apiKey,
    defaultModel: process.env.GROK_MODEL || DEFAULT_CONFIG.defaultModel
  }
}
