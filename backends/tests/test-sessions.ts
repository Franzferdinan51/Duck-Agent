/**
 * Tests for the Duck Agent session manager.
 * 
 * Run with: npx tsx backends/tests/test-sessions.ts
 */

import { SessionManager, getSessionManager } from '../sessions/manager'

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

console.log('Duck Agent Session Manager Tests')
console.log('================================')

await test('creates new session manager', () => {
  const manager = new SessionManager()
  assertTrue(manager !== null)
})

await test('creates a session', () => {
  const manager = new SessionManager()
  const session = manager.createSession('test session')
  if (!session.id) throw new Error('Missing ID')
  assertEqual(session.name, 'test session')
  assertEqual(session.backend, 'grok-build')
})

await test('creates a session with custom backend', () => {
  const manager = new SessionManager()
  const session = manager.createSession('test', 'hermes-compatible')
  assertEqual(session.backend, 'hermes-compatible')
})

await test('gets a session by ID', () => {
  const manager = new SessionManager()
  const session = manager.createSession('test')
  const found = manager.getSession(session.id)
  if (!found) throw new Error('Should find session')
  assertEqual(found.id, session.id)
})

await test('returns null for unknown session', () => {
  const manager = new SessionManager()
  const found = manager.getSession('unknown')
  if (found !== null) throw new Error('Should be null')
})

await test('new session is active by default', () => {
  const manager = new SessionManager()
  const session = manager.createSession('test')
  const active = manager.getActiveSession()
  if (!active) throw new Error('Should have active session')
  assertEqual(active.id, session.id)
})

await test('sets active session', () => {
  const manager = new SessionManager()
  const s1 = manager.createSession('s1')
  const s2 = manager.createSession('s2')
  manager.setActiveSession(s1.id)
  const active = manager.getActiveSession()
  if (!active) throw new Error('Should have active session')
  assertEqual(active.id, s1.id)
})

await test('fails to set unknown active session', () => {
  const manager = new SessionManager()
  const result = manager.setActiveSession('unknown')
  assertTrue(!result)
})

await test('adds a message to session', () => {
  const manager = new SessionManager()
  const session = manager.createSession('test')
  const msg = manager.addMessage(session.id, 'user', 'hello')
  if (!msg) throw new Error('Should add message')
  assertEqual(msg.role, 'user')
  assertEqual(msg.content, 'hello')
})

await test('returns null when adding to unknown session', () => {
  const manager = new SessionManager()
  const msg = manager.addMessage('unknown', 'user', 'hello')
  if (msg !== null) throw new Error('Should be null')
})

await test('gets messages from session', () => {
  const manager = new SessionManager()
  const session = manager.createSession('test')
  manager.addMessage(session.id, 'user', 'msg1')
  manager.addMessage(session.id, 'assistant', 'msg2')
  const messages = manager.getMessages(session.id)
  assertEqual(messages.length, 2)
})

await test('clears session messages', () => {
  const manager = new SessionManager()
  const session = manager.createSession('test')
  manager.addMessage(session.id, 'user', 'msg1')
  const result = manager.clearSession(session.id)
  assertTrue(result)
  assertEqual(manager.getMessages(session.id).length, 0)
})

await test('deletes a session', () => {
  const manager = new SessionManager()
  const session = manager.createSession('test')
  const result = manager.deleteSession(session.id)
  assertTrue(result)
  assertEqual(manager.listSessions().length, 0)
})

await test('list sessions sorted by updated time', async () => {
  const manager = new SessionManager()
  const s1 = manager.createSession('s1')
  // Wait a bit
  await new Promise(resolve => setTimeout(resolve, 10))
  const s2 = manager.createSession('s2')
  const list = manager.listSessions()
  assertEqual(list[0].id, s2.id)  // Most recent first
  assertEqual(list[1].id, s1.id)
})

await test('stores memory', () => {
  const manager = new SessionManager()
  manager.remember('greeting', 'Hello!')
  const memory = manager.recall('greeting')
  if (!memory) throw new Error('Should recall memory')
  assertEqual(memory.value, 'Hello!')
})

await test('returns null for unknown memory', () => {
  const manager = new SessionManager()
  const memory = manager.recall('unknown')
  if (memory !== null) throw new Error('Should be null')
})

await test('forgets memory', () => {
  const manager = new SessionManager()
  manager.remember('key', 'value')
  const result = manager.forget('key')
  assertTrue(result)
  assertEqual(manager.recall('key'), null)
})

await test('searches memories by tag', () => {
  const manager = new SessionManager()
  manager.remember('k1', 'v1', ['work', 'important'])
  manager.remember('k2', 'v2', ['personal'])
  manager.remember('k3', 'v3', ['work'])
  const work = manager.searchMemoriesByTag('work')
  assertEqual(work.length, 2)
})

await test('gets statistics', () => {
  const manager = new SessionManager()
  const session = manager.createSession('test')
  manager.addMessage(session.id, 'user', 'msg')
  manager.remember('key', 'value')
  const stats = manager.getStats()
  assertEqual(stats.sessionCount, 1)
  assertEqual(stats.memoryCount, 1)
  assertEqual(stats.totalMessages, 1)
  assertTrue(stats.activeSessionId !== null)
})

await test('singleton manager', () => {
  const m1 = getSessionManager()
  const m2 = getSessionManager()
  if (m1 !== m2) throw new Error('Should be singleton')
})

console.log('\n================================')
console.log(`Results: ${passCount} passed, ${failCount} failed`)
process.exit(failCount > 0 ? 1 : 0)
