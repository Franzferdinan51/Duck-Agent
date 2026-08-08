/**
 * Duck Agent - Grok Build REST API Client
 *
 * OpenAI-compatible chat-completions client used by the primary Grok Build
 * harness. The client exposes tool/function calling primitives so the runtime
 * can own an iterative agent loop instead of behaving like a one-shot chatbot.
 */

import { loadConfig, type GrokBuildConfig } from './config'

export interface GrokToolCall {
  id: string
  type: 'function'
  function: {
    name: string
    arguments: string
  }
}

export interface GrokMessage {
  role: 'system' | 'user' | 'assistant' | 'tool'
  content: string | null
  tool_call_id?: string
  tool_calls?: GrokToolCall[]
}

export interface GrokToolDefinition {
  type: 'function'
  function: {
    name: string
    description?: string
    parameters: Record<string, unknown>
  }
}

export interface GrokChatRequest {
  messages: GrokMessage[]
  model: string
  temperature?: number
  max_tokens?: number
  stream?: boolean
  tools?: GrokToolDefinition[]
  tool_choice?: 'auto' | 'none'
}

export interface GrokChatResponse {
  id: string
  object: string
  created: number
  model: string
  choices: GrokChoice[]
  usage?: {
    prompt_tokens: number
    completion_tokens: number
    total_tokens: number
  }
}

export interface GrokChoice {
  index: number
  message: GrokMessage
  finish_reason: string
}

/** Real HTTP client for the Grok Build-compatible API. */
export class GrokBuildAPIClient {
  private config: GrokBuildConfig
  private authHeader: string | null = null

  constructor(config?: Partial<GrokBuildConfig>) {
    this.config = { ...loadConfig(), ...config }
    if (this.config.apiKey) {
      this.authHeader = `Bearer ${this.config.apiKey}`
    }
  }

  isConfigured(): boolean {
    return this.authHeader !== null
  }

  async chat(request: GrokChatRequest): Promise<GrokChatResponse> {
    if (!this.isConfigured()) {
      throw new Error('Grok Build API key not configured. Set GROK_API_KEY environment variable.')
    }

    const url = `${this.config.apiEndpoint}/v1/chat/completions`
    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), this.config.timeoutMs ?? 60000)

    try {
      const response = await fetch(url, {
        method: 'POST',
        signal: controller.signal,
        headers: {
          'Content-Type': 'application/json',
          Authorization: this.authHeader!,
        },
        body: JSON.stringify({
          model: request.model,
          messages: request.messages,
          temperature: request.temperature ?? 0.7,
          max_tokens: request.max_tokens ?? 4096,
          stream: request.stream ?? false,
          ...(request.tools?.length ? { tools: request.tools } : {}),
          ...(request.tools?.length ? { tool_choice: request.tool_choice ?? 'auto' } : {}),
        }),
      })

      if (!response.ok) {
        const errorText = await response.text()
        throw new Error(`Grok Build API error (${response.status}): ${errorText}`)
      }

      return (await response.json()) as GrokChatResponse
    } finally {
      clearTimeout(timeout)
    }
  }

  async sendMessage(message: string, systemPrompt?: string): Promise<string> {
    const messages: GrokMessage[] = []
    if (systemPrompt) messages.push({ role: 'system', content: systemPrompt })
    messages.push({ role: 'user', content: message })

    const response = await this.chat({
      messages,
      model: this.config.defaultModel || 'grok-3',
    })

    return response.choices[0]?.message?.content || ''
  }

  getConfig(): GrokBuildConfig {
    return { ...this.config }
  }
}

let clientInstance: GrokBuildAPIClient | null = null

export function getAPIClient(): GrokBuildAPIClient {
  if (!clientInstance) clientInstance = new GrokBuildAPIClient()
  return clientInstance
}
