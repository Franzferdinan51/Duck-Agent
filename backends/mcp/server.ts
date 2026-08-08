/**
 * Duck Agent - MCP / Tool Integration
 *
 * The manager is the runtime-facing tool registry. Real MCP transports can
 * populate it, while in-process tools can register executable handlers now.
 * This keeps Grok Build's agent loop independent from any one transport.
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

export type MCPToolHandler = (
  args: Record<string, unknown>,
) => Promise<MCPToolResult> | MCPToolResult

interface RegisteredTool {
  server: string
  tool: MCPTool
  handler?: MCPToolHandler
}

export class MCPServerManager {
  private servers = new Map<string, MCPServerConfig>()
  private tools = new Map<string, RegisteredTool>()

  registerServer(config: MCPServerConfig): void {
    this.servers.set(config.name, { ...config, enabled: config.enabled ?? true })
  }

  unregisterServer(name: string): void {
    this.servers.delete(name)
    for (const [toolName, entry] of this.tools.entries()) {
      if (entry.server === name) this.tools.delete(toolName)
    }
  }

  getServers(): MCPServerConfig[] {
    return Array.from(this.servers.values())
  }

  getEnabledServers(): MCPServerConfig[] {
    return this.getServers().filter(server => server.enabled)
  }

  /** Register metadata discovered from an MCP server. */
  registerTools(serverName: string, tools: MCPTool[]): void {
    for (const tool of tools) {
      const existing = this.tools.get(tool.name)
      this.tools.set(tool.name, {
        server: serverName,
        tool,
        handler: existing?.handler,
      })
    }
  }

  /** Register an executable tool, useful for local tools and MCP adapters. */
  registerTool(
    serverName: string,
    tool: MCPTool,
    handler: MCPToolHandler,
  ): void {
    this.tools.set(tool.name, { server: serverName, tool, handler })
  }

  getAllTools(): MCPTool[] {
    return Array.from(this.tools.values())
      .filter(entry => this.servers.get(entry.server)?.enabled !== false)
      .map(entry => entry.tool)
  }

  findTool(name: string): { server: string; tool: MCPTool } | null {
    const entry = this.tools.get(name)
    if (!entry) return null
    if (this.servers.get(entry.server)?.enabled === false) return null
    return { server: entry.server, tool: entry.tool }
  }

  async callTool(
    toolName: string,
    args: Record<string, unknown>,
  ): Promise<MCPToolResult> {
    const entry = this.tools.get(toolName)
    if (!entry || this.servers.get(entry.server)?.enabled === false) {
      throw new Error(`Tool not found or disabled: ${toolName}`)
    }

    if (!entry.handler) {
      throw new Error(
        `Tool ${toolName} is registered but has no executable transport/handler. ` +
          'Connect the MCP stdio/HTTP transport before exposing this tool to the model.',
      )
    }

    try {
      return await entry.handler(args)
    } catch (error) {
      return {
        isError: true,
        content: [
          {
            type: 'text',
            text: error instanceof Error ? error.message : String(error),
          },
        ],
      }
    }
  }

  getStats(): { serverCount: number; enabledCount: number; toolCount: number } {
    return {
      serverCount: this.servers.size,
      enabledCount: this.getEnabledServers().length,
      toolCount: this.getAllTools().length,
    }
  }
}

/**
 * Built-in transport definitions. These are configuration targets, not fake
 * executable tools. A transport adapter must connect them and register the
 * discovered tools/handlers before the agent can call them.
 */
export const BUILTIN_MCP_SERVERS: MCPServerConfig[] = [
  { name: 'duck-agent-files', command: 'duck-agent-mcp-files', args: [], enabled: true },
  { name: 'duck-agent-git', command: 'duck-agent-mcp-git', args: [], enabled: true },
  { name: 'duck-agent-web', command: 'duck-agent-mcp-web', args: [], enabled: true },
]

let managerInstance: MCPServerManager | null = null

export function getMCPManager(): MCPServerManager {
  if (!managerInstance) {
    managerInstance = new MCPServerManager()
    for (const server of BUILTIN_MCP_SERVERS) managerInstance.registerServer(server)
  }
  return managerInstance
}
