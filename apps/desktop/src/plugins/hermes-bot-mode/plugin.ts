/**
 * Duck Agent's bundled Hermes Bot Mode integration.
 *
 * The upstream source stays vendored under `vendor/hermes-bot-mode`, pinned
 * as a Git submodule. Loading its source through the same runtime-plugin
 * pipeline keeps it isolated behind the public SDK and makes the feature
 * available in packaged Duck Agent builds without reading `~/.hermes`.
 */

import type { HermesPlugin } from '@duck-agent/plugin-sdk'

import { loadRuntimePlugin, unloadRuntimePlugin } from '@/contrib/runtime-loader'
import upstreamSource from '../../../vendor/hermes-bot-mode/plugin.js?raw'

const UPSTREAM_PLUGIN_ID = 'hermes-bots'

const plugin: HermesPlugin = {
  id: 'duck-agent-bot-mode',
  name: 'Bot Mode',
  register(ctx) {
    void loadRuntimePlugin(upstreamSource, 'Duck Agent Bot Mode', { kind: 'bundled' })
    ctx.onDispose(() => unloadRuntimePlugin(UPSTREAM_PLUGIN_ID))
  }
}

export default plugin
