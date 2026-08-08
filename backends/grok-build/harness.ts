/**
 * Duck Agent - Grok Build Harness
 *
 * Grok Build is the primary reasoning harness. The runtime owns a bounded
 * reason -> act -> observe loop and feeds every tool result back to the model.
 */

import { loadConfig, type GrokBuildConfig } from './config'
import { GrokBuildAPIClient, type GrokMessage, type GrokToolDefinition } from './api-client'
import { getMCPManager } from '../mcp/server'

export type BackendStatus = 'idle' | 'starting' | 'ready' | 'running' | 'error' | 'stopped'

export interface AgentMessage {
  role: 'user' | 'assistant' | 'system' | 'tool'
  content: string
  timestamp?: number
  toolCallId?: string
}

export interface AgentResponse {
  content: string
  tools?: ToolCall[]
  status: 'complete' | 'streaming' | 'error'
  metadata?: Record<string, unknown>
}

export interface ToolCall {
  id?: string
  name: string
  arguments: Record<string, unknown>
}

export interface AgentRunOptions {
  systemPrompt?: string
  maxSteps?: number
}

const DEFAULT_SYSTEM_PROMPT = `You are Duck Agent, an autonomous desktop AI agent powered by the Grok Build harness.
Work toward the user's goal until it is actually complete. Use tools when they are needed. Treat tool output as observations, recover from errors when possible, and do not claim an action happened unless a tool result confirms it. When the goal is complete, return a concise final result.`

const INTERRUPTED_TURN_GUIDANCE = `<turn_aborted>
The previous turn was interrupted. A tool or command may have partially executed. Re-check relevant state before retrying destructive or non-idempotent work.
</turn_aborted>`

export class GrokBuildHarness {
  private config: GrokBuildConfig
  private status: BackendStatus = 'idle'
  private messageHistory: AgentMessage[] = []
  private client: GrokBuildAPIClient
  private cancelled = false
  private interrupted = false

  constructor(config?: Partial<GrokBuildConfig>) {
    this.config = { ...loadConfig(), ...config }
    this.client = new GrokBuildAPIClient(this.config)
  }

  async start(): Promise<void> {
    this.status = 'starting'
    try {
      if (!this.client.isConfigured()) {
        throw new Error('Grok Build is not configured. Set GROK_API_KEY before starting the harness.')
      }
      this.status = 'ready'
    } catch (error) {
      this.status = 'error'
      throw error
    }
  }

  async stop(): Promise<void> {
    this.cancelled = true
    this.interrupted = this.status === 'running'
    this.status = 'stopped'
  }

  cancel(): void {
    this.cancelled = true
    this.interrupted = this.status === 'running'
  }

  async sendMessage(content: string): Promise<AgentResponse> {
    return this.runGoal(content)
  }

  async runGoal(goal: string, options: AgentRunOptions = {}): Promise<AgentResponse> {
    if (this.status !== 'ready') throw new Error(`Harness not ready. Current status: ${this.status}`)

    this.cancelled = false
    this.status = 'running'

    const maxSteps = options.maxSteps ?? this.config.maxAgentSteps ?? 24
    const mcp = getMCPManager()
    const tools: GrokToolDefinition[] = this.config.enableMcpTools
      ? mcp.getAllTools().map(tool => ({
          type: 'function' as const,
          function: { name: tool.name, description: tool.description, parameters: tool.inputSchema },
        }))
      : []

    const history = this.messageHistory.map(message => this.toGrokMessage(message))
    if (this.interrupted) {
      history.push({ role: 'user', content: INTERRUPTED_TURN_GUIDANCE })
      this.interrupted = false
    }

    const transcript: GrokMessage[] = [
      { role: 'system', content: options.systemPrompt ?? DEFAULT_SYSTEM_PROMPT },
      ...history,
      { role: 'user', content: goal },
    ]

    this.messageHistory.push({ role: 'user', content: goal, timestamp: Date.now() })
    const usedTools: ToolCall[] = []
    let recoverableToolFailures = 0

    try {
      for (let step = 1; step <= maxSteps; step++) {
        if (this.cancelled) {
          this.status = 'ready'
          return {
            content: 'Agent run cancelled.',
            status: 'error',
            metadata: { backend: 'grok-build', cancelled: true, steps: step - 1 },
          }
        }

        const response = await this.client.chat({
          messages: transcript,
          model: this.config.defaultModel || 'grok-3',
          stream: false,
          tools: tools.length ? tools : undefined,
          tool_choice: tools.length ? 'auto' : undefined,
        })

        const assistant = response.choices[0]?.message
        if (!assistant) throw new Error('Grok Build returned no assistant message')
        transcript.push(assistant)

        const toolCalls = assistant.tool_calls ?? []
        if (toolCalls.length === 0) {
          const finalContent = assistant.content || ''
          this.messageHistory.push({ role: 'assistant', content: finalContent, timestamp: Date.now() })
          this.status = 'ready'
          return {
            content: finalContent,
            tools: usedTools,
            status: 'complete',
            metadata: {
              backend: 'grok-build',
              model: response.model,
              steps: step,
              usage: response.usage,
              recoverableToolFailures,
            },
          }
        }

        const parsedCalls = toolCalls.map(call => ({
          source: call,
          parsed: {
            id: call.id,
            name: call.function.name,
            arguments: this.parseToolArguments(call.function.arguments),
          } satisfies ToolCall,
        }))
        usedTools.push(...parsedCalls.map(item => item.parsed))

        // GrokBot's mature loop treats same-turn tool calls as a batch. Run
        // independent calls concurrently, but cap fan-out to protect local/MCP
        // servers from unbounded model-generated parallelism.
        const maxConcurrent = Math.max(1, this.config.maxConcurrentTools ?? 5)
        for (let offset = 0; offset < parsedCalls.length; offset += maxConcurrent) {
          const batch = parsedCalls.slice(offset, offset + maxConcurrent)
          const observations = await Promise.all(batch.map(async ({ source, parsed }) => {
            if (this.cancelled) {
              return { id: source.id, observation: JSON.stringify({ success: false, cancelled: true }) }
            }

            try {
              const result = await mcp.callTool(parsed.name, parsed.arguments)
              return { id: source.id, observation: JSON.stringify(result) }
            } catch (error) {
              recoverableToolFailures++
              // Feed failures back as observations rather than killing the
              // entire run. The model can choose a different tool or repair
              // arguments on the next reason/action step.
              return {
                id: source.id,
                observation: JSON.stringify({
                  success: false,
                  error: error instanceof Error ? error.message : String(error),
                  recoverable: true,
                }),
              }
            }
          }))

          for (const observation of observations) {
            transcript.push({ role: 'tool', tool_call_id: observation.id, content: observation.observation })
            this.messageHistory.push({
              role: 'tool',
              toolCallId: observation.id,
              content: observation.observation,
              timestamp: Date.now(),
            })
          }
        }
      }

      this.status = 'ready'
      return {
        content: `Agent stopped after reaching the ${maxSteps}-step safety limit before producing a final answer.`,
        tools: usedTools,
        status: 'error',
        metadata: { backend: 'grok-build', maxStepsReached: true, steps: maxSteps, recoverableToolFailures },
      }
    } catch (error) {
      if (this.cancelled) {
        this.interrupted = true
        this.status = 'ready'
        return { content: 'Agent run cancelled.', tools: usedTools, status: 'error', metadata: { backend: 'grok-build', cancelled: true } }
      }
      this.status = 'error'
      return {
        content: error instanceof Error ? error.message : String(error),
        tools: usedTools,
        status: 'error',
        metadata: { backend: 'grok-build', recoverableToolFailures },
      }
    }
  }

  getStatus(): BackendStatus {
    return this.status
  }

  getHistory(): AgentMessage[] {
    return [...this.messageHistory]
  }

  clearHistory(): void {
    this.messageHistory = []
    this.interrupted = false
  }

  private parseToolArguments(value: string): Record<string, unknown> {
    if (!value.trim()) return {}
    try {
      const parsed = JSON.parse(value)
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) return parsed as Record<string, unknown>
      return { value: parsed }
    } catch {
      return { raw: value }
    }
  }

  private toGrokMessage(message: AgentMessage): GrokMessage {
    if (message.role === 'tool') {
      return {
        role: 'tool',
        content: message.content,
        ...(message.toolCallId ? { tool_call_id: message.toolCallId } : {}),
      }
    }
    return { role: message.role, content: message.content }
  }
}

let harnessInstance: GrokBuildHarness | null = null

export function getHarness(config?: Partial<GrokBuildConfig>): GrokBuildHarness {
  if (!harnessInstance) harnessInstance = new GrokBuildHarness(config)
  return harnessInstance
}
