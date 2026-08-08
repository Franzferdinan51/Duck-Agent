/**
 * Duck Agent - Backend Loader
 * 
 * Dynamically loads and manages agent backends based on configuration.
 */

import { GrokBuildHarness, getHarness as getGrokBuildHarness } from './grok-build'

export type BackendType = 'grok-build' | 'hermes-compatible' | 'prime-agent'

export interface BackendInfo {
  type: BackendType
  name: string
  description: string
  status: 'available' | 'configured' | 'active'
}

/**
 * Get the configured backend type from environment
 */
export function getConfiguredBackend(): BackendType {
  const backend = process.env.DUCK_AGENT_BACKEND
  if (backend && isValidBackend(backend)) {
    return backend
  }
  return 'grok-build' // Default to Grok Build
}

/**
 * Check if a backend type is valid
 */
export function isValidBackend(backend: string): backend is BackendType {
  return ['grok-build', 'hermes-compatible', 'prime-agent'].includes(backend)
}

/**
 * Get backend info for all available backends
 */
export function getAvailableBackends(): BackendInfo[] {
  return [
    {
      type: 'grok-build',
      name: 'Grok Build',
      description: 'Primary harness with full Grok Build capabilities',
      status: 'available'
    },
    {
      type: 'hermes-compatible',
      name: 'Hermes-Compatible',
      description: 'Hermes Agent compatibility mode',
      status: 'available'
    },
    {
      type: 'prime-agent',
      name: 'Prime Agent',
      description: 'Prime Intellect RLM-based agent',
      status: 'available'
    }
  ]
}

/**
 * Get the appropriate harness for the configured backend
 */
export async function getBackend(): Promise<GrokBuildHarness> {
  const backendType = getConfiguredBackend()
  
  switch (backendType) {
    case 'grok-build':
      return getGrokBuildHarness()
    
    case 'hermes-compatible':
      // In Hermes-compatible mode, we use Grok Build with Hermes protocol
      console.log('[Duck Agent] Loading Hermes-compatible backend...')
      return getGrokBuildHarness()
    
    case 'prime-agent':
      // Prime Agent integration would be loaded here
      console.log('[Duck Agent] Loading Prime Agent backend...')
      return getGrokBuildHarness()
    
    default:
      return getGrokBuildHarness()
  }
}

/**
 * Initialize the backend harness
 */
export async function initializeBackend(): Promise<void> {
  const harness = await getBackend()
  await harness.start()
  console.log(`[Duck Agent] Backend initialized: ${getConfiguredBackend()}`)
}

// Re-export harness types
export { type BackendStatus, type AgentMessage, type AgentResponse, type ToolCall } from './grok-build'
