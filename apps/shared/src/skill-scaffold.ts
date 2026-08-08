/** Adapted from NousResearch/hermes-agent (MIT). */
const INVOCATION_PREFIX = '[IMPORTANT: The user has invoked the '
const SINGLE_MARKER = 'The full skill content is loaded below.]'
const SINGLE_INSTRUCTION = 'The user has provided the following instruction alongside the skill invocation: '
const RUNTIME_NOTE = '\n\n[Runtime note:'
const BUNDLE_MARKER = ' skill bundle,'
const BUNDLE_INSTRUCTION = '\nUser instruction: '
const BUNDLE_SKILL_BLOCK = '\n\n[Loaded as part of the '
const NAME_RE = new RegExp(`^${INVOCATION_PREFIX.replace(/[[\]]/g, '\\$&')}"([^"]*)"`)

function between(text: string, marker: string, end: string, fromEnd = false): string {
  const index = fromEnd ? text.lastIndexOf(marker) : text.indexOf(marker)
  if (index < 0) return ''
  const tail = text.slice(index + marker.length)
  const stop = tail.indexOf(end)
  return (stop >= 0 ? tail.slice(0, stop) : tail).trim()
}

export function skillInvocationText(text: string): null | string {
  if (!text.startsWith(INVOCATION_PREFIX)) return null
  const name = (NAME_RE.exec(text)?.[1] ?? '').trim()
  if (!name) return null
  const label = name.startsWith('/') ? name : `/${name}`
  const instruction = text.includes(BUNDLE_MARKER)
    ? between(text, BUNDLE_INSTRUCTION, BUNDLE_SKILL_BLOCK)
    : text.includes(SINGLE_MARKER)
      ? between(text, SINGLE_INSTRUCTION, RUNTIME_NOTE, true)
      : ''
  return instruction ? `${label} ${instruction.replace(/\s+/g, ' ')}` : label
}
