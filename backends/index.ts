/** Duck Agent backend loader. Grok Build remains the primary/default harness. */

import { GrokBuildHarness, getHarness as getGrokBuildHarness } from './grok-build'
import { PrimeAgentHarness, getPrimeAgentHarness } from './prime-agent'

export type BackendType = 'grok-build' | 'hermes-compatible' | 'prime-agent'
export type DuckAgentBackend = GrokBuildHarness | PrimeAgentHarness

export interface BackendInfo {
  type: BackendType
  name: string
  description: string
  status: 'available' | 'configured' | 'active'
}

export function getConfiguredBackend(): BackendType {
  const backend = process.env.DUCK_AGENT_BACKEND
  return backend && isValidBackend(backend) ? backend : 'grok-build'
}

export function isValidBackend(backend: string): backend is BackendType {
  return ['grok-build', 'hermes-compatible', 'prime-agent'].includes(backend)
}

export function getAvailableBackends(): BackendInfo[] {
  return [
    {
      type: 'grok-build',
      name: 'Grok Build',
      description: 'Primary Duck Agent harness with iterative tool-driven execution',
      status: 'available',
    },
    {
      type: 'hermes-compatible',
      name: 'Hermes-Compatible',
      description: 'Preserve-first Hermes Desktop/gateway compatibility path',
      status: 'available',
    },
    {
      type: 'prime-agent',
      name: 'Prime Agent',
      description: 'Native persistent Prime Agent RPC session with RLM/daemon capabilities',
      status: 'available',
    },
  ]
}

export async function getBackend(): Promise<DuckAgentBackend> {
  switch (getConfiguredBackend()) {
    case 'prime-agent':
      return getPrimeAgentHarness({
        binary: process.env.PRIME_AGENT_BINARY,
        cwd: process.env.PRIME_AGENT_CWD || process.cwd(),
        provider: process.env.PRIME_AGENT_PROVIDER,
        model: process.env.PRIME_AGENT_MODEL,
        sessionDir: process.env.PRIME_AGENT_SESSION_DIR,
      })

    case 'hermes-compatible':
      // The preserve-first Hermes path currently shares Grok Build reasoning
      // while Hermes gateway/UI primitives are restored around it.
      return getGrokBuildHarness()

    case 'grok-build':
    default:
      return getGrokBuildHarness()
  }
}

export async function initializeBackend(): Promise<void> {
  const harness = await getBackend()
  await harness.start()
  console.log(`[Duck Agent] Backend initialized: ${getConfiguredBackend()}`)
}

export { GrokBuildHarness } from './grok-build'
export { PrimeAgentHarness, PrimeAgentRpcClient } from './prime-agent'
export { type BackendStatus, type AgentMessage, type AgentResponse, type ToolCall } from './grok-build'
export type { PrimeAgentResponse, PrimeBackendStatus, PrimeRpcEvent } from './prime-agent'
