import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, get, post } from '@/lib/api'
import { useErrorText } from '@/hooks/useErrorText'
import { BadgeScan } from '@/components/BadgeScan'
import { Banner, Card, EmptyState, Spinner } from '@/components/ui'
import type { AttributionResult, PackingState } from '@/types'

/**
 * PRD §5.5 — Packing (Packing Ladies). CONTROL POINT 5, second half.
 *
 * One step: a carton lands here already assigned (a Packer scanned the
 * physical invoice and handed it to this packing lady by scanning her badge
 * — see InvoiceMatching.tsx). She scans her own badge to confirm she packed
 * it. There is no product/quantity scanning step — what's actually inside
 * the carton is Admin's separate ERP's concern, not this app's
 * (0036_invoice_flow_simplified.sql).
 */
export function PackingPage() {
  const { t } = useTranslation()
  const errorText = useErrorText()
  const queryClient = useQueryClient()

  const [active, setActive] = useState<PackingState | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [result, setResult] = useState<AttributionResult | null>(null)

  const mine = useQuery({
    queryKey: ['packing', 'assigned-to-me'],
    queryFn: () => get<PackingState[]>('/packing/assigned-to-me'),
    refetchInterval: 15_000,
  })

  const pack = useMutation({
    mutationFn: (badgeCode: string) =>
      post<AttributionResult>('/invoices/pack', {
        invoice_number: active?.invoice_number,
        badge_code: badgeCode,
      }),
    onSuccess: (data) => {
      setResult(data)
      setError(null)
      setActive(null)
      void queryClient.invalidateQueries({ queryKey: ['invoices'] })
      void queryClient.invalidateQueries({ queryKey: ['packing', 'assigned-to-me'] })
    },
    onError: (err) => setError(err as ApiError),
  })

  if (mine.isLoading) return <Spinner label={t('packing.loading')} />

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-black">{t('packing.title')}</h1>

      {error && (
        <Banner tone={error.isControlPoint ? 'bad' : 'warn'} title={errorText(error).title}>
          {error.hint}
        </Banner>
      )}

      {result && !active && (
        <Banner tone="ok" title={result.message}>
          {t('packing.confirm_packed')}
        </Banner>
      )}

      {active ? (
        <Card
          title={active.invoice_number}
          action={
            <button
              type="button"
              className="text-base font-semibold text-slate-500"
              onClick={() => setActive(null)}
            >
              {t('common.back')}
            </button>
          }
        >
          <BadgeScan
            label={t('packing.scan_badge')}
            busy={pack.isPending}
            onBadge={(code) => pack.mutate(code)}
          />
        </Card>
      ) : (mine.data?.length ?? 0) === 0 ? (
        <EmptyState title={t('packing.none')} hint={t('packing.none_hint')} />
      ) : (
        <Card title={t('packing.my_queue')}>
          <ul className="space-y-3">
            {mine.data?.map((item) => (
              <li
                key={item.invoice_id}
                className="flex flex-wrap items-center justify-between gap-3"
              >
                <p className="text-lg font-bold">{item.invoice_number}</p>
                <button
                  type="button"
                  className="btn-primary"
                  onClick={() => {
                    setActive(item)
                    setResult(null)
                    setError(null)
                  }}
                >
                  {t('packing.pack_this')}
                </button>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  )
}
