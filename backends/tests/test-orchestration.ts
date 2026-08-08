/**
 * Tests for the Duck Agent workflow manager.
 * 
 * Run with: npx tsx backends/tests/test-orchestration.ts
 */

import { WorkflowManager, getWorkflowManager } from '../orchestration/manager'

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

console.log('Duck Agent Workflow Manager Tests')
console.log('=================================')

async function main() {
await test('creates new workflow manager', () => {
  const manager = new WorkflowManager()
  assertTrue(manager !== null)
})

await test('creates a workflow', () => {
  const manager = new WorkflowManager()
  const workflow = manager.createWorkflow('test', [])
  if (!workflow.id) throw new Error('Missing ID')
  assertEqual(workflow.name, 'test')
  assertEqual(workflow.status, 'pending')
})

await test('executes a single-step workflow', async () => {
  const manager = new WorkflowManager()
  const workflow = manager.createWorkflow('test', [
    {
      id: 'step1',
      name: 'Step 1',
      execute: async () => ({ success: true, output: 'hello' }),
    },
  ])
  const result = await manager.executeWorkflow(workflow.id)
  assertTrue(result.success)
  assertEqual(result.totalSteps, 1)
  assertEqual(result.completedSteps, 1)
  assertEqual(result.failedSteps, 0)
})

await test('executes a multi-step workflow', async () => {
  const manager = new WorkflowManager()
  const workflow = manager.createWorkflow('test', [
    {
      id: 'step1',
      name: 'Step 1',
      execute: async () => ({ success: true }),
    },
    {
      id: 'step2',
      name: 'Step 2',
      execute: async () => ({ success: true }),
    },
    {
      id: 'step3',
      name: 'Step 3',
      execute: async () => ({ success: true }),
    },
  ])
  const result = await manager.executeWorkflow(workflow.id)
  assertTrue(result.success)
  assertEqual(result.totalSteps, 3)
  assertEqual(result.completedSteps, 3)
})

await test('executes steps in dependency order', async () => {
  const manager = new WorkflowManager()
  const executionOrder: string[] = []
  
  const workflow = manager.createWorkflow('test', [
    {
      id: 'a',
      name: 'A',
      execute: async () => {
        executionOrder.push('a')
        return { success: true }
      },
    },
    {
      id: 'b',
      name: 'B',
      dependsOn: ['a'],
      execute: async () => {
        executionOrder.push('b')
        return { success: true }
      },
    },
  ])
  await manager.executeWorkflow(workflow.id)
  assertEqual(executionOrder[0], 'a')
  assertEqual(executionOrder[1], 'b')
})

await test('handles failing step', async () => {
  const manager = new WorkflowManager()
  const workflow = manager.createWorkflow('test', [
    {
      id: 'fail',
      name: 'Fail',
      execute: async () => ({ success: false, error: 'intentional failure' }),
    },
  ])
  const result = await manager.executeWorkflow(workflow.id)
  assertTrue(!result.success)
  assertEqual(result.failedSteps, 1)
})

await test('retries failing step', async () => {
  const manager = new WorkflowManager()
  let attempts = 0
  const workflow = manager.createWorkflow('test', [
    {
      id: 'flaky',
      name: 'Flaky',
      retries: 2,
      execute: async () => {
        attempts++
        if (attempts < 2) {
          throw new Error('temporary failure')
        }
        return { success: true }
      },
    },
  ])
  const result = await manager.executeWorkflow(workflow.id)
  assertTrue(result.success)
  assertEqual(attempts, 2)
})

await test('throws when executing unknown workflow', async () => {
  const manager = new WorkflowManager()
  let threw = false
  try {
    await manager.executeWorkflow('unknown')
  } catch (err) {
    threw = true
  }
  await assertTrue(threw, 'Should throw')
})

await test('gets a workflow', () => {
  const manager = new WorkflowManager()
  const workflow = manager.createWorkflow('test', [])
  const found = manager.getWorkflow(workflow.id)
  if (!found) throw new Error('Should find workflow')
  assertEqual(found.id, workflow.id)
})

await test('lists all workflows', () => {
  const manager = new WorkflowManager()
  manager.createWorkflow('w1', [])
  manager.createWorkflow('w2', [])
  assertEqual(manager.listWorkflows().length, 2)
})

await test('deletes a workflow', () => {
  const manager = new WorkflowManager()
  const workflow = manager.createWorkflow('test', [])
  const result = manager.deleteWorkflow(workflow.id)
  assertTrue(result)
  assertEqual(manager.listWorkflows().length, 0)
})

await test('context shared between steps', async () => {
  const manager = new WorkflowManager()
  const workflow = manager.createWorkflow('test', [
    {
      id: 'set',
      name: 'Set',
      execute: async (ctx) => {
        ctx.variables['key'] = 'value'
        return { success: true }
      },
    },
    {
      id: 'get',
      name: 'Get',
      dependsOn: ['set'],
      execute: async (ctx) => {
        if (ctx.variables['key'] !== 'value') {
          throw new Error('Context not shared')
        }
        return { success: true }
      },
    },
  ])
  const result = await manager.executeWorkflow(workflow.id)
  assertTrue(result.success)
})

await test('step timeout works', async () => {
  const manager = new WorkflowManager()
  const workflow = manager.createWorkflow('test', [
    {
      id: 'slow',
      name: 'Slow',
      timeoutMs: 100,
      execute: async () => {
        await new Promise(resolve => setTimeout(resolve, 500))
        return { success: true }
      },
    },
  ])
  const result = await manager.executeWorkflow(workflow.id)
  assertTrue(!result.success)
  assertEqual(result.failedSteps, 1)
})

await test('singleton manager', () => {
  const m1 = getWorkflowManager()
  const m2 = getWorkflowManager()
  if (m1 !== m2) throw new Error('Should be singleton')
})

console.log('\n=================================')
console.log(`Results: ${passCount} passed, ${failCount} failed`)
process.exitCode = failCount > 0 ? 1 : 0
}

main().catch(error => {
  console.error(error)
  process.exitCode = 1
})
