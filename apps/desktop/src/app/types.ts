export type NavKey = 'chat' | 'goals' | 'tools' | 'memory' | 'skills' | 'workspace' | 'settings'
export type RunState = 'pending' | 'running' | 'complete' | 'error'

export interface Session {
  id: string
  title: string
  time: string
  status: 'idle' | 'running' | 'complete'
}

export interface AgentStep {
  id: string
  label: string
  detail: string
  state: RunState
}

export interface AgentRun {
  goal: string
  status: RunState
  steps: AgentStep[]
}
