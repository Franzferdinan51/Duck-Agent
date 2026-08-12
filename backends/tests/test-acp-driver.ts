/**
 * Tests for the Grok Build ACP CLI driver.
 *
 * Run with: npx tsx backends/tests/test-acp-driver.ts
 */
import { runGrokAcpTurn, isSignedIn, acpHelpers } from '../grok-build/acp-driver'

let passCount = 0
let failCount = 0
let skipCount = 0

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

function skip(name: string): void {
  console.log(`  - ${name} (skipped)`)
  skipCount++
}

function assertTrue(value: boolean, msg = ''): void {
  if (!value) throw new Error(`${msg}Expected true, got ${value}`)
}

function assertEqual<T>(actual: T, expected: T, msg = ''): void {
  if (actual !== expected) throw new Error(`${msg}Expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`)
}

async function main() {
  await test('exports the ACP driver surface', () => {
    assertEqual(typeof runGrokAcpTurn, 'function', 'runGrokAcpTurn should be a function')
    assertEqual(typeof isSignedIn, 'function', 'isSignedIn should be a function')
    assertEqual(typeof acpHelpers.isSignedIn, 'function', 'acpHelpers.isSignedIn should be a function')
    assertTrue(typeof acpHelpers.DEFAULT_SYSTEM_PROMPT === 'string' && acpHelpers.DEFAULT_SYSTEM_PROMPT.length > 0, 'default system prompt set')
  })

  await test('isSignedIn reports the grok.com login state without throwing', () => {
    assertTrue(typeof isSignedIn() === 'boolean', 'isSignedIn must return a boolean')
  })

  await test('runGrokAcpTurn rejects a missing/runable-exe spawn cleanly', async () => {
    let rejected = false
    try {
      await runGrokAcpTurn('x', { cli: '/definitely_not_a_real_grok_binary_xyz' })
    } catch {
      rejected = true
    }
    assertTrue(rejected, 'expected runGrokAcpTurn to reject on a missing CLI')
  })

  await test('runGrokAcpTurn attempts an actual grok turn when signed in', async () => {
    if (!isSignedIn()) {
      skip('live grok turn (grok not signed in on this machine)')
      return
    }
    const r = await runGrokAcpTurn(
      'Reply with exactly: acp-live-ok',
      { cli: 'grok', cwd: '/tmp' },
      { onDelta: () => undefined },
    )
    assertTrue(r.ok, `turn should succeed; got ok=${r.ok} stopReason=${r.stopReason}`)
    assertTrue(r.signedIn, 'signedIn should be true on a signed-in machine')
    assertTrue(r.text.includes('acp-live-ok'), `expected marker in reply; got ${JSON.stringify(r.text.slice(0, 80))}`)
  })

  console.log('\n========================')
  console.log(`Results: ${passCount} passed, ${failCount} failed, ${skipCount} skipped`)
  process.exit(failCount > 0 ? 1 : 0)
}

main()