export type DuckAgentRunStatus = 'idle' | 'planning' | 'running' | 'waiting-approval' | 'completed' | 'failed' | 'cancelled'

export type DuckAgentRunEventName =
  | 'run.started'
  | 'run.status'
  | 'plan.updated'
  | 'model.output'
  | 'tool.started'
  | 'tool.result'
  | 'tool.failed'
  | 'approval.requested'
  | 'step.completed'
  | 'run.completed'
  | 'run.failed'
  | 'run.cancelled'

export interface DuckAgentRunEvent<T = unknown> {
  id: string
  runId: string
  name: DuckAgentRunEventName
  timestamp: number
  payload: T
}

export interface DuckAgentRuntimeSettings {
  backend: 'grok-build' | 'hermes-compatible' | 'prime-agent'
  model: string
  maxAgentSteps: number
  approvalMode: 'strict' | 'balanced' | 'autonomous'
  streamEvents: boolean
  resumeRuns: boolean
}

export {
  JsonRpcGatewayClient,
  type ConnectionState,
  type GatewayClientOptions,
  type GatewayEvent,
  type GatewayEventName,
  type GatewayRequestId,
  type JsonRpcFrame,
  type WebSocketLike,
} from './json-rpc-gateway'

export { skillInvocationText } from './skill-scaffold'

export {
  buildDuckAgentWebSocketUrl,
  buildHermesWebSocketUrl,
  GatewayReauthRequiredError,
  isGatewayReauthRequired,
  resolveGatewayWsUrl,
  type DuckAgentWebSocketUrlOptions,
  type GatewayAuthMode,
  type GatewayWsConnection,
  type GatewayWsUrlResult,
  type HermesWebSocketUrlOptions,
  type ResolveGatewayWsUrlDeps,
  type WebSocketAuthParam,
} from './websocket-url'
