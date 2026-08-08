/** OpenAI-compatible client for Duck Agent's primary Grok Build harness. */

import { loadConfig, type GrokBuildConfig } from './config'

export interface GrokToolCall {
  id: string
  type: 'function'
  function: { name: string; arguments: string }
}

export interface GrokMessage {
  role: 'system' | 'user' | 'assistant' | 'tool'
  content: string | null
  tool_call_id?: string
  tool_calls?: GrokToolCall[]
}

export interface GrokToolDefinition {
  type: 'function'
  function: { name: string; description?: string; parameters: Record<string, unknown> }
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
  usage?: { prompt_tokens: number; completion_tokens: number; total_tokens: number }
}

export interface GrokChoice {
  index: number
  message: GrokMessage
  finish_reason: string
}

const RETRYABLE_STATUS = new Set([408, 409, 425, 429, 500, 502, 503, 504])

export class GrokBuildAPIClient {
  private config: GrokBuildConfig
  private authHeader: string | null = null

  constructor(config?: Partial<GrokBuildConfig>) {
    this.config = { ...loadConfig(), ...config }
    if (this.config.apiKey) this.authHeader = `Bearer ${this.config.apiKey}`
  }

  isConfigured(): boolean {
    return this.authHeader !== null
  }

  async chat(request: GrokChatRequest): Promise<GrokChatResponse> {
    if (!this.isConfigured()) {
      throw new Error('Grok Build API key not configured. Set GROK_API_KEY environment variable.')
    }

    const attempts = Math.max(1, (this.config.requestRetries ?? 2) + 1)
    let lastError: unknown

    for (let attempt = 0; attempt < attempts; attempt++) {
      const controller = new AbortController()
      const timeout = setTimeout(() => controller.abort(), this.config.timeoutMs ?? 60_000)

      try {
        const response = await fetch(`${this.config.apiEndpoint}/v1/chat/completions`, {
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

        if (response.ok) return (await response.json()) as GrokChatResponse

        const body = await response.text()
        const error = new Error(`Grok Build API error (${response.status}): ${body}`)
        lastError = error
        if (!RETRYABLE_STATUS.has(response.status) || attempt === attempts - 1) throw error

        const retryAfter = parseRetryAfter(response.headers.get('retry-after'))
        await delay(retryAfter ?? retryDelay(this.config.retryBaseDelayMs ?? 350, attempt))
      } catch (error) {
        lastError = error
        const retryableNetworkError = isTransientNetworkError(error)
        if (!retryableNetworkError || attempt === attempts - 1) throw error
        await delay(retryDelay(this.config.retryBaseDelayMs ?? 350, attempt))
      } finally {
        clearTimeout(timeout)
      }
    }

    throw lastError instanceof Error ? lastError : new Error(String(lastError))
  }

  async sendMessage(message: string, systemPrompt?: string): Promise<string> {
    const messages: GrokMessage[] = []
    if (systemPrompt) messages.push({ role: 'system', content: systemPrompt })
    messages.push({ role: 'user', content: message })
    const response = await this.chat({ messages, model: this.config.defaultModel || 'grok-3' })
    return response.choices[0]?.message?.content || ''
  }

  getConfig(): GrokBuildConfig {
    return { ...this.config }
  }
}

function retryDelay(baseMs: number, attempt: number): number {
  const exponential = baseMs * 2 ** attempt
  return Math.round(exponential * (0.8 + Math.random() * 0.4))
}

function parseRetryAfter(value: string | null): number | null {
  if (!value) return null
  const seconds = Number(value)
  if (Number.isFinite(seconds)) return Math.max(0, seconds * 1000)
  const timestamp = Date.parse(value)
  return Number.isFinite(timestamp) ? Math.max(0, timestamp - Date.now()) : null
}

function isTransientNetworkError(error: unknown): boolean {
  if (!(error instanceof Error)) return false
  if (error.name === 'AbortError' || error.name === 'TimeoutError') return true
  const message = error.message.toLowerCase()
  return message.includes('fetch failed') || message.includes('network') || message.includes('socket') || message.includes('econnreset')
}

function delay(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

let clientInstance: GrokBuildAPIClient | null = null

export function getAPIClient(): GrokBuildAPIClient {
  if (!clientInstance) clientInstance = new GrokBuildAPIClient()
  return clientInstance
}
