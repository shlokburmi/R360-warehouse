import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, get, post } from '@/lib/api'
import { BadgeScan } from '@/components/BadgeScan'
import { Banner, Card, EmptyState, Spinner } from '@/components/ui'
import type { AttributionResult, Invoice } from '@/types'

/**
 * PRD §5.5 — Packing attribution (Packing Ladies).
 * CONTROL POINT 5, second half.
 *
 * The packer picks up a product plus its invoice from the matching table and
 * packs it. All this screen does is bind her badge to that invoice at that
 * moment — which is what makes an error six weeks later traceable to a person
 * rather than to a shift.
 *
 * The list only ever contains verified invoices, because an unverified one
 * cannot be packed at all: the database refuses it.
 */
export function PackingPage() {
  const queryClient = useQueryClient()

  const [selected, setSelected] = useState<Invoice | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [result, setResult] = useState<AttributionResult | null>(null)

  const ready = useQuery({
    queryKey: ['invoices', 'verified'],
    queryFn: () => get<Invoice[]>('/invoices?stage=verified'),
    refetchInterval: 15_000,
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
    },
    onError: (err) => setError(err as ApiError),
  })

  if (ready.isLoading) return <Spinner label="Loading invoices…" />

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-black">Packing</h1>

      {error && (
        <Banner tone={error.isControlPoint ? 'bad' : 'warn'} title={error.message}>
          {error.hint}
        </Banner>
      )}

      {result && !selected && (
        <Banner tone="ok" title={result.message}>
          Packed by {result.who.full_name}. The invoice and packer are now linked
          permanently.
        </Banner>
      )}

      {selected ? (
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
                Back
              </button>
            }
          >
            <dl className="grid grid-cols-2 gap-3 text-base">
              <div>
                <dt className="text-slate-500 dark:text-slate-400">Product</dt>
                <dd className="text-lg font-bold">{selected.sku}</dd>
                <dd>{selected.description}</dd>
              </div>
              <div>
                <dt className="text-slate-500 dark:text-slate-400">Quantity</dt>
                <dd className="text-4xl font-black tabular-nums">{selected.units}</dd>
              </div>
            </dl>

            <div className="mt-4">
              <Banner tone="ok" title={`Verified by ${selected.verified_by_name}`}>
                Your badge must be different from theirs — the system will refuse
                otherwise.
              </Banner>
            </div>
          </Card>

          <Card title="Scan your badge to record the pack">
            <BadgeScan
              label="Confirm you packed this carton"
              busy={pack.isPending}
              onBadge={(code) => pack.mutate(code)}
            />
          </Card>
        </>
      ) : ready.data?.length === 0 ? (
        <EmptyState
          title="Nothing ready to pack"
          hint="Invoices appear here once an invoice matcher has verified them."
        />
      ) : (
        ready.data?.map((invoice) => (
          <Card
            key={invoice.invoice_id}
            title={invoice.invoice_number}
            subtitle={invoice.customer_name ?? undefined}
          >
            <div className="mb-4 flex items-baseline justify-between gap-3">
              <div>
                <p className="text-lg font-bold">{invoice.sku}</p>
                <p className="text-base text-slate-500 dark:text-slate-400">
                  {invoice.description}
                </p>
                <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
                  Verified by {invoice.verified_by_name}
                </p>
              </div>
              <p className="text-3xl font-black tabular-nums">{invoice.units}</p>
            </div>
            <button
              type="button"
              className="btn-primary w-full"
              onClick={() => {
                setSelected(invoice)
                setResult(null)
                setError(null)
              }}
            >
              Pack this
            </button>
          </Card>
        ))
      )}
    </div>
  )
}
