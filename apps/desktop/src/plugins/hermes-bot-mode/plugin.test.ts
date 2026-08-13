import { describe, expect, it, vi } from 'vitest'

const runtime = vi.hoisted(() => ({
  load: vi.fn().mockResolvedValue('hermes-bots'),
  unload: vi.fn()
}))

vi.mock('@/contrib/runtime-loader', () => ({
  loadRuntimePlugin: runtime.load,
  unloadRuntimePlugin: runtime.unload
}))

import plugin from './plugin'

describe('bundled Bot Mode', () => {
  it('loads the vendored upstream source and unloads it with Duck Agent', () => {
    let dispose: (() => void) | undefined
    plugin.register({ onDispose: callback => (dispose = callback) } as never)

    expect(runtime.load).toHaveBeenCalledWith(
      expect.stringContaining("from '@hermes/plugin-sdk'"),
      'Duck Agent Bot Mode',
      { kind: 'bundled' }
    )

    dispose?.()
    expect(runtime.unload).toHaveBeenCalledWith('hermes-bots')
  })
})
