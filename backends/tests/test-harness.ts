/**
 * Tests for the GrokBuildHarness class.
 * 
 * Run with: npx tsx backends/tests/test-harness.ts
 */

import { GrokBuildHarness, getHarness } from '../grok-build/harness'

let passCount = 0
let failCount = 0

function test(name: string, fn: () => void | Promise<void>) {
  return Promise.resolve()
    .then(fn)
    .then(() => {
      console.log(`  ✓ ${name}`)
      passCount++
    })
    .catch(err => {
      console.error(`  ✗ ${name}`)
      console.error(`    ${err instanceof Error ? err.message : err}`)
      failCount++
    })
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

console.log('Duck Agent Grok Build Harness Tests')
console.log('====================================')

await test('creates new harness instance', () => {
  const harness = new GrokBuildHarness()
  assertEqual(harness.getStatus(), 'idle')
})

await test('starts harness', async () => {
  const harness = new GrokBuildHarness()
  await harness.start()
  assertEqual(harness.getStatus(), 'ready')
})

await test('stops harness', async () => {
  const harness = new GrokBuildHarness()
  await harness.start()
  await harness.stop()
  assertEqual(harness.getStatus(), 'stopped')
})

await test('sends message', async () => {
  const harness = new GrokBuildHarness()
  await harness.start()
  const response = await harness.sendMessage('Hello')
  if (!response.content) throw new Error('Response missing content')
  if (response.status !== 'complete') throw new Error('Response not complete')
  if (response.metadata?.backend !== 'grok-build') throw new Error('Missing backend metadata')
})

await test('throws when sending message before start', async () => {
  const harness = new GrokBuildHarness()
  let threw = false
  try {
    await harness.sendMessage('hello')
  } catch (err) {
    threw = true
  }
  await assertTrue(threw, 'Should throw when not started')
})

await test('tracks message history', async () => {
  const harness = new GrokBuildHarness()
  await harness.start()
  await harness.sendMessage('msg1')
  await harness.sendMessage('msg2')
  const history = harness.getHistory()
  assertEqual(history.length, 2)
  assertEqual(history[0].role, 'user')
  assertEqual(history[1].role, 'user')
})

await test('clears history', async () => {
  const harness = new GrokBuildHarness()
  await harness.start()
  await harness.sendMessage('msg1')
  harness.clearHistory()
  assertEqual(harness.getHistory().length, 0)
})

await test('singleton harness instance', async () => {
  const h1 = getHarness()
  const h2 = getHarness()
  if (h1 !== h2) throw new Error('Should return same instance')
})

await test('accepts custom config', async () => {
  const harness = new GrokBuildHarness({
    defaultModel: 'grok-test',
    timeoutMs: 30000
  })
  await harness.start()
  const response = await harness.sendMessage('test')
  if (response.metadata?.model !== 'grok-test') {
    throw new Error('Custom model not used')
  }
})

console.log('\n====================================')
console.log(`Results: ${passCount} passed, ${failCount} failed`)
process.exit(failCount > 0 ? 1 : 0)
