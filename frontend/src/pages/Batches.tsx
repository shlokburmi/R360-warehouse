import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, get, post, postControlPoint } from '@/lib/api'
import { useErrorText } from '@/hooks/useErrorText'
import { useScanning } from '@/hooks/useScanning'
import { useRealtimeInvalidate } from '@/hooks/useRealtimeInvalidate'
import {
  Banner,
  Card,
  EmptyState,
  ProgressCounter,
  Spinner,
  StatusChip,
} from '@/components/ui'
import type { Batch, BatchCompleteResult, Invoice } from '@/types'

/**
 * PRD §5.6 — Out-scan and batch release (Admin). CONTROL POINT 6.
 *
 * A batch is planned first, from the pool of packed cartons, and only then
 * out-scanned. That ordering is what makes the control point real: it compares
 * cartons assigned against cartons physically scanned. A batch assembled from
 * whatever happened to be scanned could never fail its own check.
 */
export function BatchesPage() {
  const { t } = useTranslation()
  const errorText = useErrorText()
  const queryClient = useQueryClient()
  const [openBatchId, setOpenBatchId] = useState<string | null>(null)
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [error, setError] = useState<ApiError | null>(null)

  const ready = useQuery({
    queryKey: ['packing-ready'],
    queryFn: () => get<Invoice[]>('/packing/ready'),
    refetchInterval: 20_000,
  })

  const batches = useQuery({
    queryKey: ['batches'],
    queryFn: () => get<Batch[]>('/batches'),
    refetchInterval: 20_000,
  })

  useRealtimeInvalidate('batches', [['batches']])

  const createBatch = useMutation({
    mutationFn: () =>
      post<Batch>('/batches', { invoice_ids: [...picked] }),
    onSuccess: (batch) => {
      setPicked(new Set())
      setError(null)
      setOpenBatchId(batch.batch_id)
      void queryClient.invalidateQueries({ queryKey: ['batches'] })
      void queryClient.invalidateQueries({ queryKey: ['packing-ready'] })
    },
    onError: (err) => setError(err as ApiError),
  })

  if (ready.isLoading || batches.isLoading) return <Spinner />

  if (openBatchId) {
    return (
      <BatchDetail
        batchId={openBatchId}
        onBack={() => {
          setOpenBatchId(null)
          void queryClient.invalidateQueries({ queryKey: ['batches'] })
        }}
      />
    )
  }

  const active = batches.data?.filter((b) => b.status !== 'released' && b.status !== 'cancelled')
  const done = batches.data?.filter((b) => b.status === 'released')

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-black">{t('batches.title')}</h1>

      {error && (
        <Banner tone={error.isControlPoint ? 'bad' : 'warn'} title={errorText(error).title}>
          {error.hint}
        </Banner>
      )}

      {/* A failed fetch here otherwise looks identical to "nothing waiting" —
          isLoading goes false on a failure too, not just a success. */}
      {(ready.isError || batches.isError) && (
        <Banner
          tone="warn"
          title={errorText((ready.error ?? batches.error) as ApiError).title}
        >
          {((ready.error ?? batches.error) as ApiError)?.hint}
        </Banner>
      )}

      {active && active.length > 0 && (
        <Card title={t('batches.in_progress')}>
          <ul className="divide-y divide-slate-200 dark:divide-slate-800">
            {active.map((batch) => (
              <li key={batch.batch_id} className="flex items-center justify-between gap-3 py-3">
                <div className="min-w-0">
                  <p className="font-bold">{batch.batch_code}</p>
                  <p className="text-sm text-slate-500 dark:text-slate-400">
                    {batch.scanned_cartons} of {batch.assigned_cartons} cartons scanned
                  </p>
                </div>
                <div className="flex items-center gap-3">
                  <StatusChip status={batch.status} />
                  <button
                    type="button"
                    className="btn-primary"
                    onClick={() => setOpenBatchId(batch.batch_id)}
                  >
                    Open
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <Card
        title={t('batches.waiting')}
        subtitle={t('batches.select_hint')}
      >
        {ready.data?.length === 0 ? (
          <EmptyState
            title={t('batches.none_waiting')}
            hint={t('batches.none_hint')}
          />
        ) : (
          <>
            <ul className="mb-4 divide-y divide-slate-200 dark:divide-slate-800">
              {ready.data?.map((invoice) => (
                <li key={invoice.invoice_id}>
                  <label className="flex cursor-pointer items-center gap-3 py-3">
                    <input
                      type="checkbox"
                      className="h-6 w-6 shrink-0"
                      checked={picked.has(invoice.invoice_id)}
                      onChange={(event) => {
                        const next = new Set(picked)
                        if (event.target.checked) next.add(invoice.invoice_id)
                        else next.delete(invoice.invoice_id)
                        setPicked(next)
                      }}
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block font-bold">{invoice.invoice_number}</span>
                      <span className="block truncate text-sm text-slate-500 dark:text-slate-400">
                        {invoice.customer_name} · packed by {invoice.packed_by_name}
                      </span>
                    </span>
                  </label>
                </li>
              ))}
            </ul>

            <div className="flex gap-3">
              <button
                type="button"
                className="btn-ghost"
                onClick={() =>
                  setPicked(new Set(ready.data?.map((i) => i.invoice_id) ?? []))
                }
              >
                {t('batches.select_all')}
              </button>
              <button
                type="button"
                className="btn-primary flex-1"
                disabled={picked.size === 0 || createBatch.isPending}
                onClick={() => createBatch.mutate()}
              >
                {createBatch.isPending
                  ? 'Creating…'
                  : `Create batch of ${picked.size} carton${picked.size === 1 ? '' : 's'}`}
              </button>
            </div>
          </>
        )}
      </Card>

      {done && done.length > 0 && (
        <Card title={t('batches.released')}>
          <ul className="divide-y divide-slate-200 dark:divide-slate-800">
            {done.slice(0, 10).map((batch) => (
              <li key={batch.batch_id} className="flex items-center justify-between gap-3 py-3">
                <div className="min-w-0">
                  <p className="font-bold">{batch.batch_code}</p>
                  <p className="text-sm text-slate-500 dark:text-slate-400">
                    {batch.assigned_cartons} cartons · released by {batch.released_by_name}
                  </p>
                </div>
                <StatusChip status={batch.status} />
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  )
}

function BatchDetail({ batchId, onBack }: { batchId: string; onBack: () => void }) {
  const { t } = useTranslation()
  const errorText = useErrorText()
  const queryClient = useQueryClient()
  const [error, setError] = useState<ApiError | null>(null)
  const [completeResult, setCompleteResult] = useState<BatchCompleteResult | null>(null)

  const batch = useQuery({
    queryKey: ['batch', batchId],
    queryFn: () => get<Batch>(`/batches/${batchId}`),
    refetchInterval: 10_000,
  })

  useRealtimeInvalidate('batches', [['batch', batchId]], `id=eq.${batchId}`)

  // Reuses the same scan loop as the gate and offloading pages, so out-scan gets
  // offline queueing and idempotent replay for free. There is no physical
  // carton label to scan any more, so each tap submits the carton's own
  // invoice number as the code — the resolver in fn_scan_resolve already
  // matches on invoice_number directly.
  const { feedback, submit, busy } = useScanning(batchId, 'out_scan')

  const complete = useMutation({
    mutationFn: () =>
      postControlPoint<BatchCompleteResult>(`/batches/${batchId}/complete`),
    onSuccess: (data) => {
      setCompleteResult(data)
      setError(null)
      void queryClient.invalidateQueries({ queryKey: ['batch', batchId] })
    },
    onError: (err) => setError(err as ApiError),
  })

  const release = useMutation({
    mutationFn: () => post<Batch>(`/batches/${batchId}/release`),
    onSuccess: () => {
      setError(null)
      void queryClient.invalidateQueries({ queryKey: ['batch', batchId] })
      void queryClient.invalidateQueries({ queryKey: ['batches'] })
    },
    onError: (err) => setError(err as ApiError),
  })

  if (batch.isLoading) return <Spinner />
  if (batch.isError) {
    // Distinct from "not found" below — a network failure must not be
    // reported as though the batch does not exist.
    const err = batch.error as ApiError
    return (
      <Banner tone="warn" title={errorText(err).title}>
        {err.hint}
      </Banner>
    )
  }
  if (!batch.data) return <Banner tone="bad" title={t('batches.not_found')} />

  const b = batch.data
  const scanning = b.status === 'open' || b.status === 'scanning'

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-2xl font-black">{b.batch_code}</h1>
          <p className="text-base text-slate-500 dark:text-slate-400">
            Created by {b.created_by_name}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <StatusChip status={b.status} />
          <button type="button" className="text-base font-semibold text-slate-500" onClick={onBack}>
            {t('common.back')}
          </button>
        </div>
      </div>

      {error && (
        <Banner tone={error.isControlPoint ? 'bad' : 'warn'} title={errorText(error).title}>
          {error.hint}
        </Banner>
      )}

      {completeResult && !completeResult.completed && (
        <Banner tone="bad" title={completeResult.message}>
          {t('batches.cannot_release')}
        </Banner>
      )}

      <ProgressCounter
        label={t('batches.cartons_out_scanned')}
        scanned={b.scanned_cartons}
        total={b.assigned_cartons}
      />

      {scanning && feedback.length > 0 && (
        <ul className="space-y-2">
          {feedback.map((item) => (
            <li
              key={item.id}
              className={`flex flex-wrap items-center justify-between gap-3 rounded-lg px-3 py-2 text-base ${
                item.tone === 'ok'
                  ? 'bg-ok-bg text-ok dark:bg-ok-darkbg dark:text-ok-dark'
                  : item.tone === 'warn'
                    ? 'bg-warn-bg text-warn dark:bg-warn-darkbg dark:text-warn-dark'
                    : 'bg-bad-bg text-bad dark:bg-bad-darkbg dark:text-bad-dark'
              }`}
            >
              <span className="font-mono text-sm">{item.code}</span>
              <span className="font-bold">{item.message}</span>
            </li>
          ))}
        </ul>
      )}

      {scanning && b.remaining_cartons === 0 && (
        <button
          type="button"
          className="btn-success w-full"
          disabled={complete.isPending}
          onClick={() => complete.mutate()}
        >
          {t('batches.all_scanned')}
        </button>
      )}

      {b.status === 'complete' && (
        <>
          <Banner tone="ok" title={t('batches.complete')}>
            {b.assigned_cartons} cartons verified present.
          </Banner>
          <button
            type="button"
            className="btn-primary w-full"
            disabled={release.isPending}
            onClick={() => release.mutate()}
          >
            {t('batches.release')}
          </button>
        </>
      )}

      {b.status === 'released' && (
        <Banner tone="ok" title={`Released by ${b.released_by_name}`}>
          {t('batches.invoices_closed')}
        </Banner>
      )}

      <Card title={t('batches.cartons_in_batch')} subtitle={scanning ? t('batches.scan_each') : undefined}>
        <ul className="divide-y divide-slate-200 dark:divide-slate-800">
          {b.cartons.map((carton) => (
            <li
              key={carton.invoice_id}
              className="flex items-center justify-between gap-3 py-3"
            >
              <div className="min-w-0">
                <p className="font-bold">{carton.invoice_number}</p>
                <p className="truncate text-sm text-slate-500 dark:text-slate-400">
                  {carton.customer_name} · packed by {carton.packed_by_name}
                </p>
              </div>
              {carton.out_scanned_at ? (
                <span className="chip bg-ok-bg text-ok dark:bg-ok-darkbg dark:text-ok-dark">
                  scanned
                </span>
              ) : scanning ? (
                <button
                  type="button"
                  className="btn-primary shrink-0"
                  disabled={busy}
                  onClick={() => void submit(carton.invoice_number)}
                >
                  {t('batches.mark_scanned')}
                </button>
              ) : (
                <span className="chip bg-warn-bg text-warn dark:bg-warn-darkbg dark:text-warn-dark">
                  waiting
                </span>
              )}
            </li>
          ))}
        </ul>
      </Card>
    </div>
  )
}
