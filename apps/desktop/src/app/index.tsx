import { useMemo, useState } from 'react'
import {
  IconBrain,
  IconChevronRight,
  IconDatabase,
  IconFileCode,
  IconLayoutDashboard,
  IconMessageCircle,
  IconPlugConnected,
  IconPlus,
  IconRobot,
  IconSearch,
  IconSettings,
  IconSparkles,
  IconTerminal2,
  IconTool,
} from '@tabler/icons-react'
import { BrandMark } from '../components/brand-mark'
import { AgentRunPanel } from '../components/agent-run-panel'
import { RuntimeInspector } from '../components/runtime-inspector'
import { DuckAgentBackendSettings } from './settings/duck-agent-backend-settings'
import type { AgentRun, NavKey, Session } from './types'

const sessions: Session[] = [
  { id: '1', title: 'Finish Duck Agent desktop', time: 'Now', status: 'running' },
  { id: '2', title: 'Audit Grok Build tool loop', time: '24m', status: 'complete' },
  { id: '3', title: 'Review MCP transport plan', time: 'Yesterday', status: 'idle' },
]

const initialRun: AgentRun = {
  goal: 'Finish the Duck Agent desktop application without removing useful Hermes-derived functionality.',
  status: 'running',
  steps: [
    { id: 'plan', label: 'Plan desktop restoration', detail: 'Inventory Hermes-derived surfaces and Duck Agent runtime contracts.', state: 'complete' },
    { id: 'ui', label: 'Restore the control surface', detail: 'Sessions, goals, tools, memory, workspace, runtime status, and settings.', state: 'running' },
    { id: 'runtime', label: 'Connect runtime events', detail: 'Render reason → act → observe progress as structured events.', state: 'pending' },
    { id: 'verify', label: 'Verify and package', detail: 'Typecheck, test, package, and exercise recovery states.', state: 'pending' },
  ],
}

const navItems: Array<{ key: NavKey; label: string; icon: typeof IconMessageCircle }> = [
  { key: 'chat', label: 'Agent', icon: IconMessageCircle },
  { key: 'goals', label: 'Goals', icon: IconLayoutDashboard },
  { key: 'tools', label: 'Tools & MCP', icon: IconPlugConnected },
  { key: 'memory', label: 'Memory', icon: IconBrain },
  { key: 'skills', label: 'Skills', icon: IconSparkles },
  { key: 'workspace', label: 'Workspace', icon: IconFileCode },
]

export function DuckAgentApp() {
  const [active, setActive] = useState<NavKey>('chat')
  const [query, setQuery] = useState('')
  const [composer, setComposer] = useState('')
  const [run, setRun] = useState(initialRun)

  const filteredSessions = useMemo(
    () => sessions.filter((session) => session.title.toLowerCase().includes(query.toLowerCase())),
    [query],
  )

  const submit = () => {
    const goal = composer.trim()
    if (!goal) return
    setRun({
      goal,
      status: 'running',
      steps: [
        { id: 'understand', label: 'Understand the goal', detail: 'Build task context and choose the next action.', state: 'running' },
        { id: 'act', label: 'Use tools and work', detail: 'Tool calls and observations will appear here.', state: 'pending' },
        { id: 'finish', label: 'Complete the goal', detail: 'Verify the result and report what changed.', state: 'pending' },
      ],
    })
    setComposer('')
  }

  return (
    <div className="duck-app">
      <aside className="sidebar">
        <div className="brand-row">
          <BrandMark size={34} />
          <div>
            <strong>Duck Agent</strong>
            <span>Autonomous workspace</span>
          </div>
        </div>

        <button className="new-task" type="button" onClick={() => { setActive('chat'); setComposer('') }}>
          <IconPlus size={17} />
          New goal
          <span>⌘N</span>
        </button>

        <nav className="main-nav" aria-label="Duck Agent navigation">
          {navItems.map(({ key, label, icon: Icon }) => (
            <button key={key} className={active === key ? 'active' : ''} onClick={() => setActive(key)} type="button">
              <Icon size={18} />
              {label}
              {key === 'goals' && <em>1</em>}
            </button>
          ))}
        </nav>

        <div className="sessions-heading">
          <span>Sessions</span>
          <button type="button" aria-label="Create session"><IconPlus size={15} /></button>
        </div>
        <label className="session-search">
          <IconSearch size={15} />
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search sessions" />
        </label>
        <div className="session-list">
          {filteredSessions.map((session) => (
            <button key={session.id} type="button" className="session-row" onClick={() => setActive('chat')}>
              <span className={`status-dot ${session.status}`} />
              <span className="session-copy"><strong>{session.title}</strong><small>{session.time}</small></span>
              <IconChevronRight size={14} />
            </button>
          ))}
        </div>

        <div className="sidebar-footer">
          <button type="button" onClick={() => setActive('workspace')}><IconTerminal2 size={18} /> Terminal</button>
          <button type="button" className={active === 'settings' ? 'active' : ''} onClick={() => setActive('settings')}><IconSettings size={18} /> Settings</button>
        </div>
      </aside>

      <main className="workspace">
        <header className="topbar">
          <div className="crumb"><span>Duck Agent</span><IconChevronRight size={14} /><strong>{active === 'chat' ? 'Agent run' : navItems.find((item) => item.key === active)?.label ?? 'Settings'}</strong></div>
          <div className="runtime-pill"><span className="live-dot" /> Grok Build <b>Primary</b></div>
        </header>

        <section className="work-content">
          {active === 'chat' || active === 'goals' ? (
            <AgentRunPanel run={run} />
          ) : active === 'settings' ? (
            <DuckAgentBackendSettings />
          ) : (
            <FeatureSurface active={active} />
          )}
        </section>

        {(active === 'chat' || active === 'goals') && (
          <div className="composer-wrap">
            <div className="composer">
              <textarea
                value={composer}
                onChange={(event) => setComposer(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && !event.shiftKey) {
                    event.preventDefault()
                    submit()
                  }
                }}
                placeholder="Give Duck Agent a goal…"
                rows={2}
              />
              <div className="composer-actions">
                <div><button type="button"><IconPlus size={17} /> Add context</button><button type="button"><IconTool size={17} /> Tools</button></div>
                <button className="run-button" type="button" onClick={submit}><IconRobot size={17} /> Run goal</button>
              </div>
            </div>
            <p>Duck Agent can act through enabled tools. High-impact actions should require approval.</p>
          </div>
        )}
      </main>

      <RuntimeInspector run={run} />
    </div>
  )
}

function FeatureSurface({ active }: { active: NavKey }) {
  const content: Record<string, { title: string; copy: string; items: Array<[string, string]> }> = {
    tools: { title: 'Tools & MCP', copy: 'Manage the capabilities available to the Grok Build agent loop.', items: [['Filesystem', 'Ready'], ['Git workspace', 'Ready'], ['Web research', 'Ready'], ['External MCP servers', 'Connect']] },
    memory: { title: 'Memory', copy: 'Working context and durable project knowledge belong to the runtime, not a single model turn.', items: [['Working memory', 'Active'], ['Project facts', '12 items'], ['Pinned context', '3 items'], ['Retention policy', 'Review']] },
    skills: { title: 'Skills', copy: 'Reusable instructions and workflows that Duck Agent can select while completing goals.', items: [['Coding', 'Enabled'], ['Web research', 'Enabled'], ['File operations', 'Enabled'], ['Task planning', 'Enabled']] },
    workspace: { title: 'Workspace', copy: 'Files, repository state, artifacts, and terminal sessions stay attached to the active task.', items: [['Repository', 'Duck-Agent'], ['Branch', 'main'], ['Artifacts', '0'], ['Terminal sessions', '1']] },
  }
  const surface = content[active] ?? content.workspace
  return (
    <div className="feature-page">
      <div className="page-kicker">Duck Agent runtime</div>
      <h1>{surface.title}</h1>
      <p>{surface.copy}</p>
      <div className="feature-grid">
        {surface.items.map(([name, value]) => (
          <button type="button" className="feature-card" key={name}>
            <span>{name}</span><strong>{value}</strong><IconChevronRight size={16} />
          </button>
        ))}
      </div>
      <div className="preserve-note"><IconDatabase size={20} /><div><strong>Preserve-first migration</strong><p>Hermes-derived functionality is kept and adapted behind Duck Agent runtime contracts. Missing integrations should be completed rather than removed.</p></div></div>
    </div>
  )
}
