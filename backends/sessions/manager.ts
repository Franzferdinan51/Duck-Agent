/**
 * Duck Agent - Session Management
 * 
 * Manages agent sessions - persistent conversations with the agent.
 */

import { randomUUID } from 'crypto'

export interface SessionMessage {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  timestamp: number
  metadata?: Record<string, unknown>
}

export interface Session {
  id: string
  name: string
  createdAt: number
  updatedAt: number
  messages: SessionMessage[]
  metadata: Record<string, unknown>
  /** Backend used for this session */
  backend: string
}

export interface Memory {
  id: string
  key: string
  value: string
  createdAt: number
  tags?: string[]
}

/**
 * Session Manager
 * 
 * Manages Duck Agent sessions and persistent memory.
 */
export class SessionManager {
  private sessions: Map<string, Session>
  private memories: Map<string, Memory>
  private activeSessionId: string | null

  constructor() {
    this.sessions = new Map()
    this.memories = new Map()
    this.activeSessionId = null
  }

  /**
   * Create a new session
   */
  createSession(name: string, backend: string = 'grok-build'): Session {
    const now = Date.now()
    const session: Session = {
      id: randomUUID(),
      name,
      createdAt: now,
      updatedAt: now,
      messages: [],
      metadata: {},
      backend,
    }
    this.sessions.set(session.id, session)
    this.activeSessionId = session.id
    return session
  }

  /**
   * Get a session by ID
   */
  getSession(id: string): Session | null {
    return this.sessions.get(id) || null
  }

  /**
   * Get the active session
   */
  getActiveSession(): Session | null {
    if (!this.activeSessionId) return null
    return this.sessions.get(this.activeSessionId) || null
  }

  /**
   * Set the active session
   */
  setActiveSession(id: string): boolean {
    if (!this.sessions.has(id)) {
      return false
    }
    this.activeSessionId = id
    return true
  }

  /**
   * Add a message to a session
   */
  addMessage(sessionId: string, role: SessionMessage['role'], content: string, metadata?: Record<string, unknown>): SessionMessage | null {
    const session = this.sessions.get(sessionId)
    if (!session) return null

    const message: SessionMessage = {
      id: randomUUID(),
      role,
      content,
      timestamp: Date.now(),
      metadata,
    }
    session.messages.push(message)
    session.updatedAt = Date.now()
    return message
  }

  /**
   * Get messages from a session
   */
  getMessages(sessionId: string): SessionMessage[] {
    const session = this.sessions.get(sessionId)
    return session ? [...session.messages] : []
  }

  /**
   * Clear messages from a session
   */
  clearSession(sessionId: string): boolean {
    const session = this.sessions.get(sessionId)
    if (!session) return false
    session.messages = []
    session.updatedAt = Date.now()
    return true
  }

  /**
   * Delete a session
   */
  deleteSession(id: string): boolean {
    if (this.activeSessionId === id) {
      this.activeSessionId = null
    }
    return this.sessions.delete(id)
  }

  /**
   * List all sessions
   */
  listSessions(): Session[] {
    return Array.from(this.sessions.values())
      .sort((a, b) => b.updatedAt - a.updatedAt)
  }

  /**
   * Store a memory
   */
  remember(key: string, value: string, tags?: string[]): Memory {
    const memory: Memory = {
      id: randomUUID(),
      key,
      value,
      createdAt: Date.now(),
      tags,
    }
    this.memories.set(key, memory)
    return memory
  }

  /**
   * Recall a memory
   */
  recall(key: string): Memory | null {
    return this.memories.get(key) || null
  }

  /**
   * Forget a memory
   */
  forget(key: string): boolean {
    return this.memories.delete(key)
  }

  /**
   * List all memories
   */
  listMemories(): Memory[] {
    return Array.from(this.memories.values())
  }

  /**
   * Search memories by tag
   */
  searchMemoriesByTag(tag: string): Memory[] {
    return Array.from(this.memories.values()).filter(m => m.tags?.includes(tag))
  }

  /**
   * Get statistics
   */
  getStats(): {
    sessionCount: number
    memoryCount: number
    activeSessionId: string | null
    totalMessages: number
  } {
    const totalMessages = Array.from(this.sessions.values())
      .reduce((sum, s) => sum + s.messages.length, 0)
    return {
      sessionCount: this.sessions.size,
      memoryCount: this.memories.size,
      activeSessionId: this.activeSessionId,
      totalMessages,
    }
  }
}

let managerInstance: SessionManager | null = null

export function getSessionManager(): SessionManager {
  if (!managerInstance) {
    managerInstance = new SessionManager()
  }
  return managerInstance
}
