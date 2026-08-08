/**
 * Tests for the GrokBuildHarness autonomous loop.
 * Run with: npx tsx backends/tests/test-harness.ts
 */

import { GrokBuildHarness, getHarness } from '../grok-build/harness'
import { getMCPManager } from '../mcp/server'

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

function makeResponse(message: Record<string, unknown>, model = 'grok-test') {
  return new Response(JSON.stringify({
    id: 'test-response',
    object: 'chat.completion',
    created: Date.now(),
    model,
    choices: [{ index: 0, message, finish_reason: 'stop' }],
    usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
  }), { status: 200, headers: { 'Content-Type': 'application/json' } })
}

console.log('Duck Agent Grok Build Harness Tests')
console.log('====================================')

async function main() {
  await test('creates new harness instance', () => {
    const harness = new GrokBuildHarness({ apiKey: 'test-key' })
    assertEqual(harness.getStatus(), 'idle')
  })

  await test('starts and stops configured harness', async () => {
    const harness = new GrokBuildHarness({ apiKey: 'test-key' })
    await harness.start()
    assertEqual(harness.getStatus(), 'ready')
    await harness.stop()
    assertEqual(harness.getStatus(), 'stopped')
  })

  await test('rejects an unconfigured harness at start', async () => {
    const harness = new GrokBuildHarness({ apiKey: undefined })
    let threw = false
    try {
      await harness.start()
    } catch {
      threw = true
    }
    assertTrue(threw)
  })

  await test('performs a real model round trip through the client', async () => {
    const originalFetch = globalThis.fetch
    globalThis.fetch = async () => makeResponse({ role: 'assistant', content: 'Hello from Grok Build' })

    try {
      const harness = new GrokBuildHarness({ apiKey: 'test-key', defaultModel: 'grok-test' })
      await harness.start()
      const response = await harness.sendMessage('Hello')
      assertEqual(response.content, 'Hello from Grok Build')
      assertEqual(response.status, 'complete')
      assertEqual(response.metadata?.backend, 'grok-build')
      assertEqual(response.metadata?.model, 'grok-test')
    } finally {
      globalThis.fetch = originalFetch
    }
  })

  await test('executes tool call and feeds observation back to model', async () => {
    const mcp = getMCPManager()
    mcp.registerServer({ name: 'test-runtime', command: 'in-process', args: [], enabled: true })
    mcp.registerTool(
      'test-runtime',
      {
        name: 'lookup_value',
        description: 'Look up a value',
        inputSchema: {
          type: 'object',
          properties: { key: { type: 'string' } },
          required: ['key'],
        },
      },
      async args => ({ content: [{ type: 'text', text: `value:${String(args.key)}` }] }),
    )

    const requests: any[] = []
    let calls = 0
    const originalFetch = globalThis.fetch
    globalThis.fetch = async (_url, init) => {
      calls++
      const body = JSON.parse(String(init?.body || '{}'))
      requests.push(body)

      if (calls === 1) {
        return makeResponse({
          role: 'assistant',
          content: null,
          tool_calls: [{
            id: 'call-1',
            type: 'function',
            function: { name: 'lookup_value', arguments: '{"key":"duck"}' },
          }],
        })
      }

      return makeResponse({ role: 'assistant', content: 'The lookup returned value:duck.' })
    }

    try {
      const harness = new GrokBuildHarness({ apiKey: 'test-key', defaultModel: 'grok-test', maxAgentSteps: 5 })
      await harness.start()
      const response = await harness.runGoal('Find the duck value')

      assertEqual(response.status, 'complete')
      assertEqual(response.tools?.length, 1)
      assertEqual(response.tools?.[0]?.name, 'lookup_value')
      assertEqual(calls, 2)

      const secondMessages = requests[1].messages as Array<Record<string, unknown>>
      const toolObservation = secondMessages.find(message => message.role === 'tool')
      assertTrue(Boolean(toolObservation), 'Second model call should include the tool observation')
      assertTrue(String(toolObservation?.content).includes('value:duck'))
    } finally {
      globalThis.fetch = originalFetch
    }
  })

  await test('throws when sending before start', async () => {
    const harness = new GrokBuildHarness({ apiKey: 'test-key' })
    let threw = false
    try {
      await harness.sendMessage('hello')
    } catch {
      threw = true
    }
    assertTrue(threw)
  })

  await test('tracks user and assistant history', async () => {
    const originalFetch = globalThis.fetch
    globalThis.fetch = async () => makeResponse({ role: 'assistant', content: 'done' })
    try {
      const harness = new GrokBuildHarness({ apiKey: 'test-key' })
      await harness.start()
      await harness.sendMessage('msg1')
      const history = harness.getHistory()
      assertEqual(history.length, 2)
      assertEqual(history[0].role, 'user')
      assertEqual(history[1].role, 'assistant')
    } finally {
      globalThis.fetch = originalFetch
    }
  })

  await test('clears history', async () => {
    const originalFetch = globalThis.fetch
    globalThis.fetch = async () => makeResponse({ role: 'assistant', content: 'done' })
    try {
      const harness = new GrokBuildHarness({ apiKey: 'test-key' })
      await harness.start()
      await harness.sendMessage('msg1')
      harness.clearHistory()
      assertEqual(harness.getHistory().length, 0)
    } finally {
      globalThis.fetch = originalFetch
    }
  })

  await test('singleton harness instance', () => {
    const h1 = getHarness({ apiKey: 'test-key' })
    const h2 = getHarness()
    assertTrue(h1 === h2)
  })

  console.log('\n====================================')
  console.log(`Results: ${passCount} passed, ${failCount} failed`)
  process.exitCode = failCount > 0 ? 1 : 0
}

main().catch(err => {
  console.error(err)
  process.exitCode = 1
})
