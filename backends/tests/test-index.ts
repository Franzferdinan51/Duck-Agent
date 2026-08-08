/**
 * Tests for the Duck Agent backend loader.
 * 
 * Run with: npx tsx backends/tests/test-index.ts
 */

import {
  getConfiguredBackend,
  isValidBackend,
  getAvailableBackends,
  getBackend,
  initializeBackend,
} from '../index'

let passCount = 0
let failCount = 0

function test(name: string, fn: () => void) {
  try {
    fn()
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

function assertTrue(value: boolean, msg = ''): void {
  if (!value) {
    throw new Error(`${msg}Expected true, got ${value}`)
  }
}

console.log('Duck Agent Backend Tests')
console.log('========================')

console.log('\nBackends:')
test('returns default backend (grok-build)', () => {
  // Clear env
  delete process.env.DUCK_AGENT_BACKEND
  assertEqual(getConfiguredBackend(), 'grok-build')
})

test('returns grok-build when set', () => {
  process.env.DUCK_AGENT_BACKEND = 'grok-build'
  assertEqual(getConfiguredBackend(), 'grok-build')
  delete process.env.DUCK_AGENT_BACKEND
})

test('returns hermes-compatible when set', () => {
  process.env.DUCK_AGENT_BACKEND = 'hermes-compatible'
  assertEqual(getConfiguredBackend(), 'hermes-compatible')
  delete process.env.DUCK_AGENT_BACKEND
})

test('returns prime-agent when set', () => {
  process.env.DUCK_AGENT_BACKEND = 'prime-agent'
  assertEqual(getConfiguredBackend(), 'prime-agent')
  delete process.env.DUCK_AGENT_BACKEND
})

test('falls back to grok-build for invalid backend', () => {
  process.env.DUCK_AGENT_BACKEND = 'invalid'
  assertEqual(getConfiguredBackend(), 'grok-build')
  delete process.env.DUCK_AGENT_BACKEND
})

console.log('\nValidation:')
test('validates grok-build', () => {
  assertTrue(isValidBackend('grok-build'))
})

test('validates hermes-compatible', () => {
  assertTrue(isValidBackend('hermes-compatible'))
})

test('validates prime-agent', () => {
  assertTrue(isValidBackend('prime-agent'))
})

test('rejects invalid backend', () => {
  assertTrue(!isValidBackend('invalid'))
})

test('rejects empty backend', () => {
  assertTrue(!isValidBackend(''))
})

console.log('\nBackend Info:')
test('returns all available backends', async () => {
  const backends = await getAvailableBackends()
  assertEqual(backends.length, 3)
})

test('backend has required fields', async () => {
  const backends = await getAvailableBackends()
  for (const backend of backends) {
    if (!backend.type) throw new Error('Missing type')
    if (!backend.name) throw new Error('Missing name')
    if (!backend.description) throw new Error('Missing description')
  }
})

console.log('\nBackend Loader:')
test('gets backend instance', async () => {
  const backend = await getBackend()
  assertTrue(backend !== null)
  assertTrue(typeof backend.start === 'function')
  assertTrue(typeof backend.stop === 'function')
  assertTrue(typeof backend.sendMessage === 'function')
  assertTrue(typeof backend.getStatus === 'function')
})

test('initializes backend', async () => {
  await initializeBackend()
  // If we get here, initialization succeeded
  assertTrue(true)
})

console.log('\n========================')
console.log(`Results: ${passCount} passed, ${failCount} failed`)
process.exit(failCount > 0 ? 1 : 0)
