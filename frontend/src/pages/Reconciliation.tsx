import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, get, postControlPoint } from '@/lib/api'
import { useErrorText } from '@/hooks/useErrorText'
import { useAuth } from '@/hooks/useAuth'
import { Banner, Card, Spinner } from '@/components/ui'
import type { Reconciliation } from '@/types'

/**
 * PRD Step 5 — Inbound reconciliation, done by the offloading team. CONTROL POINT 4.
 *
 * The warehouse figure is shown but not editable: it is derived from the scan
 * ledger, and letting anyone type over it would make the comparison
 * meaningless. The offloading team enters their own independent count, and a
 * disagreement blocks putaway rather than being averaged away.
 */
export function ReconciliationPage() {
  const { t } = useTranslation()
  const errorText = useErrorText()
  const { entryId = '' } = useParams()
  const queryClient = useQueryClient()
  const { me } = useAuth()

  // CONTROL POINT 4 is the offloading team's own independent count, and the API
  // accepts the submission from offloading (and admin) only. Ops Manager can
  // open this page to see where a count stands — that is the point of an
  // independent count being visible — but the inputs are not theirs to fill.
  const isInbound = me?.role === 'offloading' || me?.role === 'admin'
  const [counts, setCounts] = useState<Record<string, string>>({})
  const [error, setError] = useState<ApiError | null>(null)

  const reconciliation = useQuery({
    queryKey: ['reconciliation', entryId],
    queryFn: () => get<Reconciliation>(`/entries/${entryId}/reconciliation`),
  })

  // CONTROL POINT 4 answers 409 on a mismatch with the compared lines and the
  // exception code, which is exactly what the page needs to show.
  const submit = useMutation({
    mutationFn: () =>
      postControlPoint<Reconciliation>(`/entries/${entryId}/reconciliation`, {
        lines: Object.entries(counts).map(([lineId, value]) => ({
          purchase_order_line_id: lineId,
          inbound_count: Number(value),
        })),
      }),
    onSuccess: (result) => {
      setError(null)
      // The response *is* the new reconciliation state (CONTROL POINT 4
      // answers with the compared lines either way), so apply it directly
      // rather than throwing it away and waiting on a refetch to learn what
      // we were already told. That second round trip was the whole delay
      // between tapping Submit and the page acknowledging it — on a slow
      // connection or a cold backend, long enough to look like nothing
      // happened. The invalidate still runs behind this.
      queryClient.setQueryData(['reconciliation', entryId], result)
      void queryClient.invalidateQueries({ queryKey: ['reconciliation', entryId] })
      void queryClient.invalidateQueries({ queryKey: ['entry', entryId] })
    },
    onError: (err) => setError(err as ApiError),
  })

  if (reconciliation.isLoading) return <Spinner />
  if (!reconciliation.data) return <Banner tone="bad" title={t('recon.none')} />

  const lines = reconciliation.data.lines
  const allEntered = lines.every(
    (line) => counts[line.purchase_order_line_id] !== undefined || line.inbound_count !== null,
  )

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-black">{t('recon.title')}</h1>

      {error && (
        <Banner tone={error.isControlPoint ? 'bad' : 'warn'} title={errorText(error).title}>
          {error.hint}
        </Banner>
      )}

      <Banner
        tone={
          reconciliation.data.all_matched
            ? 'ok'
            : submit.data && !submit.data.all_matched
              ? 'bad'
              : 'info'
        }
        title={submit.data?.message ?? reconciliation.data.message}
      >
        {submit.data?.exception_code &&
          `Exception ${submit.data.exception_code} raised. Putaway is blocked until the counts agree.`}
      </Banner>

      {lines.map((line) => {
        const entered = counts[line.purchase_order_line_id] ?? line.inbound_count?.toString() ?? ''
        const mismatch = entered !== '' && Number(entered) !== line.warehouse_count

        return (
          <Card key={line.purchase_order_line_id} title={line.sku} subtitle={line.description}>
            <div className="grid grid-cols-3 gap-3">
              <div>
                <p className="label">{t('recon.po_expected')}</p>
                <p className="text-2xl font-black tabular-nums">{line.expected_units}</p>
              </div>
              <div>
                <p className="label">{t('recon.warehouse')}</p>
                <p className="text-2xl font-black tabular-nums">{line.warehouse_count}</p>
                <p className="text-xs text-slate-500">{t('recon.from_scans')}</p>
              </div>
              <div>
                <p className="label">{t('recon.your_count')}</p>
                {isInbound ? (
                  <input
                    className={`input text-center text-2xl font-black ${
                      mismatch ? 'input-error' : ''
                    }`}
                    type="number"
                    inputMode="numeric"
                    min={0}
                    value={entered}
                    onChange={(event) =>
                      setCounts((current) => ({
                        ...current,
                        [line.purchase_order_line_id]: event.target.value,
                      }))
                    }
                  />
                ) : (
                  <p className="text-2xl font-black tabular-nums">
                    {line.inbound_count ?? '—'}
                  </p>
                )}
              </div>
            </div>

            {mismatch && (
              <div className="mt-3">
                <Banner
                  tone="bad"
                  title={`Mismatch: warehouse ${line.warehouse_count} vs inbound ${entered}`}
                >
                  {t('recon.will_hold')}
                </Banner>
              </div>
            )}
          </Card>
        )
      })}

      {isInbound ? (
        <button
          type="button"
          className="btn-primary w-full"
          // Disabled once the counts agree: CONTROL POINT 4 is satisfied and
          // putaway is the next step, so leaving a live "Submit counts"
          // sitting under a green "ready for putaway" banner only invites a
          // second submission of the same numbers and leaves the operator
          // unsure whether the first one registered. A *mismatch* still
          // leaves it pressable, because that is the recount loop
          // `inbound_update`'s policy exists to allow (0005_rls.sql).
          disabled={!allEntered || submit.isPending || reconciliation.data.all_matched}
          onClick={() => submit.mutate()}
        >
          {reconciliation.data.all_matched
            ? t('recon.submitted')
            : submit.isPending
              ? 'Submitting…'
              : 'Submit counts'}
        </button>
      ) : (
        <Banner tone="info" title={t('recon.waiting_inbound')}>
          {t('recon.waiting_inbound_body')}
        </Banner>
      )}
    </div>
  )
}
