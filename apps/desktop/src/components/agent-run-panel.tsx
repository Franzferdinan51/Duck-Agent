import { IconAlertTriangle, IconCheck, IconClock, IconLoader2, IconPlayerStop, IconTool } from '@tabler/icons-react'
import type { AgentRun, AgentStep } from '../app/types'

export function AgentRunPanel({ run }: { run: AgentRun }) {
  const done = run.steps.filter((step) => step.state === 'complete').length
  const percent = Math.max(6, Math.round((done / run.steps.length) * 100))

  return (
    <div className="run-page">
      <div className="run-heading">
        <div><span className="eyebrow">Active goal</span><h1>{run.goal}</h1></div>
        <button type="button" className="stop-button"><IconPlayerStop size={16} /> Stop</button>
      </div>

      <div className="progress-card">
        <div className="progress-meta"><div><span className="live-dot" /><strong>Agent working</strong></div><span>{done}/{run.steps.length} steps</span></div>
        <div className="progress-track"><span style={{ width: `${percent}%` }} /></div>
      </div>

      <div className="timeline">
        {run.steps.map((step, index) => <Step key={step.id} step={step} index={index + 1} last={index === run.steps.length - 1} />)}
      </div>

      <div className="activity-card">
        <div className="activity-title"><IconTool size={18} /><div><strong>Tool activity</strong><span>Structured actions and observations from the runtime</span></div><small>LIVE</small></div>
        <div className="tool-event"><span>read_repository</span><code>apps/desktop/src</code><em>completed</em></div>
        <div className="tool-event"><span>inspect_runtime</span><code>grok-build / agent loop</code><em>completed</em></div>
      </div>
    </div>
  )
}

function Step({ step, index, last }: { step: AgentStep; index: number; last: boolean }) {
  const Icon = step.state === 'complete' ? IconCheck : step.state === 'running' ? IconLoader2 : step.state === 'error' ? IconAlertTriangle : IconClock
  return (
    <div className={`timeline-row ${step.state}`}>
      <div className="timeline-rail"><span><Icon size={15} className={step.state === 'running' ? 'spin' : ''} /></span>{!last && <i />}</div>
      <div className="timeline-copy"><div><small>STEP {index}</small><strong>{step.label}</strong></div><p>{step.detail}</p></div>
      <span className="step-state">{step.state}</span>
    </div>
  )
}
