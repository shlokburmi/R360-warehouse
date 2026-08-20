import { useTranslation } from 'react-i18next'

import { LANGUAGES, setLanguage, storedLanguage, type LanguageCode } from '@/i18n'
import { cn } from '@/lib/utils'

/**
 * Language switch, in two shapes for two very different moments.
 *
 * `variant="cards"` is the login screen: big tap targets, each language written
 * in its own script. Someone who cannot read English cannot be asked to find
 * "Language" in an English menu first, so the choice is offered in the language
 * being chosen.
 *
 * `variant="compact"` is the header, for correcting a wrong choice mid-shift.
 */
export function LanguageToggle({
  variant = 'compact',
  className,
}: {
  variant?: 'compact' | 'cards'
  className?: string
}) {
  const { i18n, t } = useTranslation()
  const active = (i18n.language as LanguageCode) ?? storedLanguage()

  if (variant === 'cards') {
    return (
      <div className={cn('grid grid-cols-2 gap-3', className)}>
        {LANGUAGES.map((lang) => (
          <button
            key={lang.code}
            type="button"
            onClick={() => void setLanguage(lang.code)}
            aria-pressed={active === lang.code}
            className={cn(
              'min-h-touch rounded-2xl border-2 px-4 py-3 text-xl font-bold transition-all',
              active === lang.code
                ? 'border-blue-600 bg-blue-50 text-blue-800 dark:border-blue-400 dark:bg-blue-500/15 dark:text-blue-200'
                : 'border-slate-300 bg-white/60 text-slate-700 hover:bg-white dark:border-white/15 dark:bg-white/5 dark:text-slate-200',
            )}
          >
            {lang.native}
          </button>
        ))}
      </div>
    )
  }

  return (
    <div
      className={cn(
        'flex shrink-0 rounded-xl border border-slate-300 bg-white/60 p-0.5 dark:border-white/15 dark:bg-white/5',
        className,
      )}
      role="group"
      aria-label={t('common.language')}
    >
      {LANGUAGES.map((lang) => (
        <button
          key={lang.code}
          type="button"
          onClick={() => void setLanguage(lang.code)}
          aria-pressed={active === lang.code}
          className={cn(
            'rounded-lg px-2.5 py-1.5 text-sm font-bold transition-colors',
            active === lang.code
              ? 'bg-blue-600 text-white'
              : 'text-slate-600 hover:text-slate-900 dark:text-slate-300 dark:hover:text-white',
          )}
        >
          {/* The label is the script itself, not a translated word for the
              language — "ಕನ್ನಡ" is legible to the person who needs it whatever
              the UI is currently set to. */}
          {lang.code === 'en' ? 'EN' : 'ಕನ್ನಡ'}
        </button>
      ))}
    </div>
  )
}
