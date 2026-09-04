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

  const image = (
    // A solid white frame around the image, not just the QR's own built-in
    // quiet zone — in dark mode the surrounding Card is a dark, frosted
    // surface, and a scanner reading this straight off a screen (rather than
    // a printed card, which was always on white) needs a clean, undistracted
    // border regardless of theme.
    <div className="mx-auto w-fit rounded-xl bg-white p-4">
      <img src={badge.data.badge_qr} alt="" width={260} height={260} />
    </div>
  )

  if (bare) return image

  return (
    <Card title={t('badge.mine_title')} subtitle={t('badge.mine_hint')}>
      {image}
    </Card>
  )
}
