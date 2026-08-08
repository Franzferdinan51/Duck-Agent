import type { ButtonHTMLAttributes } from 'react'
import { cn } from '../../lib/utils'

export function Button({ className, type = 'button', ...props }: ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      type={type}
      className={cn('duck-button', className)}
      {...props}
    />
  )
}
