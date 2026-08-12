/**
 * Duck Agent - Grok Build Backend
 * Primary harness integration for Duck Agent powered by Grok Build.
 */

export {
  GrokBuildHarness,
  getHarness,
  type BackendStatus,
  type AgentMessage,
  type AgentResponse,
  type AgentRunOptions,
  type ToolCall,
} from './harness'
export { GrokBuildAPIClient, type GrokMessage, type GrokToolCall, type GrokToolDefinition } from './api-client'
export { loadConfig, type GrokBuildConfig, DEFAULT_CONFIG } from './config'
export {
  runGrokAcpTurn,
  isSignedIn,
  acpHelpers,
  type AcpTurnEvents,
  type AcpTurnOptions,
  type AcpTurnResult,
} from './acp-driver'
