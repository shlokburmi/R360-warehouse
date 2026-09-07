import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import { ApiError, get } from '@/lib/api'
import { useErrorText } from '@/hooks/useErrorText'
import { Banner, Card, Spinner } from '@/components/ui'
import type { MyBadge } from '@/types'

/**
 * A badge holder's own current QR, shown on her own dashboard so a colleague
 * (e.g. an Invoice Matcher at /invoices/assign) can scan it off the screen
 * instead of the printed card — see /badges/mine and
 * 0037_self_badge_view.sql. Self-only: the backend resolves the badge from
 * the caller's own session, so this can never show anyone else's.
 *
 * Image only, same as BadgeCardPrint — the raw code is never put on screen
 * as text.
 *
 * `bare` skips the surrounding Card so this can sit inside another one (see
 * AboutMe.tsx) instead of nesting two cards.
 */
export function MyBadgeQr({ bare = false }: { bare?: boolean }) {
  const { t } = useTranslation()
  const errorText = useErrorText()
  const [fullscreen, setFullscreen] = useState(false)

  const badge = useQuery({
    queryKey: ['badges', 'mine'],
    queryFn: () => get<MyBadge>('/badges/mine'),
  })

  if (badge.isLoading) return <Spinner label={t('badge.loading_mine')} />

  if (badge.error) {
    const error = badge.error as ApiError
    return (
      <Banner tone="warn" title={errorText(error).title}>
        {error.hint}
      </Banner>
    )
  }

  if (!badge.data) return null

  // A phone screen makes a much smaller physical QR than, say, a laptop
  // screen showing the same image — a scanning camera reads a QR's physical
  // size, not its pixel count, so a small one is genuinely harder to
  // resolve, not just a rendering nitpick. The inline image is sized up from
  // its old fixed 260px, but the fullscreen view is the real fix: it uses as
  // much of the screen as the code's own aspect ratio allows, which a card
  // sitting in a page of other content never can.
  const image = (
    <div className="mx-auto w-fit max-w-full rounded-xl bg-white p-4">
      <img
        src={badge.data.badge_qr}
        alt=""
        className="mx-auto block h-auto w-full max-w-[22rem]"
      />
    </div>
  )

  const content = (
    <>
      {image}
      <button
        type="button"
        className="btn-primary mt-3 w-full"
        onClick={() => setFullscreen(true)}
      >
        {t('badge.view_fullscreen')}
      </button>
    </>
  )

  return (
    <>
      {bare ? (
        content
      ) : (
        <Card title={t('badge.mine_title')} subtitle={t('badge.mine_hint')}>
          {content}
        </Card>
      )}

      {fullscreen && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label={t('badge.mine_title')}
          className="fixed inset-0 z-50 flex flex-col items-center justify-center gap-6 bg-white p-6"
        >
          <img
            src={badge.data.badge_qr}
            alt=""
            className="block"
            style={{ width: 'min(85vw, 85vh)', height: 'min(85vw, 85vh)' }}
          />
          <button
            type="button"
            className="btn-primary w-full max-w-xs"
            onClick={() => setFullscreen(false)}
          >
            {t('common.done')}
          </button>
        </div>
      )}
    </>
  )
}
