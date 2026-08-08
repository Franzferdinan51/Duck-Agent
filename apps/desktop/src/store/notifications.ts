export function notify(message: string): void {
  window.dispatchEvent(new CustomEvent('duck-agent:notification', { detail: { type: 'info', message } }))
}

export function notifyError(error: unknown, fallback = 'Something went wrong'): void {
  const message = error instanceof Error ? error.message : fallback
  window.dispatchEvent(new CustomEvent('duck-agent:notification', { detail: { type: 'error', message } }))
}
