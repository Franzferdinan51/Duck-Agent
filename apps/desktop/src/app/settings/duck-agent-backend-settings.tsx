import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { useI18n } from '@/i18n'
import { Check, Zap, Bot, Cpu } from '@/lib/icons'
import { notify, notifyError } from '@/store/notifications'
import { cn } from '@/lib/utils'

const BACKENDS = [
  {
    id: 'grok-build',
    name: 'Grok Build',
    description:
      'Primary harness. Agent work (chat/work) runs on the installed grok binary; the Duck-Agent CLI/launcher hands off to it.',
    icon: Zap,
    recommended: true
  },
  {
    id: 'hermes-compatible',
    name: 'Duck-Agent-Compatible',
    description: 'Duck-Agent compatibility mode (embedded runtime)', 
    icon: Bot
  },
  {
    id: 'prime-agent',
    name: 'Prime Agent',
    description: 'Prime Intellect RLM-based agent',
    icon: Cpu
  }
] as const

type BackendId = (typeof BACKENDS)[number]['id']

interface BackendSettingsProps {
  className?: string
}

export function DuckAgentBackendSettings({ className }: BackendSettingsProps) {
  const { t } = useI18n()
  const [selected, setSelected] = useState<BackendId>(() => {
    return (localStorage.getItem('duck_agent_backend') as BackendId) || 'grok-build'
  })
  const [saving, setSaving] = useState(false)

  async function handleSave() {
    setSaving(true)
    try {
      localStorage.setItem('duck_agent_backend', selected)
      // In a full implementation, this would also update the backend env/config
      notify(t.settings.saved || 'Settings saved')
    } catch (err) {
      notifyError(err, 'Failed to save backend preference')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className={cn('space-y-6', className)}>
      <div>
        <h2 className="text-lg font-semibold">Duck Agent Backend</h2>
        <p className="text-muted mt-1 text-sm">
          Select the agent backend that powers Duck Agent. Each backend offers different capabilities.
        </p>
      </div>

      <div className="space-y-3">
        {BACKENDS.map(backend => {
          const Icon = backend.icon
          const isSelected = selected === backend.id

          return (
            <button
              key={backend.id}
              className={cn(
                'w-full text-left p-4 rounded-lg border-2 transition-all',
                'hover:border-primary/50',
                isSelected ? 'border-primary bg-primary/5' : 'border-transparent bg-secondary'
              )}
              onClick={() => setSelected(backend.id)}
            >
              <div className="flex items-start gap-3">
                <div
                  className={cn(
                    'mt-0.5 p-2 rounded-lg',
                    isSelected ? 'bg-primary text-primary-foreground' : 'bg-muted'
                  )}
                >
                  <Icon className="size-5" />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-medium">{backend.name}</span>
                    {backend.recommended && (
                      <span className="text-xs px-2 py-0.5 rounded-full bg-primary/20 text-primary">
                        Recommended
                      </span>
                    )}
                    {isSelected && <Check className="size-4 text-primary ml-auto" />}
                  </div>
                  <p className="text-sm text-muted mt-1">{backend.description}</p>
                </div>
              </div>
            </button>
          )
        })}
      </div>

      <div className="flex justify-end pt-4 border-t">
        <Button onClick={handleSave} disabled={saving}>
          {saving ? 'Saving...' : 'Save Changes'}
        </Button>
      </div>
    </div>
  )
}
