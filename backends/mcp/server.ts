/**
 * Duck Agent - MCP Server Integration
 * 
 * Model Context Protocol (MCP) server integration for Duck Agent.
 * Allows the agent to use MCP tools for extended capabilities.
 */

export interface MCPServerConfig {
  name: string
  command: string
  args: string[]
  env?: Record<string, string>
  enabled?: boolean
}

export interface MCPTool {
  name: string
  description: string
  inputSchema: Record<string, unknown>
}

export interface MCPToolCall {
  name: string
  arguments: Record<string, unknown>
}

export interface MCPToolResult {
  content: Array<{
    type: 'text' | 'image' | 'resource'
    text?: string
    data?: string
    mimeType?: string
  }>
  isError?: boolean
}

/**
 * MCP Server Manager
 * 
 * Manages MCP servers and tool calls for Duck Agent.
 */
export class MCPServerManager {
  private servers: Map<string, MCPServerConfig>
  private tools: Map<string, MCPTool[]>

  constructor() {
    this.servers = new Map()
    this.tools = new Map()
  }

  /**
   * Register an MCP server
   */
  registerServer(config: MCPServerConfig): void {
    this.servers.set(config.name, {
      ...config,
      enabled: config.enabled ?? true,
    })
  }

  /**
   * Unregister an MCP server
   */
  unregisterServer(name: string): void {
    this.servers.delete(name)
    this.tools.delete(name)
  }

  /**
   * Get all registered servers
   */
  getServers(): MCPServerConfig[] {
    return Array.from(this.servers.values())
  }

  /**
   * Get enabled servers
   */
  getEnabledServers(): MCPServerConfig[] {
    return this.getServers().filter(s => s.enabled)
  }

  /**
   * Register tools from a server
   */
  registerTools(serverName: string, tools: MCPTool[]): void {
    this.tools.set(serverName, tools)
  }

  /**
   * Get all available tools
   */
  getAllTools(): MCPTool[] {
    const all: MCPTool[] = []
    for (const [serverName, tools] of this.tools.entries()) {
      const server = this.servers.get(serverName)
      if (server?.enabled) {
        all.push(...tools)
      }
    }
    return all
  }

  /**
   * Find a tool by name
   */
  findTool(name: string): { server: string; tool: MCPTool } | null {
    for (const [serverName, tools] of this.tools.entries()) {
      const tool = tools.find(t => t.name === name)
      if (tool) {
        return { server: serverName, tool }
      }
    }
    return null
  }

  /**
   * Call a tool
   */
  async callTool(toolName: string, args: Record<string, unknown>): Promise<MCPToolResult> {
    const found = this.findTool(toolName)
    if (!found) {
      throw new Error(`Tool not found: ${toolName}`)
    }

    // In a real implementation, this would call the MCP server
    return {
      content: [
        {
          type: 'text',
          text: `Duck Agent: Tool ${toolName} executed with args ${JSON.stringify(args)}`,
        },
      ],
    }
  }

  /**
   * Get statistics
   */
  getStats(): {
    serverCount: number
    enabledCount: number
    toolCount: number
  } {
    return {
      serverCount: this.servers.size,
      enabledCount: this.getEnabledServers().length,
      toolCount: this.getAllTools().length,
    }
  }
}

// Built-in Duck Agent MCP servers
export const BUILTIN_MCP_SERVERS: MCPServerConfig[] = [
  {
    name: 'duck-agent-files',
    command: 'duck-agent-mcp-files',
    args: [],
    enabled: true,
  },
  {
    name: 'duck-agent-git',
    command: 'duck-agent-mcp-git',
    args: [],
    enabled: true,
  },
  {
    name: 'duck-agent-web',
    command: 'duck-agent-mcp-web',
    args: [],
    enabled: true,
  },
]

let managerInstance: MCPServerManager | null = null

export function getMCPManager(): MCPServerManager {
  if (!managerInstance) {
    managerInstance = new MCPServerManager()
    // Register built-in servers
    for (const server of BUILTIN_MCP_SERVERS) {
      managerInstance.registerServer(server)
    }
  }
  return managerInstance
}
