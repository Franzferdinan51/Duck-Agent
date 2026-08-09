/**
 * Tests for the Grok Build API client.
 * 
 * Run with: npx tsx backends/tests/test-api-client.ts
 */

import { GrokBuildAPIClient, getAPIClient } from '../grok-build/api-client'

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

console.log('Duck Agent Grok Build API Client Tests')
console.log('======================================')

async function main() {
await test('creates new API client', () => {
  const client = new GrokBuildAPIClient()
  assertTrue(client !== null)
})

await test('reports not configured without API key', () => {
  const originalKey = process.env.GROK_API_KEY
  delete process.env.GROK_API_KEY
  
  const client = new GrokBuildAPIClient()
  assertTrue(!client.isConfigured())
  
  if (originalKey) process.env.GROK_API_KEY = originalKey
})

await test('reports configured with API key', () => {
  const originalKey = process.env.GROK_API_KEY
  process.env.GROK_API_KEY = 'test-key-123'
  
  const client = new GrokBuildAPIClient()
  assertTrue(client.isConfigured())
  
  if (originalKey) process.env.GROK_API_KEY = originalKey
  else delete process.env.GROK_API_KEY
})

await test('throws when sending without API key', async () => {
  const originalKey = process.env.GROK_API_KEY
  delete process.env.GROK_API_KEY
  
  const client = new GrokBuildAPIClient()
  let threw = false
  try {
    await client.sendMessage('test')
  } catch (err) {
    threw = true
    if (!String(err).includes('API key')) {
      throw new Error('Wrong error message')
    }
  }
  await assertTrue(threw, 'Should throw when API key missing')
  
  if (originalKey) process.env.GROK_API_KEY = originalKey
})

await test('respects custom config', () => {
  const client = new GrokBuildAPIClient({
    apiEndpoint: 'https://custom.api.com',
    defaultModel: 'grok-custom',
    apiKey: 'custom-key',
  })
  
  const config = client.getConfig()
  if (config.apiEndpoint !== 'https://custom.api.com') {
    throw new Error('Custom endpoint not set')
  }
  if (config.defaultModel !== 'grok-custom') {
    throw new Error('Custom model not set')
  }
  assertTrue(client.isConfigured())
})

await test('singleton instance returns same client', () => {
  const c1 = getAPIClient()
  const c2 = getAPIClient()
  if (c1 !== c2) throw new Error('Should return same instance')
})

await test('config has default model', () => {
  const client = new GrokBuildAPIClient()
  const config = client.getConfig()
  if (!config.defaultModel) throw new Error('Missing default model')
  if (!config.apiEndpoint) throw new Error('Missing API endpoint')
})

await test('API key from env is used', () => {
  const originalKey = process.env.GROK_API_KEY
  process.env.GROK_API_KEY = 'env-test-key'
  
  const client = new GrokBuildAPIClient()
  assertTrue(client.isConfigured())
  
  if (originalKey) process.env.GROK_API_KEY = originalKey
  else delete process.env.GROK_API_KEY
})

console.log('\n======================================')
console.log(`Results: ${passCount} passed, ${failCount} failed`)
process.exitCode = failCount > 0 ? 1 : 0
}

main().catch(error => {
  console.error(error)
  process.exitCode = 1
})
