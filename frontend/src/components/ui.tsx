import type { ReactNode } from 'react'

/**
 * Shared primitives. PRD §6: large targets, high contrast, colour-coded status,
 * readable outdoors. The colour is never the only signal — every banner also
 * carries a word and an icon, because sunlight and colour blindness both flatten
 * a green/red distinction.
 */

type Tone = 'ok' | 'bad' | 'warn' | 'info'

const TONE_CLASSES: Record<Tone, string> = {
  ok: 'bg-ok-bg text-ok dark:bg-ok-darkbg dark:text-ok-dark',
  bad: 'bg-bad-bg text-bad dark:bg-bad-darkbg dark:text-bad-dark',
  warn: 'bg-warn-bg text-warn dark:bg-warn-darkbg dark:text-warn-dark',
  info: 'bg-info-bg text-info dark:bg-info-darkbg dark:text-info-dark',
}

const TONE_ICONS: Record<Tone, string> = { ok: '✓', bad: '✕', warn: '!', info: 'i' }

export function Banner({
  tone,
  title,
  children,
  action,
}: {
  tone: Tone
  title: string
  children?: ReactNode
  action?: ReactNode
}) {
  return (
    <div
      className={`flex items-start gap-3 rounded-xl p-4 ${TONE_CLASSES[tone]}`}
      role={tone === 'bad' ? 'alert' : 'status'}
    >
      <span
        aria-hidden
        className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-current/15 text-lg font-black"
      >
        {TONE_ICONS[tone]}
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-lg font-bold">{title}</p>
        {children && <div className="mt-1 text-base">{children}</div>}
      </div>
      {action}
    </div>
  )
}

export function Card({
  title,
  subtitle,
  children,
  action,
}: {
  title?: string
  subtitle?: string
  children: ReactNode
  action?: ReactNode
}) {
  return (
    <section className="card">
      {(title || action) && (
        <header className="mb-4 flex items-start justify-between gap-3">
          <div>
            {title && <h2 className="text-xl font-bold">{title}</h2>}
            {subtitle && (
              <p className="mt-0.5 text-base text-slate-600 dark:text-slate-400">{subtitle}</p>
            )}
          </div>
          {action}
        </header>
      )}
      {children}
    </section>
  )
}

export function Field({
  label,
  error,
  hint,
  required,
  children,
}: {
  label: string
  error?: string
  hint?: string
  required?: boolean
  children: ReactNode
}) {
  return (
    <div className="mb-4">
      <label className="label">
        {label}
        {required && <span className="ml-1 text-bad dark:text-bad-dark">*</span>}
      </label>
      {children}
      {hint && !error && (
        <p className="mt-1.5 text-sm text-slate-500 dark:text-slate-400">{hint}</p>
      )}
      {error && (
        <p className="mt-1.5 text-sm font-semibold text-bad dark:text-bad-dark" role="alert">
          {error}
        </p>
      )}
    </div>
  )
}

/** Big, unmissable count for the scanning pages — read from arm's length. */
export function ProgressCounter({
  scanned,
  total,
  label,
}: {
  scanned: number
  total: number
  label: string
}) {
  const complete = total > 0 && scanned >= total
  const pct = total > 0 ? Math.min(100, (scanned / total) * 100) : 0

  return (
    <div className="card text-center">
      <p className="text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
        {label}
      </p>
      <p
        className={`my-2 text-6xl font-black tabular-nums ${
          complete ? 'text-ok dark:text-ok-dark' : 'text-slate-900 dark:text-slate-100'
        }`}
      >
        {scanned}
        <span className="text-3xl text-slate-400 dark:text-slate-500"> / {total}</span>
      </p>
      <div className="h-3 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-800">
        <div
          className={`h-full rounded-full transition-all duration-300 ${
            complete ? 'bg-ok' : 'bg-blue-600'
          }`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  )
}

const STATUS_TONES: Record<string, Tone> = {
  pending_approval: 'warn',
  approved: 'ok',
  rejected: 'bad',
  cancelled: 'bad',
  inside: 'info',
  counting: 'info',
  box_verified: 'ok',
  offloading: 'info',
  offloaded: 'ok',
  reconciled: 'ok',
  departed: 'info',
  pending: 'warn',
  verified: 'info',
  scanning: 'info',
  complete: 'ok',
  held: 'bad',
  short_accepted: 'warn',
  emptied: 'info',
  open: 'bad',
  escalated: 'bad',
  resolved: 'ok',
  withdrawn: 'info',
}

export function StatusChip({ status }: { status: string }) {
  const tone = STATUS_TONES[status] ?? 'info'
  return (
    <span className={`chip ${TONE_CLASSES[tone]}`}>{status.replace(/_/g, ' ')}</span>
  )
}

export function Spinner({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-3 py-10 text-slate-500" role="status">
      <span className="h-6 w-6 animate-spin rounded-full border-4 border-slate-300 border-t-blue-600" />
      <span className="text-lg">{label}</span>
    </div>
  )
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="py-12 text-center">
      <p className="text-lg font-semibold text-slate-600 dark:text-slate-300">{title}</p>
      {hint && <p className="mt-1 text-base text-slate-500 dark:text-slate-400">{hint}</p>}
    </div>
  )
}

export function Stat({
  label,
  value,
  tone = 'info',
  onClick,
}: {
  label: string
  value: number | string
  tone?: Tone
  onClick?: () => void
}) {
  const Tag = onClick ? 'button' : 'div'
  return (
    <Tag
      onClick={onClick}
      className={`card text-left ${onClick ? 'w-full transition hover:brightness-105' : ''}`}
    >
      <p className="text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
        {label}
      </p>
      <p
        className={`mt-1 text-4xl font-black tabular-nums ${
          Number(value) > 0 && (tone === 'bad' || tone === 'warn')
            ? TONE_CLASSES[tone].split(' ').filter((c) => c.startsWith('text-')).join(' ')
            : ''
        }`}
      >
        {value}
      </p>
    </Tag>
  )
}
