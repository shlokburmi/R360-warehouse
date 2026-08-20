import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Scanner } from '@/components/Scanner'
import { Banner } from '@/components/ui'

/**
 * Badge capture for attribution (PRD §5.4, §5.5).
 *
 * Worth being explicit about what this is: the badge records *who handled this
 * item*. It is not a login. The station tablet is already signed in; the badge
 * distinguishes which of several matchers or packers is standing at it.
 *
 * That is also why the badge code is never displayed back — it is an opaque
 * token, and echoing it onto a screen in a shared work area would turn it into
 * something worth copying.
 */
export function BadgeScan({
  label,
  onBadge,
  busy,
}: {
  label: string
  onBadge: (badgeCode: string) => void
  busy?: boolean
}) {
  const { t } = useTranslation()
  const [typed, setTyped] = useState('')
  const [showManual, setShowManual] = useState(false)

  return (
    <div className="space-y-3">
      <Banner tone="info" title={label}>
        Scanning your badge records that you handled this item. It does not sign
        you in.
      </Banner>

      {!showManual ? (
        <>
          <Scanner onScan={(code) => onBadge(code)} paused={busy} />
          <button
            type="button"
            className="btn-ghost w-full"
            onClick={() => setShowManual(true)}
          >
            {t('badge.type_instead')}
          </button>
        </>
      ) : (
        <form
          className="flex gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            onBadge(typed.trim())
            setTyped('')
          }}
        >
          <input
            className="input font-mono"
            placeholder="BDG-…"
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
            autoCorrect="off"
            spellCheck={false}
            aria-label={t('badge.code')}
          />
          <button type="submit" className="btn-primary" disabled={typed.trim().length < 4 || busy}>
            {t('common.confirm')}
          </button>
        </form>
      )}
    </div>
  )
}
