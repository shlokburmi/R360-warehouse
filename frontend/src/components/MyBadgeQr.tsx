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
 */
export function MyBadgeQr() {
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

  return (
    <Card title={t('badge.mine_title')} subtitle={t('badge.mine_hint')}>
      <img
        src={badge.data.badge_qr}
        alt=""
        width={220}
        height={220}
        className="mx-auto"
      />
    </Card>
  )
}
