/**
 * Integration tests for Duck Agent backend.
 * 
 * Tests the complete flow from backend configuration to message handling.
 * Run with: npx tsx backends/tests/test-integration.ts
 */

import { GrokBuildHarness } from '../grok-build/harness'
import { loadConfig } from '../grok-build/config'

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

console.log('Duck Agent Integration Tests')
console.log('============================')

await test('config loads with defaults', () => {
  const config = loadConfig()
  if (!config.apiEndpoint) throw new Error('Missing apiEndpoint')
  if (!config.defaultModel) throw new Error('Missing defaultModel')
})

await test('config honors env vars', () => {
  const originalEndpoint = process.env.GROK_API_ENDPOINT
  const originalModel = process.env.GROK_MODEL
  
  process.env.GROK_API_ENDPOINT = 'https://test.api.com'
  process.env.GROK_MODEL = 'grok-test'
  
  const config = loadConfig()
  
  if (config.apiEndpoint !== 'https://test.api.com') {
    throw new Error('Env var not honored')
  }
  if (config.defaultModel !== 'grok-test') {
    throw new Error('Model env var not honored')
  }
  
  // Restore
  if (originalEndpoint) process.env.GROK_API_ENDPOINT = originalEndpoint
  else delete process.env.GROK_API_ENDPOINT
  if (originalModel) process.env.GROK_MODEL = originalModel
  else delete process.env.GROK_MODEL
})

await test('full harness lifecycle (start -> send -> stop)', async () => {
  const harness = new GrokBuildHarness()
  await harness.start()
  
  // Verify status
  if (harness.getStatus() !== 'ready') throw new Error('Not ready after start')
  
  // Send message
  const response = await harness.sendMessage('integration test')
  if (!response.content) throw new Error('No response content')
  if (response.metadata?.backend !== 'grok-build') throw new Error('Wrong backend')
  
  // Stop
  await harness.stop()
  if (harness.getStatus() !== 'stopped') throw new Error('Not stopped')
})

await test('harness handles multiple messages', async () => {
  const harness = new GrokBuildHarness()
  await harness.start()
  
  await harness.sendMessage('msg1')
  await harness.sendMessage('msg2')
  await harness.sendMessage('msg3')
  
  const history = harness.getHistory()
  assertEqual(history.length, 3)
  
  await harness.stop()
})

await test('harness clears history on clear', async () => {
  const harness = new GrokBuildHarness()
  await harness.start()
  
  await harness.sendMessage('msg1')
  harness.clearHistory()
  
  assertEqual(harness.getHistory().length, 0)
  
  await harness.stop()
})

await test('harness refuses messages after stop', async () => {
  const harness = new GrokBuildHarness()
  await harness.start()
  await harness.stop()
  
  let threw = false
  try {
    await harness.sendMessage('after stop')
  } catch (err) {
    threw = true
  }
  await assertTrue(threw, 'Should throw after stop')
})

await test('harness tracks message timestamps', async () => {
  const harness = new GrokBuildHarness()
  await harness.start()
  
  const before = Date.now()
  await harness.sendMessage('test')
  const after = Date.now()
  
  const history = harness.getHistory()
  const ts = history[0].timestamp
  
  if (!ts) throw new Error('Missing timestamp')
  if (ts < before) throw new Error('Timestamp too early')
  if (ts > after) throw new Error('Timestamp too late')
  
  await harness.stop()
})

await test('harness supports different roles', async () => {
  const harness = new GrokBuildHarness()
  await harness.start()
  await harness.sendMessage('user message')
  
  const history = harness.getHistory()
  assertEqual(history[0].role, 'user')
  assertEqual(history[0].content, 'user message')
  
  await harness.stop()
})

console.log('\n============================')
console.log(`Results: ${passCount} passed, ${failCount} failed`)
process.exit(failCount > 0 ? 1 : 0)
