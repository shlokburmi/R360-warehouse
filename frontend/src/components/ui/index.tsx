import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'

import { cn } from '@/lib/utils'

/**
 * Shared primitives. PRD §6: large targets, high contrast, colour-coded status,
 * readable outdoors. The colour is never the only signal — every banner also
 * carries a word and an icon, because sunlight and colour blindness both flatten
 * a green/red distinction.
 *
 * These eight components are the whole restyle surface. Twenty pages import from
 * here and style themselves entirely through these plus the CSS classes in
 * styles/index.css, so the visual language changed without a single page's logic
 * being touched. Every export keeps its previous name and prop shape for exactly
 * that reason.
 *
 * What the restyle changed: surfaces became frosted glass over the gradient
 * wash, radii grew, and shadows became coloured rather than neutral. What it did
 * not change: the status palette, the type scale, and the touch targets. Those
 * three are the ones a guard's ability to use this at a gate depends on.
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
      /* Deliberately opaque, unlike every other surface here. A banner is the
         one element whose job is to be unmissable — putting a translucent
         backdrop-blur behind a "count mismatch" message would let the gradient
         underneath decide how visible the refusal is. */
      className={cn(
        'flex items-start gap-3 rounded-2xl p-4 ring-1 ring-inset ring-black/5 dark:ring-white/10',
        TONE_CLASSES[tone],
      )}
      role={tone === 'bad' ? 'alert' : 'status'}
    >
      <span
        aria-hidden
        /* `bg-current/15` was here before the restyle and never rendered —
           Tailwind cannot apply an opacity modifier to currentColor, so it
           emitted nothing and the circle was transparent. These two tint off
           the theme instead, which is what it was reaching for. */
        className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-black/10 text-lg font-black dark:bg-white/15"
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
  /** Opt out of the frosted surface — used by anything that gets printed. */
  solid,
  className,
}: {
  title?: string
  subtitle?: string
  children: ReactNode
  action?: ReactNode
  solid?: boolean
  className?: string
}) {
  return (
    <section className={cn(solid ? 'card-solid' : 'card', className)}>
      {(title || action) && (
        <header className="mb-4 flex items-start justify-between gap-3">
          <div>
            {title && <h2 className="text-xl font-bold tracking-tight">{title}</h2>}
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
        <p className="mt-1.5 text-sm text-slate-600 dark:text-slate-400">{hint}</p>
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
      <p className="text-sm font-bold uppercase tracking-wide text-slate-600 dark:text-slate-400">
        {label}
      </p>
      <p
        className={cn(
          'my-2 text-5xl font-black tabular-nums transition-colors sm:text-6xl',
          complete ? 'text-ok dark:text-ok-dark' : 'text-slate-900 dark:text-slate-100',
        )}
      >
        {scanned}
        <span className="text-3xl text-slate-500 dark:text-slate-500"> / {total}</span>
      </p>
      <div className="h-3 overflow-hidden rounded-full bg-slate-200/80 dark:bg-white/10">
        <div
          className={cn(
            'h-full rounded-full transition-all duration-300',
            complete
              ? 'bg-gradient-to-r from-ok to-green-500'
              : 'bg-gradient-to-r from-blue-600 to-cyan-400',
          )}
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
  const { t } = useTranslation()
  const tone = STATUS_TONES[status] ?? 'info'
  // The status arrives as a raw Postgres enum value. An unknown one falls back to
  // the de-underscored English rather than rendering the key, because a new enum
  // added server-side must not blank out the chip.
  const label = t(`status.${status}`, { defaultValue: status.replace(/_/g, ' ') })
  return <span className={cn('chip', TONE_CLASSES[tone])}>{label}</span>
}

export function Spinner({ label = 'Loading…' }: { label?: string }) {
  return (
    <div
      className="flex items-center justify-center gap-3 py-10 text-slate-600 dark:text-slate-300"
      role="status"
    >
      <span className="h-6 w-6 animate-spin rounded-full border-4 border-slate-300/70 border-t-blue-600 dark:border-white/20 dark:border-t-blue-400" />
      <span className="text-lg">{label}</span>
    </div>
  )
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    // Deliberately *not* a `.card`. This is used both standalone (Approvals,
    // Entries) and nested inside a Card that already provides the surface
    // (Dashboard, Reports) — giving it its own frosted panel would double the
    // border and the blur in the nested case.
    <div className="py-12 text-center">
      <p className="text-lg font-bold text-slate-700 dark:text-slate-200">{title}</p>
      {hint && <p className="mt-1 text-base text-slate-600 dark:text-slate-400">{hint}</p>}
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
  const alarming = Number(value) > 0 && (tone === 'bad' || tone === 'warn')

  return (
    <Tag
      onClick={onClick}
      className={cn(
        'card text-left',
        onClick && 'w-full transition-all hover:-translate-y-0.5 hover:shadow-xl',
        // A non-zero count of held boxes or breached SLAs is the reason someone
        // opened the dashboard. It gets a tinted edge so it is findable in
        // peripheral vision, not just once you read the number.
        alarming && (tone === 'bad' ? 'ring-1 ring-bad/30' : 'ring-1 ring-warn/40'),
      )}
    >
      <p className="text-sm font-bold uppercase tracking-wide text-slate-600 dark:text-slate-400">
        {label}
      </p>
      <p
        className={cn(
          'mt-1 text-3xl font-black tabular-nums sm:text-4xl',
          alarming &&
            (tone === 'bad' ? 'text-bad dark:text-bad-dark' : 'text-warn dark:text-warn-dark'),
        )}
      >
        {value}
      </p>
    </Tag>
  )
}

export { AnimatedGroup, heroTransitionVariants } from './animated-group'
export { LanguageToggle } from './language-toggle'
export { GradientBackdrop } from './gradient-backdrop'
export { Button, buttonVariants } from './button'
export { ThemeToggle } from './theme-toggle'
