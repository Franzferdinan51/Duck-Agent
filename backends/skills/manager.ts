/**
 * Duck Agent - Skill System
 * 
 * Allows the agent to learn and use skills over time.
 * Skills are reusable patterns that improve the agent's capabilities.
 */

export interface Skill {
  id: string
  name: string
  description: string
  category: string
  /** Code/function to execute when skill is used */
  execute: (input: SkillInput) => Promise<SkillOutput>
  /** Optional input schema */
  inputSchema?: Record<string, unknown>
  /** Tags for discovery */
  tags?: string[]
  /** Version */
  version: string
}

export interface SkillInput {
  [key: string]: unknown
}

export interface SkillOutput {
  success: boolean
  result?: unknown
  error?: string
  metadata?: Record<string, unknown>
}

/**
 * Skill Manager
 * 
 * Manages Duck Agent's skills - reusable patterns and capabilities.
 */
export class SkillManager {
  private skills: Map<string, Skill>
  private usageStats: Map<string, number>

  constructor() {
    this.skills = new Map()
    this.usageStats = new Map()
  }

  /**
   * Register a new skill
   */
  registerSkill(skill: Skill): void {
    this.skills.set(skill.id, skill)
  }

  /**
   * Unregister a skill
   */
  unregisterSkill(id: string): void {
    this.skills.delete(id)
    this.usageStats.delete(id)
  }

  /**
   * Get a skill by ID
   */
  getSkill(id: string): Skill | null {
    return this.skills.get(id) || null
  }

  /**
   * Find skills by category
   */
  findByCategory(category: string): Skill[] {
    return Array.from(this.skills.values()).filter(s => s.category === category)
  }

  /**
   * Find skills by tag
   */
  findByTag(tag: string): Skill[] {
    return Array.from(this.skills.values()).filter(s => s.tags?.includes(tag))
  }

  /**
   * Search skills by keyword
   */
  search(keyword: string): Skill[] {
    const lower = keyword.toLowerCase()
    return Array.from(this.skills.values()).filter(s =>
      s.name.toLowerCase().includes(lower) ||
      s.description.toLowerCase().includes(lower) ||
      s.tags?.some(t => t.toLowerCase().includes(lower))
    )
  }

  /**
   * Execute a skill
   */
  async executeSkill(id: string, input: SkillInput): Promise<SkillOutput> {
    const skill = this.skills.get(id)
    if (!skill) {
      return {
        success: false,
        error: `Skill not found: ${id}`,
      }
    }

    // Track usage
    const currentCount = this.usageStats.get(id) || 0
    this.usageStats.set(id, currentCount + 1)

    try {
      const result = await skill.execute(input)
      // The skill's execute returns a SkillOutput, so we return it
      return result
    } catch (err) {
      return {
        success: false,
        error: err instanceof Error ? err.message : String(err),
      }
    }
  }

  /**
   * Get all skills
   */
  getAllSkills(): Skill[] {
    return Array.from(this.skills.values())
  }

  /**
   * Get usage statistics
   */
  getUsageStats(): Record<string, number> {
    return Object.fromEntries(this.usageStats)
  }

  /**
   * Get most used skills
   */
  getMostUsedSkills(limit: number = 5): Skill[] {
    return Array.from(this.usageStats.entries())
      .sort((a, b) => b[1] - a[1])
      .slice(0, limit)
      .map(([id]) => this.skills.get(id)!)
      .filter(Boolean)
  }

  /**
   * Total skills count
   */
  count(): number {
    return this.skills.size
  }
}

/**
 * Built-in skills for Duck Agent
 */
export function registerBuiltinSkills(manager: SkillManager): void {
  // Coding skill
  manager.registerSkill({
    id: 'coding',
    name: 'Coding',
    description: 'Help with coding tasks including writing, debugging, and reviewing code',
    category: 'development',
    version: '1.0.0',
    tags: ['code', 'programming', 'development'],
    execute: async (input) => {
      return {
        success: true,
        result: `Code analysis: ${JSON.stringify(input)}`,
      }
    },
  })

  // Web search skill
  manager.registerSkill({
    id: 'web-search',
    name: 'Web Search',
    description: 'Search the web for information',
    category: 'research',
    version: '1.0.0',
    tags: ['search', 'web', 'research'],
    execute: async (input) => {
      return {
        success: true,
        result: `Search results for: ${JSON.stringify(input)}`,
      }
    },
  })

  // File operations skill
  manager.registerSkill({
    id: 'file-ops',
    name: 'File Operations',
    description: 'Read, write, and manipulate files',
    category: 'productivity',
    version: '1.0.0',
    tags: ['files', 'productivity'],
    execute: async (input) => {
      return {
        success: true,
        result: `File operation: ${JSON.stringify(input)}`,
      }
    },
  })

  // Task planning skill
  manager.registerSkill({
    id: 'task-planning',
    name: 'Task Planning',
    description: 'Break down complex tasks into manageable steps',
    category: 'productivity',
    version: '1.0.0',
    tags: ['planning', 'tasks', 'productivity'],
    execute: async (input) => {
      return {
        success: true,
        result: `Task plan: ${JSON.stringify(input)}`,
      }
    },
  })
}

let managerInstance: SkillManager | null = null

export function getSkillManager(): SkillManager {
  if (!managerInstance) {
    managerInstance = new SkillManager()
    registerBuiltinSkills(managerInstance)
  }
  return managerInstance
}
