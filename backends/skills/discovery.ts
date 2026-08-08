import { existsSync, readdirSync, readFileSync, statSync } from 'fs'
import { homedir } from 'os'
import { dirname, join } from 'path'

/** Adapted from Franzferdinan51/GrokBot skill discovery. */
export type DuckSkillScope = 'project' | 'user' | 'compatible'
export type DuckSkill = { name: string; description: string; path: string; scope: DuckSkillScope }

function walk(root: string, scope: DuckSkillScope, output: DuckSkill[]): void {
  if (!existsSync(root)) return
  for (const entry of readdirSync(root)) {
    const path = join(root, entry)
    let stat
    try { stat = statSync(path) } catch { continue }
    if (stat.isDirectory()) walk(path, scope, output)
    else if (entry === 'SKILL.md') {
      const text = readFileSync(path, 'utf8')
      const name = text.match(/^name:\s*(.+)$/m)?.[1]?.trim() || dirname(path).split(/[\\/]/).pop() || 'Unnamed skill'
      const description = text.match(/^description:\s*(.+)$/m)?.[1]?.trim().replace(/^['"]|['"]$/g, '') || ''
      output.push({ name, description, path, scope })
    }
  }
}

export function listDuckSkills(workspace?: string): DuckSkill[] {
  const found: DuckSkill[] = []
  if (workspace) {
    walk(join(workspace, '.grok', 'skills'), 'project', found)
    walk(join(workspace, '.agents', 'skills'), 'project', found)
    walk(join(workspace, '.hermes', 'skills'), 'project', found)
  }
  walk(join(homedir(), '.grok', 'skills'), 'user', found)
  walk(join(homedir(), '.agents', 'skills'), 'compatible', found)
  walk(join(homedir(), '.claude', 'skills'), 'compatible', found)
  walk(join(homedir(), '.hermes', 'skills'), 'compatible', found)
  const unique = new Map<string, DuckSkill>()
  for (const skill of found) if (!unique.has(skill.name)) unique.set(skill.name, skill)
  return [...unique.values()].sort((a, b) => a.name.localeCompare(b.name))
}
