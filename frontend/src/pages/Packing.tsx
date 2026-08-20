import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, get, post } from '@/lib/api'
import { useErrorText } from '@/hooks/useErrorText'
import { useScanning } from '@/hooks/useScanning'
import { useAuth } from '@/hooks/useAuth'
import { BadgeScan } from '@/components/BadgeScan'
import { Scanner } from '@/components/Scanner'
import { Banner, Card, EmptyState, ProgressCounter, Spinner } from '@/components/ui'
import type { AssignResult, AttributionResult, Invoice, PackingState } from '@/types'

/**
 * PRD §5.5 — Packing (Packing Ladies). CONTROL POINT 5, second half.
 *
 * Three steps, in the order the floor does them:
 *
 *  1. **Assign.** A lead scans the packer's badge card and the carton becomes
 *     hers. Scanning a colleague's card is the intended use of a badge — it is
 *     physically present at the bench — and it is why this needs no relaxation
 *     of the rule that nobody can *look up* a badge code.
 *
 *  2. **Scan the product boxes in.** Since migration 0019 this is what closes
 *     the gap between goods received and goods dispatched: a product box counted
 *     into the warehouse that never appears in a carton used to pass every
 *     control point.
 *
 *  3. **Close the carton.** Refused until the count matches, by the database.
 *
 * The queue only ever contains verified invoices, because an unverified one
 * cannot be assigned or packed at all.
 */
export function PackingPage() {
  const { t } = useTranslation()
  const errorText = useErrorText()
  const queryClient = useQueryClient()
  const { me } = useAuth()

  const [selected, setSelected] = useState<Invoice | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [result, setResult] = useState<AttributionResult | null>(null)
  const [assigning, setAssigning] = useState(false)

  const ready = useQuery({
    queryKey: ['invoices', 'verified'],
    queryFn: () => get<Invoice[]>('/invoices?stage=verified'),
    refetchInterval: 15_000,
  })

  const mine = useQuery({
    queryKey: ['packing', 'assigned-to-me'],
    queryFn: () => get<PackingState[]>('/packing/assigned-to-me'),
    refetchInterval: 15_000,
  })

  const invoiceId = selected?.invoice_id ?? ''

  const state = useQuery({
    queryKey: ['packing-state', invoiceId],
    queryFn: () => get<PackingState>(`/invoices/${invoiceId}/packing`),
    enabled: Boolean(invoiceId),
  })

  // The same scanning loop as every other station, so the offline queue and its
  // idempotent replay apply here unchanged.
  const scanning = useScanning(invoiceId, 'pack_unit')

  const assign = useMutation({
    mutationFn: (badgeCode: string) =>
      post<AssignResult>('/invoices/assign', {
        invoice_number: selected?.invoice_number,
        badge_code: badgeCode,
      }),
    onSuccess: () => {
      setError(null)
      setAssigning(false)
      void queryClient.invalidateQueries({ queryKey: ['packing-state', invoiceId] })
      void queryClient.invalidateQueries({ queryKey: ['packing', 'assigned-to-me'] })
    },
    onError: (err) => setError(err as ApiError),
  })

  const pack = useMutation({
    mutationFn: (badgeCode: string) =>
      post<AttributionResult>('/invoices/pack', {
        invoice_number: selected?.invoice_number,
        badge_code: badgeCode,
      }),
    onSuccess: (data) => {
      setResult(data)
      setError(null)
      setSelected(null)
      void queryClient.invalidateQueries({ queryKey: ['invoices'] })
      void queryClient.invalidateQueries({ queryKey: ['packing', 'assigned-to-me'] })
    },
    onError: (err) => setError(err as ApiError),
  })

  function open(invoice: Invoice) {
    setSelected(invoice)
    setResult(null)
    setError(null)
    setAssigning(false)
  }

  if (ready.isLoading) return <Spinner label={t('packing.loading')} />

  const packing = state.data
  const assignedElsewhere =
    packing?.assigned_to && me?.id && packing.assigned_to !== me.id

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-black">{t('packing.title')}</h1>

      {error && (
        <Banner tone={error.isControlPoint ? 'bad' : 'warn'} title={errorText(error).title}>
          {error.hint}
        </Banner>
      )}

      {result && !selected && (
        <Banner tone="ok" title={result.message}>
          {t('packing.confirm_packed')}
        </Banner>
      )}

      {selected && packing ? (
        <>
          <Card
            title={selected.invoice_number}
            subtitle={selected.customer_name ?? undefined}
            action={
              <button
                type="button"
                className="text-base font-semibold text-slate-500"
                onClick={() => setSelected(null)}
              >
                {t('common.back')}
              </button>
            }
          >
            <dl className="grid grid-cols-1 gap-3 text-base sm:grid-cols-2">
              <div>
                <dt className="text-slate-500 dark:text-slate-400">{t('matching.product')}</dt>
                <dd className="text-lg font-bold">{selected.sku}</dd>
                <dd>{selected.description}</dd>
              </div>
              <div>
                <dt className="text-slate-500 dark:text-slate-400">{t('matching.quantity')}</dt>
                <dd className="text-4xl font-black tabular-nums">{packing.required_units}</dd>
              </div>
            </dl>
          </Card>

          {/* Step 1 — assignment. Shown first and only until it is done, so the
              bench is never looking at two open questions at once. */}
          {!packing.assigned_to || assigning ? (
            <Card title={t('packing.assign_title')} subtitle={t('packing.assign_hint')}>
              <BadgeScan
                label={t('packing.assign_scan_badge')}
                busy={assign.isPending}
                onBadge={(code) => assign.mutate(code)}
              />
              {assigning && (
                <button
                  type="button"
                  className="btn-ghost mt-3 w-full"
                  onClick={() => setAssigning(false)}
                >
                  {t('common.cancel')}
                </button>
              )}
            </Card>
          ) : (
            <Banner
              tone={assignedElsewhere ? 'warn' : 'ok'}
              title={t('packing.assigned_to', { name: packing.assigned_to_name })}
              action={
                <button
                  type="button"
                  className="text-base font-semibold underline"
                  onClick={() => setAssigning(true)}
                >
                  {t('packing.reassign')}
                </button>
              }
            >
              {assignedElsewhere &&
                t('packing.not_assigned_to_you', { name: packing.assigned_to_name })}
            </Banner>
          )}

          {/* Steps 2 and 3 — only once somebody owns the carton. */}
          {packing.assigned_to && !assigning && (
            <>
              <ProgressCounter
                scanned={packing.packed_units}
                total={packing.required_units}
                label={t('packing.products_in_carton')}
              />

              {!packing.ready_to_close ? (
                <Card title={t('packing.scan_products')} subtitle={t('packing.scan_products_hint')}>
                  <Scanner onScan={(code) => void scanning.submit(code)} paused={scanning.busy} />

                  {scanning.feedback.length > 0 && (
                    <ul className="mt-3 space-y-2">
                      {scanning.feedback.map((item) => (
                        <li key={item.id}>
                          <Banner
                            tone={item.tone}
                            title={item.code}
                          >
                            {item.message}
                          </Banner>
                        </li>
                      ))}
                    </ul>
                  )}
                </Card>
              ) : (
                <Card title={t('packing.carton_ready')}>
                  <BadgeScan
                    label={t('packing.scan_badge')}
                    busy={pack.isPending}
                    onBadge={(code) => pack.mutate(code)}
                  />
                </Card>
              )}
            </>
          )}
        </>
      ) : (
        <>
          {/* A packer's own queue comes first: it is the only list she can act
              on without someone handing her a card. */}
          {(mine.data?.length ?? 0) > 0 && (
            <Card title={t('packing.my_queue')}>
              <ul className="space-y-3">
                {mine.data?.map((item) => (
                  <li
                    key={item.invoice_id}
                    className="flex flex-wrap items-center justify-between gap-3"
                  >
                    <div className="min-w-0">
                      <p className="text-lg font-bold">{item.invoice_number}</p>
                      <p className="text-base text-slate-600 dark:text-slate-400">
                        {item.sku} ·{' '}
                        {t('packing.carton_short', {
                          packed: item.packed_units,
                          required: item.required_units,
                        })}
                      </p>
                    </div>
                    <button
                      type="button"
                      className="btn-primary"
                      onClick={() =>
                        open({
                          invoice_id: item.invoice_id,
                          invoice_number: item.invoice_number,
                          sku: item.sku ?? '',
                          units: item.required_units,
                        } as Invoice)
                      }
                    >
                      {t('packing.pack_this')}
                    </button>
                  </li>
                ))}
              </ul>
            </Card>
          )}

          {ready.data?.length === 0 && (mine.data?.length ?? 0) === 0 ? (
            <EmptyState title={t('packing.none')} hint={t('packing.none_hint')} />
          ) : (
            ready.data?.map((invoice) => (
              <Card
                key={invoice.invoice_id}
                title={invoice.invoice_number}
                subtitle={invoice.customer_name ?? undefined}
              >
                <div className="mb-4 flex items-baseline justify-between gap-3">
                  <div className="min-w-0">
                    <p className="text-lg font-bold">{invoice.sku}</p>
                    <p className="text-base text-slate-500 dark:text-slate-400">
                      {invoice.description}
                    </p>
                    <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
                      {invoice.verified_by_name}
                    </p>
                  </div>
                  <p className="text-3xl font-black tabular-nums">{invoice.units}</p>
                </div>
                <button
                  type="button"
                  className="btn-primary w-full"
                  onClick={() => open(invoice)}
                >
                  {t('packing.pack_this')}
                </button>
              </Card>
            ))
          )}
        </>
      )}
    </div>
  )
}
