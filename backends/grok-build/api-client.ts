/**
 * Duck Agent - Grok Build REST API Client
 * 
 * Real implementation of the Grok Build API client.
 */

import { loadConfig, type GrokBuildConfig } from './config'

export interface GrokMessage {
  role: 'system' | 'user' | 'assistant'
  content: string
}

export interface GrokChatRequest {
  messages: GrokMessage[]
  model: string
  temperature?: number
  max_tokens?: number
  stream?: boolean
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

/**
 * Grok Build API Client
 * 
 * Real HTTP client for the Grok Build API.
 */
export class GrokBuildAPIClient {
  private config: GrokBuildConfig
  private authHeader: string | null = null

  constructor(config?: Partial<GrokBuildConfig>) {
    this.config = { ...loadConfig(), ...config }
    if (this.config.apiKey) {
      this.authHeader = `Bearer ${this.config.apiKey}`
    }
  }

  /**
   * Check if the API client is configured
   */
  isConfigured(): boolean {
    return this.authHeader !== null
  }

  /**
   * Send a chat completion request
   */
  async chat(request: GrokChatRequest): Promise<GrokChatResponse> {
    if (!this.isConfigured()) {
      throw new Error('Grok Build API key not configured. Set GROK_API_KEY environment variable.')
    }

    const url = `${this.config.apiEndpoint}/v1/chat/completions`
    
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': this.authHeader!,
      },
      body: JSON.stringify({
        model: request.model,
        messages: request.messages,
        temperature: request.temperature ?? 0.7,
        max_tokens: request.max_tokens ?? 2048,
        stream: request.stream ?? false,
      }),
    })

    if (!response.ok) {
      const errorText = await response.text()
      throw new Error(`Grok Build API error (${response.status}): ${errorText}`)
    }

    return await response.json() as GrokChatResponse
  }

  /**
   * Simple convenience method for sending a single message
   */
  async sendMessage(message: string, systemPrompt?: string): Promise<string> {
    const messages: GrokMessage[] = []
    
    if (systemPrompt) {
      messages.push({ role: 'system', content: systemPrompt })
    }
    
    messages.push({ role: 'user', content: message })

    const response = await this.chat({
      messages,
      model: this.config.defaultModel || 'grok-3',
    })

    return response.choices[0]?.message?.content || ''
  }

  /**
   * Get the current configuration
   */
  getConfig(): GrokBuildConfig {
    return { ...this.config }
  }
}

let clientInstance: GrokBuildAPIClient | null = null

export function getAPIClient(): GrokBuildAPIClient {
  if (!clientInstance) {
    clientInstance = new GrokBuildAPIClient()
  }
  return clientInstance
}
