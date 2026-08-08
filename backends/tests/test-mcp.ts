/**
 * Tests for the Duck Agent MCP/tool manager.
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
  if (actual !== expected) throw new Error(`${msg}Expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`)
}

function assertTrue(value: boolean, msg = ''): void {
  if (!value) throw new Error(`${msg}Expected true, got ${value}`)
}

console.log('Duck Agent MCP / Tool Tests')
console.log('===========================')

async function main() {
await test('creates new MCP manager', () => {
  assertTrue(new MCPServerManager() !== null)
})

await test('registers and unregisters a server', () => {
  const manager = new MCPServerManager()
  manager.registerServer({ name: 'test-server', command: 'test-cmd', args: [] })
  assertEqual(manager.getServers().length, 1)
  manager.unregisterServer('test-server')
  assertEqual(manager.getServers().length, 0)
})

await test('registers metadata-only tools', () => {
  const manager = new MCPServerManager()
  manager.registerServer({ name: 'test-server', command: 'test-cmd', args: [] })
  manager.registerTools('test-server', [
    { name: 'test-tool', description: 'A test tool', inputSchema: { type: 'object' } },
  ])
  assertEqual(manager.getAllTools().length, 1)
})

await test('executes a registered tool handler', async () => {
  const manager = new MCPServerManager()
  manager.registerServer({ name: 'test-server', command: 'test-cmd', args: [] })
  manager.registerTool(
    'test-server',
    { name: 'echo', description: 'Echo a value', inputSchema: { type: 'object' } },
    async args => ({ content: [{ type: 'text', text: String(args.value) }] }),
  )

  const result = await manager.callTool('echo', { value: 'hello' })
  assertEqual(result.content[0]?.text, 'hello')
})

await test('does not fake execution for metadata-only tools', async () => {
  const manager = new MCPServerManager()
  manager.registerServer({ name: 'test-server', command: 'test-cmd', args: [] })
  manager.registerTools('test-server', [
    { name: 'metadata-only', description: 'No transport', inputSchema: {} },
  ])

  let threw = false
  try {
    await manager.callTool('metadata-only', {})
  } catch {
    threw = true
  }
  assertTrue(threw, 'Metadata-only tools must not pretend to execute')
})

await test('returns tool errors as observations', async () => {
  const manager = new MCPServerManager()
  manager.registerServer({ name: 'test-server', command: 'test-cmd', args: [] })
  manager.registerTool(
    'test-server',
    { name: 'fails', description: 'Always fails', inputSchema: {} },
    async () => {
      throw new Error('boom')
    },
  )
  const result = await manager.callTool('fails', {})
  assertEqual(result.isError, true)
  assertTrue(result.content[0]?.text?.includes('boom') === true)
})

await test('disabled servers hide their tools', () => {
  const manager = new MCPServerManager()
  manager.registerServer({ name: 'disabled', command: 'cmd', args: [], enabled: false })
  manager.registerTool(
    'disabled',
    { name: 'hidden', description: 'Hidden', inputSchema: {} },
    async () => ({ content: [{ type: 'text', text: 'nope' }] }),
  )
  assertEqual(manager.getAllTools().length, 0)
  assertEqual(manager.findTool('hidden'), null)
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

await test('singleton manager has built-in server definitions', () => {
  const m1 = getMCPManager()
  const m2 = getMCPManager()
  assertTrue(m1 === m2)
  assertTrue(m1.getServers().length > 0)
})

await test('built-in servers are defined', () => {
  assertTrue(BUILTIN_MCP_SERVERS.length > 0)
  for (const server of BUILTIN_MCP_SERVERS) {
    assertTrue(Boolean(server.name))
    assertTrue(Boolean(server.command))
  }
})

console.log('\n===========================')
console.log(`Results: ${passCount} passed, ${failCount} failed`)
process.exitCode = failCount > 0 ? 1 : 0
}

main().catch(error => {
  console.error(error)
  process.exitCode = 1
})
