import { IconActivity, IconBolt, IconBrain, IconPlugConnected, IconShieldCheck } from '@tabler/icons-react'
import type { AgentRun } from '../app/types'

export function RuntimeInspector({ run }: { run: AgentRun }) {
  return (
    <aside className="inspector">
      <div className="inspector-head"><span>Run inspector</span><IconActivity size={17} /></div>
      <section><h3>Runtime</h3><Info icon={IconBolt} title="Grok Build" value="Primary harness" status /><Info icon={IconBrain} title="Agent loop" value={`${run.steps.length} planned steps`} /><Info icon={IconPlugConnected} title="Tools" value="3 enabled" /></section>
      <section><h3>Autonomy</h3><Info icon={IconShieldCheck} title="Approval policy" value="Balanced" status /><div className="policy-copy">Read and research actions can run automatically. Destructive operations should pause for approval.</div></section>
      <section><h3>Context</h3><div className="context-chip">Duck-Agent repository</div><div className="context-chip">Desktop application</div><div className="context-chip">Hermes-derived UI</div></section>
      <div className="inspector-footer"><span className="live-dot" /><div><strong>Runtime connected</strong><small>Streaming structured events</small></div></div>
    </aside>
  )
}

function Info({ icon: Icon, title, value, status = false }: { icon: typeof IconActivity; title: string; value: string; status?: boolean }) {
  return <div className="info-row"><span className="info-icon"><Icon size={16} /></span><div><strong>{title}</strong><small>{value}</small></div>{status && <i />}</div>
}
