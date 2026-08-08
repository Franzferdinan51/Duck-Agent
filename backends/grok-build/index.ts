/**
 * Duck Agent - Grok Build Backend
 * 
 * Primary harness integration for Duck Agent powered by Grok Build.
 */

export { GrokBuildHarness, getHarness, type BackendStatus, type AgentMessage, type AgentResponse, type ToolCall } from './harness'
export { loadConfig, type GrokBuildConfig, DEFAULT_CONFIG } from './config'
