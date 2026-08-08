import { useMemo, useState } from 'react'
import { IconBolt, IconCheck, IconCpu, IconRobot, IconShieldCheck } from '@tabler/icons-react'

const BACKENDS = [
  {
    id: 'grok-build',
    name: 'Grok Build',
    description: 'Primary Duck Agent harness for planning, reasoning, tool selection, and iterative execution.',
    icon: IconBolt,
    recommended: true,
  },
  {
    id: 'hermes-compatible',
    name: 'Hermes-Compatible',
    description: 'Compatibility path for Hermes-style workflows while Duck Agent keeps Grok Build primary.',
    icon: IconRobot,
  },
  {
    id: 'prime-agent',
    name: 'Prime Agent',
    description: 'Experimental Prime Intellect / RLM integration path.',
    icon: IconCpu,
  },
] as const

type BackendId = (typeof BACKENDS)[number]['id']
type ApprovalMode = 'strict' | 'balanced' | 'autonomous'

export function DuckAgentBackendSettings() {
  const [selected, setSelected] = useState<BackendId>(() =>
    (localStorage.getItem('duck_agent_backend') as BackendId) || 'grok-build',
  )
  const [model, setModel] = useState(() => localStorage.getItem('duck_agent_model') || 'grok-3')
  const [maxSteps, setMaxSteps] = useState(() => Number(localStorage.getItem('duck_agent_max_steps') || 24))
  const [approval, setApproval] = useState<ApprovalMode>(() =>
    (localStorage.getItem('duck_agent_approval') as ApprovalMode) || 'balanced',
  )
  const [streamEvents, setStreamEvents] = useState(() => localStorage.getItem('duck_agent_stream_events') !== 'false')
  const [resumeRuns, setResumeRuns] = useState(() => localStorage.getItem('duck_agent_resume_runs') !== 'false')
  const [saved, setSaved] = useState(false)

  const active = useMemo(() => BACKENDS.find((backend) => backend.id === selected) ?? BACKENDS[0], [selected])

  function save() {
    localStorage.setItem('duck_agent_backend', selected)
    localStorage.setItem('duck_agent_model', model)
    localStorage.setItem('duck_agent_max_steps', String(maxSteps))
    localStorage.setItem('duck_agent_approval', approval)
    localStorage.setItem('duck_agent_stream_events', String(streamEvents))
    localStorage.setItem('duck_agent_resume_runs', String(resumeRuns))
    window.dispatchEvent(new CustomEvent('duck-agent:runtime-settings', {
      detail: { backend: selected, model, maxSteps, approval, streamEvents, resumeRuns },
    }))
    setSaved(true)
    window.setTimeout(() => setSaved(false), 1800)
  }

  return (
    <div className="settings-page">
      <div className="page-kicker">Duck Agent runtime</div>
      <h1>Runtime & autonomy</h1>
      <p className="settings-lead">
        Configure the primary harness and the boundaries Duck Agent follows while completing long-running goals.
      </p>

      <section className="settings-section">
        <div className="settings-section-head">
          <div><strong>Agent harness</strong><span>Grok Build stays the primary product path.</span></div>
          <span className="settings-badge"><IconBolt size={13} /> {active.name}</span>
        </div>
        <div className="backend-grid">
          {BACKENDS.map((backend) => {
            const Icon = backend.icon
            const isSelected = selected === backend.id
            return (
              <button type="button" key={backend.id} className={`backend-card ${isSelected ? 'selected' : ''}`} onClick={() => setSelected(backend.id)}>
                <span className="backend-icon"><Icon size={19} /></span>
                <span className="backend-copy"><strong>{backend.name}</strong><small>{backend.description}</small></span>
                {backend.recommended && <em>PRIMARY</em>}
                {isSelected && <IconCheck className="backend-check" size={17} />}
              </button>
            )
          })}
        </div>
      </section>

      <section className="settings-section settings-form">
        <label>
          <span>Default model</span>
          <input value={model} onChange={(event) => setModel(event.target.value)} placeholder="grok-3" />
          <small>Model used by the Grok Build harness unless a task selects another compatible model.</small>
        </label>
        <label>
          <span>Maximum agent steps <b>{maxSteps}</b></span>
          <input type="range" min={4} max={64} value={maxSteps} onChange={(event) => setMaxSteps(Number(event.target.value))} />
          <small>Hard stop for one autonomous run. Recovery or a resumed goal can start another bounded run.</small>
        </label>
        <label>
          <span>Approval policy</span>
          <select value={approval} onChange={(event) => setApproval(event.target.value as ApprovalMode)}>
            <option value="strict">Strict — approve most tool actions</option>
            <option value="balanced">Balanced — approve high-impact actions</option>
            <option value="autonomous">Autonomous — minimal prompts</option>
          </select>
        </label>
        <Toggle checked={streamEvents} onChange={setStreamEvents} title="Stream structured run events" copy="Show planning, tool calls, observations, retries, approvals, and completion as the run happens." />
        <Toggle checked={resumeRuns} onChange={setResumeRuns} title="Resume interrupted goals" copy="Allow the runtime to reopen durable task state when a supported session is resumed." />
      </section>

      <div className="approval-note">
        <IconShieldCheck size={20} />
        <div><strong>Approval boundary</strong><p>Destructive and high-impact operations should pause with the action, arguments, reason, and expected effect visible before execution.</p></div>
      </div>

      <div className="settings-actions">
        <span>{saved ? 'Saved locally and runtime event dispatched.' : 'Changes apply to new runs unless the runtime supports live reconfiguration.'}</span>
        <button type="button" onClick={save}>{saved ? <IconCheck size={16} /> : <IconBolt size={16} />}{saved ? 'Saved' : 'Save runtime settings'}</button>
      </div>
    </div>
  )
}

function Toggle({ checked, onChange, title, copy }: { checked: boolean; onChange: (value: boolean) => void; title: string; copy: string }) {
  return (
    <label className="settings-toggle">
      <span><strong>{title}</strong><small>{copy}</small></span>
      <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} />
    </label>
  )
}
