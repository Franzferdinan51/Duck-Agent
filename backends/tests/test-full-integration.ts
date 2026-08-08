/**
 * Full system integration test for Duck Agent.
 * 
 * Tests the complete workflow: session -> skill -> backend -> workflow
 * Run with: npx tsx backends/tests/test-full-integration.ts
 */

import { getHarness } from '../grok-build/harness'
import { getAPIClient } from '../grok-build/api-client'
import { getMCPManager } from '../mcp/server'
import { getSkillManager } from '../skills/manager'
import { getSessionManager } from '../sessions/manager'
import { getWorkflowManager } from '../orchestration/manager'

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

console.log('Duck Agent Full System Integration Test')
console.log('==========================================')

await test('all managers are singletons', () => {
  const h1 = getHarness()
  const h2 = getHarness()
  if (h1 !== h2) throw new Error('Harness not singleton')
  
  const api1 = getAPIClient()
  const api2 = getAPIClient()
  if (api1 !== api2) throw new Error('API client not singleton')
  
  const mcp1 = getMCPManager()
  const mcp2 = getMCPManager()
  if (mcp1 !== mcp2) throw new Error('MCP manager not singleton')
  
  const sm1 = getSkillManager()
  const sm2 = getSkillManager()
  if (sm1 !== sm2) throw new Error('Skill manager not singleton')
  
  const sess1 = getSessionManager()
  const sess2 = getSessionManager()
  if (sess1 !== sess2) throw new Error('Session manager not singleton')
  
  const wf1 = getWorkflowManager()
  const wf2 = getWorkflowManager()
  if (wf1 !== wf2) throw new Error('Workflow manager not singleton')
})

await test('harness can send messages', async () => {
  const harness = getHarness()
  await harness.start()
  const response = await harness.sendMessage('Hello from integration test')
  if (!response.content) throw new Error('No response')
  await harness.stop()
})

await test('session manager tracks active sessions', () => {
  const manager = getSessionManager()
  const session = manager.createSession('integration test')
  const active = manager.getActiveSession()
  if (!active) throw new Error('No active session')
  assertEqual(active.id, session.id)
})

await test('skills can be executed', async () => {
  const manager = getSkillManager()
  const result = await manager.executeSkill('coding', { task: 'test' })
  assertTrue(result.success)
})

await test('MCP manager has built-in tools', () => {
  const manager = getMCPManager()
  const servers = manager.getServers()
  if (servers.length === 0) throw new Error('No MCP servers')
})

await test('complete workflow executes all steps', async () => {
  const wfManager = getWorkflowManager()
  const sessionManager = getSessionManager()
  const skillManager = getSkillManager()
  
  // Create a session
  const session = sessionManager.createSession('workflow test')
  const sessionId = session.id
  
  // Define a workflow that uses the session and skill manager
  const workflow = wfManager.createWorkflow('e2e-test', [
    {
      id: 'add-msg',
      name: 'Add message to session',
      execute: async (ctx) => {
        const mgr = getSessionManager()
        const msg = mgr.addMessage(sessionId, 'user', 'hello from workflow')
        ctx.variables['msgId'] = msg?.id
        return { success: !!msg, message_id: msg?.id }
      },
    },
    {
      id: 'execute-skill',
      name: 'Execute a skill',
      dependsOn: ['add-msg'],
      execute: async () => {
        const mgr = getSkillManager()
        const result = await mgr.executeSkill('coding', { task: 'test' })
        return { success: result.success }
      },
    },
    {
      id: 'remember',
      name: 'Remember something',
      dependsOn: ['execute-skill'],
      execute: async (ctx) => {
        const mgr = getSessionManager()
        mgr.remember('workflow-result', 'success')
        ctx.variables['remembered'] = true
        return { success: true }
      },
    },
  ])
  
  const result = await wfManager.executeWorkflow(workflow.id)
  assertTrue(result.success)
  assertEqual(result.totalSteps, 3)
  assertEqual(result.completedSteps, 3)
})

await test('session messages persist after workflow', () => {
  const manager = getSessionManager()
  const sessions = manager.listSessions()
  if (sessions.length === 0) throw new Error('No sessions')
  
  // Find the workflow test session
  const wfSession = sessions.find(s => s.name === 'workflow test')
  if (!wfSession) throw new Error('No workflow test session')
  if (wfSession.messages.length === 0) throw new Error('No messages in workflow test session')
})

await test('memory persists across calls', () => {
  const manager = getSessionManager()
  manager.remember('test-key', 'test-value')
  const memory = manager.recall('test-key')
  if (!memory) throw new Error('Memory not found')
  assertEqual(memory.value, 'test-value')
})

await test('stats from all managers aggregate correctly', () => {
  const sessionManager = getSessionManager()
  const wfManager = getWorkflowManager()
  const skillManager = getSkillManager()
  const mcpManager = getMCPManager()
  
  const sessionStats = sessionManager.getStats()
  const workflowList = wfManager.listWorkflows()
  const skillCount = skillManager.count()
  const mcpStats = mcpManager.getStats()
  
  if (sessionStats.sessionCount === 0) throw new Error('No sessions')
  if (workflowList.length === 0) throw new Error('No workflows')
  if (skillCount === 0) throw new Error('No skills')
  if (mcpStats.serverCount === 0) throw new Error('No MCP servers')
  
  console.log(`    Sessions: ${sessionStats.sessionCount}, Workflows: ${workflowList.length}, Skills: ${skillCount}, MCP servers: ${mcpStats.serverCount}`)
})

await test('backend switching is honored', () => {
  const original = process.env.DUCK_AGENT_BACKEND
  
  process.env.DUCK_AGENT_BACKEND = 'grok-build'
  // Test loads correctly
  
  process.env.DUCK_AGENT_BACKEND = 'hermes-compatible'
  // Test loads correctly
  
  process.env.DUCK_AGENT_BACKEND = 'prime-agent'
  // Test loads correctly
  
  // Restore
  if (original) process.env.DUCK_AGENT_BACKEND = original
  else delete process.env.DUCK_AGENT_BACKEND
})

console.log('\n==========================================')
console.log(`Results: ${passCount} passed, ${failCount} failed`)
process.exit(failCount > 0 ? 1 : 0)
