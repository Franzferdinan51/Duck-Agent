/**
 * Tests for the Duck Agent skill manager.
 * 
 * Run with: npx tsx backends/tests/test-skills.ts
 */

import { SkillManager, getSkillManager } from '../skills/manager'

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

console.log('Duck Agent Skill Manager Tests')
console.log('==============================')

async function main() {
await test('creates new skill manager', () => {
  const manager = new SkillManager()
  assertTrue(manager !== null)
  assertEqual(manager.count(), 0)
})

await test('registers a skill', () => {
  const manager = new SkillManager()
  manager.registerSkill({
    id: 'test',
    name: 'Test',
    description: 'Test skill',
    category: 'test',
    version: '1.0.0',
    execute: async () => ({ success: true }),
  })
  assertEqual(manager.count(), 1)
})

await test('unregisters a skill', () => {
  const manager = new SkillManager()
  manager.registerSkill({
    id: 'test',
    name: 'Test',
    description: 'Test skill',
    category: 'test',
    version: '1.0.0',
    execute: async () => ({ success: true }),
  })
  manager.unregisterSkill('test')
  assertEqual(manager.count(), 0)
})

await test('gets a skill by ID', () => {
  const manager = new SkillManager()
  manager.registerSkill({
    id: 'test',
    name: 'Test',
    description: 'Test skill',
    category: 'test',
    version: '1.0.0',
    execute: async () => ({ success: true }),
  })
  const skill = manager.getSkill('test')
  if (!skill) throw new Error('Should find skill')
  assertEqual(skill.id, 'test')
})

await test('returns null for unknown skill', () => {
  const manager = new SkillManager()
  const skill = manager.getSkill('unknown')
  if (skill !== null) throw new Error('Should be null')
})

await test('finds skills by category', () => {
  const manager = new SkillManager()
  manager.registerSkill({
    id: 'cat1',
    name: 'Cat 1',
    description: 'd',
    category: 'cat-a',
    version: '1.0.0',
    execute: async () => ({ success: true }),
  })
  manager.registerSkill({
    id: 'cat2',
    name: 'Cat 2',
    description: 'd',
    category: 'cat-b',
    version: '1.0.0',
    execute: async () => ({ success: true }),
  })
  const results = manager.findByCategory('cat-a')
  assertEqual(results.length, 1)
  assertEqual(results[0].id, 'cat1')
})

await test('finds skills by tag', () => {
  const manager = new SkillManager()
  manager.registerSkill({
    id: 's1',
    name: 'S1',
    description: 'd',
    category: 'c',
    version: '1.0.0',
    tags: ['web', 'search'],
    execute: async () => ({ success: true }),
  })
  const results = manager.findByTag('web')
  assertEqual(results.length, 1)
  assertEqual(results[0].id, 's1')
})

await test('searches skills by keyword', () => {
  const manager = new SkillManager()
  manager.registerSkill({
    id: 'web-search',
    name: 'Web Search',
    description: 'Search the web',
    category: 'research',
    version: '1.0.0',
    execute: async () => ({ success: true }),
  })
  const results = manager.search('web')
  assertTrue(results.length >= 1)
})

await test('executes a skill', async () => {
  const manager = new SkillManager()
  manager.registerSkill({
    id: 'echo',
    name: 'Echo',
    description: 'Echo input',
    category: 'test',
    version: '1.0.0',
    execute: async (input) => ({ success: true, result: input }),
  })
  const result = await manager.executeSkill('echo', { foo: 'bar' })
  assertTrue(result.success)
  assertEqual(result.result?.foo, 'bar')
})

await test('returns error for unknown skill', async () => {
  const manager = new SkillManager()
  const result = await manager.executeSkill('unknown', {})
  assertTrue(!result.success)
  if (!result.error?.includes('not found')) throw new Error('Wrong error')
})

await test('tracks usage stats', async () => {
  const manager = new SkillManager()
  manager.registerSkill({
    id: 'counter',
    name: 'Counter',
    description: 'Counts usage',
    category: 'test',
    version: '1.0.0',
    execute: async () => ({ success: true }),
  })
  await manager.executeSkill('counter', {})
  await manager.executeSkill('counter', {})
  await manager.executeSkill('counter', {})
  const stats = manager.getUsageStats()
  assertEqual(stats.counter, 3)
})

await test('gets most used skills', async () => {
  const manager = new SkillManager()
  manager.registerSkill({
    id: 'a',
    name: 'A',
    description: 'd',
    category: 'c',
    version: '1.0.0',
    execute: async () => ({ success: true }),
  })
  manager.registerSkill({
    id: 'b',
    name: 'B',
    description: 'd',
    category: 'c',
    version: '1.0.0',
    execute: async () => ({ success: true }),
  })
  // Use b more than a
  await manager.executeSkill('a', {})
  await manager.executeSkill('b', {})
  await manager.executeSkill('b', {})
  await manager.executeSkill('b', {})
  const most = manager.getMostUsedSkills(1)
  assertEqual(most[0].id, 'b')
})

await test('singleton has built-in skills', () => {
  const m1 = getSkillManager()
  const m2 = getSkillManager()
  if (m1 !== m2) throw new Error('Should be singleton')
  if (m1.count() === 0) throw new Error('Should have built-in skills')
})

await test('built-in skills include coding', () => {
  const m = getSkillManager()
  const skill = m.getSkill('coding')
  if (!skill) throw new Error('Should have coding skill')
  assertEqual(skill.category, 'development')
})

console.log('\n==============================')
console.log(`Results: ${passCount} passed, ${failCount} failed`)
process.exitCode = failCount > 0 ? 1 : 0
}

main().catch(error => {
  console.error(error)
  process.exitCode = 1
})
