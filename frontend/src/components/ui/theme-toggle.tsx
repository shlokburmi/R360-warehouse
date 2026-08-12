import { useTheme } from '@/hooks/useTheme'
import { cn } from '@/lib/utils'

/**
 * Light/dark switch.
 *
 * A two-state segmented control rather than a single icon that swaps between
 * ☀ and ☾. With one icon there is no way to tell whether it shows the current
 * mode or the one you are about to switch to — a genuine ambiguity, and the
 * previous header button had it. Here both options are always visible and the
 * active one is filled, so the current state and the available action are the
 * same glance.
 */
export function ThemeToggle({ className }: { className?: string }) {
  const { dark, setDark } = useTheme()

  return (
    <div
      role="group"
      aria-label="Colour theme"
      className={cn(
        'inline-flex shrink-0 items-center gap-0.5 rounded-xl border border-slate-300/70 bg-white/60 p-0.5 backdrop-blur',
        'dark:border-white/15 dark:bg-white/5',
        className,
      )}
    >
      {(
        [
          { value: false, icon: '☀', label: 'Light' },
          { value: true, icon: '☾', label: 'Dark' },
        ] as const
      ).map((option) => {
        const active = dark === option.value
        return (
          <button
            key={option.label}
            type="button"
            onClick={() => setDark(option.value)}
            aria-pressed={active}
            title={`${option.label} mode`}
            className={cn(
              'rounded-[0.6rem] px-2.5 py-1.5 text-base leading-none transition-all',
              active
                ? 'bg-gradient-to-b from-blue-600 to-blue-700 text-white shadow-sm'
                : 'text-slate-600 hover:bg-white/70 dark:text-slate-400 dark:hover:bg-white/10',
            )}
          >
            <span aria-hidden>{option.icon}</span>
            <span className="sr-only">{option.label} mode</span>
          </button>
        )
      })}
    </div>
  )
}
