import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, get, post } from '@/lib/api'
import { useErrorText } from '@/hooks/useErrorText'
import { Banner, Card, EmptyState, Field, Spinner } from '@/components/ui'
import type { BatchAwaitingCount, LoadApproval } from '@/types'

/**
 * The guard's carton count on a finished batch.
 *
 * The outbound mirror of the gate: someone physically counts what is on the bay,
 * and Ops decides. Nothing is released for loading until that decision exists.
 *
 * Two deliberate choices in the layout. The system's expected number is shown
 * *after* the guard has typed theirs, not before — a count that starts by
 * telling you the answer is not a count, it is a confirmation. And a mismatch is
 * not treated as an error here: the guard's job is to report what is there, and
 * deciding what a discrepancy means is Ops's.
 */
export function LoadingPage() {
  const { t } = useTranslation()
  const errorText = useErrorText()
  const queryClient = useQueryClient()

  const [counts, setCounts] = useState<Record<string, string>>({})
  const [error, setError] = useState<ApiError | null>(null)

  const awaiting = useQuery({
    queryKey: ['loading', 'awaiting-count'],
    queryFn: () => get<BatchAwaitingCount[]>('/loading/awaiting-count'),
    refetchInterval: 20_000,
  })

  const pending = useQuery({
    queryKey: ['loading', 'pending'],
    queryFn: () => get<LoadApproval[]>('/loading/pending'),
    refetchInterval: 20_000,
  })

  const fileCount = useMutation({
    mutationFn: ({ batchId, counted }: { batchId: string; counted: number }) =>
      post<LoadApproval>(`/loading/batches/${batchId}/count`, {
        counted_cartons: counted,
      }),
    onSuccess: () => {
      setError(null)
      setCounts({})
      void queryClient.invalidateQueries({ queryKey: ['loading'] })
    },
    onError: (err) => setError(err as ApiError),
  })

  if (awaiting.isLoading) return <Spinner />

  const toCount = awaiting.data ?? []
  const filed = pending.data ?? []

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-black">{t('loading.title')}</h1>
        <p className="mt-1 text-base text-slate-600 dark:text-slate-400">
          {t('loading.subtitle')}
        </p>
      </div>

      {error && (
        <Banner tone={error.isControlPoint ? 'bad' : 'warn'} title={errorText(error).title}>
          {error.hint}
        </Banner>
      )}

      {toCount.length === 0 && filed.length === 0 && (
        <EmptyState title={t('loading.awaiting_none')} hint={t('loading.awaiting_none_hint')} />
      )}

      {toCount.map((batch) => {
        const typed = counts[batch.batch_id] ?? ''
        const value = Number(typed)
        const valid = typed !== '' && Number.isInteger(value) && value >= 0

        return (
          <Card key={batch.batch_id} title={batch.batch_code}>
            <Field label={t('loading.count_label')} required>
              <input
                className="input text-3xl font-black tabular-nums"
                inputMode="numeric"
                value={typed}
                onChange={(event) =>
                  setCounts((c) => ({
                    ...c,
                    [batch.batch_id]: event.target.value.replace(/\D/g, ''),
                  }))
                }
              />
            </Field>

            {/* Only once they have committed to a number. Showing the expected
                count first would make this a confirmation rather than a count. */}
            {valid && (
              <div className="mb-4">
                <Banner
                  tone={value === batch.carton_count ? 'ok' : 'warn'}
                  title={
                    value === batch.carton_count
                      ? t('loading.count_matches')
                      : t('loading.count_mismatch')
                  }
                >
                  {t('loading.system_expects')}: {batch.carton_count}
                </Banner>
              </div>
            )}

            <button
              type="button"
              className="btn-primary w-full"
              disabled={!valid || fileCount.isPending}
              onClick={() => fileCount.mutate({ batchId: batch.batch_id, counted: value })}
            >
              {t('loading.submit_count')}
            </button>
          </Card>
        )
      })}

      {filed.map((approval) => (
        <Card
          key={approval.id}
          title={approval.batch_code}
          subtitle={t('loading.counted_by', { name: approval.counted_by_name })}
        >
          <Banner
            tone={approval.matches ? 'info' : 'warn'}
            title={t('loading.counted_waiting', { counted: approval.counted_cartons })}
          >
            {t('loading.system_expects')}: {approval.expected_cartons}
          </Banner>
        </Card>
      ))}
    </div>
  )
}
