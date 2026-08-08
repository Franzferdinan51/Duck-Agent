/**
 * Tests for the Duck Agent MCP server manager.
 * 
 * Run with: npx tsx backends/tests/test-mcp.ts
 */

import { MCPServerManager, getMCPManager, BUILTIN_MCP_SERVERS } from '../mcp/server'

let passCount = 0
let failCount = 0

async function test(name: string, fn: () => void | Promise<void>) {
  try {
    await fn()
    console.log(`  ✓ ${name}`)
    passCount++
  } catch (err) {
    console.error(`  ✗ ${name}`)
    console.error(`    ${err instanceof Error ? err.message : err}`)
    failCount++
  }
}

function assertEqual<T>(actual: T, expected: T, msg = ''): void {
  if (actual !== expected) {
    throw new Error(`${msg}Expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`)
  }
}

async function assertTrue(value: boolean, msg = ''): Promise<void> {
  if (!value) {
    throw new Error(`${msg}Expected true, got ${value}`)
  }
}

console.log('Duck Agent MCP Server Tests')
console.log('===========================')

await test('creates new MCP manager', () => {
  const manager = new MCPServerManager()
  assertTrue(manager !== null)
})

await test('registers a server', () => {
  const manager = new MCPServerManager()
  manager.registerServer({
    name: 'test-server',
    command: 'test-cmd',
    args: [],
  })
  assertEqual(manager.getServers().length, 1)
})

await test('unregisters a server', () => {
  const manager = new MCPServerManager()
  manager.registerServer({
    name: 'test-server',
    command: 'test-cmd',
    args: [],
  })
  manager.unregisterServer('test-server')
  assertEqual(manager.getServers().length, 0)
})

await test('registers tools', () => {
  const manager = new MCPServerManager()
  manager.registerServer({
    name: 'test-server',
    command: 'test-cmd',
    args: [],
  })
  manager.registerTools('test-server', [
    {
      name: 'test-tool',
      description: 'A test tool',
      inputSchema: {},
    },
  ])
  assertEqual(manager.getAllTools().length, 1)
})

await test('finds a tool by name', () => {
  const manager = new MCPServerManager()
  manager.registerServer({
    name: 'test-server',
    command: 'test-cmd',
    args: [],
  })
  manager.registerTools('test-server', [
    { name: 'tool1', description: 'Tool 1', inputSchema: {} },
    { name: 'tool2', description: 'Tool 2', inputSchema: {} },
  ])
  const found = manager.findTool('tool2')
  if (!found) throw new Error('Should find tool')
  assertEqual(found.tool.name, 'tool2')
  assertEqual(found.server, 'test-server')
})

await test('returns null for unknown tool', () => {
  const manager = new MCPServerManager()
  const found = manager.findTool('unknown')
  if (found !== null) throw new Error('Should return null')
})

await test('calls a tool', async () => {
  const manager = new MCPServerManager()
  manager.registerServer({
    name: 'test-server',
    command: 'test-cmd',
    args: [],
  })
  manager.registerTools('test-server', [
    { name: 'test-tool', description: 'Test', inputSchema: {} },
  ])
  const result = await manager.callTool('test-tool', { foo: 'bar' })
  assertEqual(result.isError, undefined)
  assertTrue(result.content.length > 0)
})

await test('throws when calling unknown tool', async () => {
  const manager = new MCPServerManager()
  let threw = false
  try {
    await manager.callTool('unknown', {})
  } catch (err) {
    threw = true
  }
  await assertTrue(threw, 'Should throw')
})

await test('tracks enabled servers', () => {
  const manager = new MCPServerManager()
  manager.registerServer({ name: 'enabled', command: 'cmd', args: [], enabled: true })
  manager.registerServer({ name: 'disabled', command: 'cmd', args: [], enabled: false })
  assertEqual(manager.getEnabledServers().length, 1)
  assertEqual(manager.getEnabledServers()[0].name, 'enabled')
})

await test('returns stats', () => {
  const manager = new MCPServerManager()
  manager.registerServer({ name: 'server1', command: 'cmd', args: [] })
  manager.registerServer({ name: 'server2', command: 'cmd', args: [], enabled: false })
  manager.registerTools('server1', [
    { name: 'tool1', description: 't1', inputSchema: {} },
    { name: 'tool2', description: 't2', inputSchema: {} },
  ])
  const stats = manager.getStats()
  assertEqual(stats.serverCount, 2)
  assertEqual(stats.enabledCount, 1)
  assertEqual(stats.toolCount, 2)
})

await test('singleton manager has built-in servers', () => {
  const m1 = getMCPManager()
  const m2 = getMCPManager()
  if (m1 !== m2) throw new Error('Should be singleton')
  if (m1.getServers().length === 0) throw new Error('Should have built-in servers')
})

await test('built-in servers are defined', () => {
  if (BUILTIN_MCP_SERVERS.length === 0) throw new Error('Should have built-in servers')
  for (const server of BUILTIN_MCP_SERVERS) {
    if (!server.name) throw new Error('Server missing name')
    if (!server.command) throw new Error('Server missing command')
  }
})

console.log('\n===========================')
console.log(`Results: ${passCount} passed, ${failCount} failed`)
process.exit(failCount > 0 ? 1 : 0)
