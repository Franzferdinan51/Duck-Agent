import type { CSSProperties } from 'react'

export function BrandMark({ size = 32, className = '' }: { size?: number; className?: string }) {
  const style = { '--duck-size': `${size}px` } as CSSProperties
  return (
    <span className={`duck-brand-mark ${className}`} style={style} aria-label="Duck Agent">
      <svg width={size} height={size} viewBox="0 0 64 64" role="img" aria-hidden="true">
        <rect x="3" y="3" width="58" height="58" rx="16" fill="currentColor" opacity="0.12" />
        <path d="M21 36.5c0-10.4 7.2-19 16.1-19 7.5 0 13.6 5.3 14.8 12.3-4.7-1.8-9.6-1.2-13.2 1.4 6.1.1 10.8 2 14.3 5.6-3.4 7.4-10.8 12.5-19.4 12.5C23.8 49.3 14 43.8 11 35.7c2.9 1.1 6.3 1.4 10 .8Z" fill="currentColor" />
        <path d="M48.1 27.8 59 31.5l-10.5 4.2c-2-2.1-4.4-3.6-7.2-4.4 2.5-1.8 4.8-2.9 6.8-3.5Z" fill="#f4a51c" />
        <circle cx="39.5" cy="24.7" r="2.2" fill="#0b1015" />
        <path d="M18.5 43.2c5.7 1.8 10.5 1.5 14.2-.8" fill="none" stroke="#0b1015" strokeWidth="2.5" strokeLinecap="round" opacity=".62" />
      </svg>
    </span>
  )
}
