import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, get, post } from '@/lib/api'
import { useErrorText } from '@/hooks/useErrorText'
import { useAuth } from '@/hooks/useAuth'
import { Banner, Card, EmptyState, Spinner, StatusChip } from '@/components/ui'
import type { WarehouseException } from '@/types'

/** /resolve answers with one extra field: what the decision did to the truck. */
type ResolveResult = WarehouseException & { outcome?: string | null }

/**
 * PRD §5.9 — Exception management.
 *
 * A held box has exactly three ways out (DECISIONS.md §3) and every one of them
 * requires a note. There is no "dismiss", because the point of the exception is
 * that a named person decided what happened to the goods.
 */
const BOX_RESOLUTIONS = [
  {
    value: 'accept_short',
    label: 'Accept short',
    tone: 'warn' as const,
    help: 'The shortfall is real and agreed. Scanned units enter stock; the gap is logged against the vendor.',
  },
  {
    value: 'recount',
    label: 'Recount',
    tone: 'info' as const,
    help: 'Suspected scan error. The box reopens; previous scans stay in the ledger as evidence.',
  },
  {
    value: 'reject_box',
    label: 'Reject box',
    tone: 'bad' as const,
    help: 'Refuse the box entirely. Nothing enters; the full quantity is logged as rejected.',
  },
]

const GENERAL_RESOLUTIONS = [
  {
    value: 'accept',
    label: 'Approve & proceed',
    tone: 'ok' as const,
    help: 'The discrepancy is accepted as it stands, and the step that raised it can continue. A control point cannot be approved past.',
  },
  {
    value: 'reject',
    label: 'Reject & return',
    tone: 'bad' as const,
    help: 'The goods go back with the vehicle. The gate entry is cancelled, which cannot be undone.',
  },
]

export function ExceptionsPage() {
  const { t } = useTranslation()
  const errorText = useErrorText()
  const queryClient = useQueryClient()
  const { me } = useAuth()
  const [showResolved, setShowResolved] = useState(false)
  const [active, setActive] = useState<string | null>(null)
  const [resolution, setResolution] = useState<string>('')
  const [note, setNote] = useState('')
  const [error, setError] = useState<ApiError | null>(null)
  const [done, setDone] = useState<{
    code: string
    resolution: string
    outcome?: string | null
  } | null>(null)

  const isOps = me?.role === 'admin' || me?.role === 'ops_manager'

  const exceptions = useQuery({
    queryKey: ['exceptions', showResolved],
    queryFn: () =>
      get<WarehouseException[]>(
        showResolved ? '/exceptions' : '/exceptions?status=open&status=escalated',
      ),
    refetchInterval: 20_000,
  })

  const reset = () => {
    setActive(null)
    setResolution('')
    setNote('')
  }

  const resolve = useMutation({
    mutationFn: (id: string) =>
      post<ResolveResult>(`/exceptions/${id}/resolve`, { resolution, note }),
    onSuccess: (result) => {
      setError(null)
      // The card is about to leave the open list, so the confirmation has to
      // outlive it: "resolved" on its own does not tell anyone whether the
      // goods can move, and this page is where the decision was made.
      setDone({
        code: result.exception_code,
        resolution: result.resolution ?? resolution,
        outcome: result.outcome,
      })
      reset()
      window.scrollTo({ top: 0, behavior: 'smooth' })
      void queryClient.invalidateQueries({ queryKey: ['exceptions'] })
      void queryClient.invalidateQueries({ queryKey: ['boxes'] })
      void queryClient.invalidateQueries({ queryKey: ['entries'] })
      void queryClient.invalidateQueries({ queryKey: ['entry'] })
      void queryClient.invalidateQueries({ queryKey: ['dashboard'] })
    },
    onError: (err) => setError(err as ApiError),
  })

  const escalate = useMutation({
    mutationFn: (id: string) =>
      post(`/exceptions/${id}/escalate`, { email_superadmin: true, note: note || null }),
    onSuccess: (_data, id) => {
      setError(null)
      const escalated = exceptions.data?.find((e) => e.id === id)
      setDone({
        code: escalated?.exception_code ?? '',
        resolution: 'escalated',
        outcome: 'Emailed to the superadmin. The goods stay held until someone decides.',
      })
      reset()
      window.scrollTo({ top: 0, behavior: 'smooth' })
      void queryClient.invalidateQueries({ queryKey: ['exceptions'] })
    },
    onError: (err) => setError(err as ApiError),
  })

  if (exceptions.isLoading) return <Spinner label={t('exceptions.loading')} />

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-black">{t('exceptions.title')}</h1>
        <button
          type="button"
          className="btn-ghost"
          onClick={() => setShowResolved((current) => !current)}
        >
          {showResolved ? 'Open only' : 'Show all'}
        </button>
      </div>

      {done && (
        <Banner
          tone={done.resolution === 'reject' || done.resolution === 'reject_box' ? 'warn' : 'ok'}
          title={
            done.code
              ? `${done.code} — ${done.resolution.replace(/_/g, ' ')}`
              : 'Decision recorded'
          }
        >
          <span className="flex flex-wrap items-center gap-3">
            <span>{done.outcome ?? 'The decision is on the record against the vendor and PO.'}</span>
            <button type="button" className="underline" onClick={() => setDone(null)}>
              {t('common.done')}
            </button>
          </span>
        </Banner>
      )}

      {/* Only when no card is mid-decision: an open panel shows its own copy,
          because a banner up here is off the screen the operator is looking at
          and a refusal that nobody sees looks exactly like a dead button. */}
      {error && active === null && (
        <Banner tone="bad" title={errorText(error).title}>
          {error.hint}
        </Banner>
      )}

      {exceptions.data?.length === 0 && (
        <EmptyState title={t('exceptions.none')} hint={t('exceptions.none_hint')} />
      )}

      {exceptions.data?.map((exception) => {
        const isBoxException = exception.box_id !== null
        const options = isBoxException ? BOX_RESOLUTIONS : GENERAL_RESOLUTIONS
        const isActive = active === exception.id
        const chosen = options.find((option) => option.value === resolution)

        return (
          <Card
            key={exception.id}
            title={exception.title}
            subtitle={`${exception.exception_code} · ${exception.vendor_name ?? 'No vendor'} · ${
              exception.po_number ?? 'No PO'
            }`}
            action={<StatusChip status={exception.status} />}
          >
            <dl className="mb-4 grid grid-cols-1 gap-3 text-base sm:grid-cols-2">
              <div>
                <dt className="text-slate-500 dark:text-slate-400">{t('exceptions.reported_by')}</dt>
                <dd className="font-bold">{exception.reported_by_name}</dd>
              </div>
              <div>
                <dt className="text-slate-500 dark:text-slate-400">{t('exceptions.when')}</dt>
                <dd className="font-bold">
                  {new Date(exception.reported_at).toLocaleString()}
                </dd>
              </div>
              {exception.entry_code && (
                <div>
                  <dt className="text-slate-500 dark:text-slate-400">{t('exceptions.gate_entry')}</dt>
                  <dd className="font-bold">{exception.entry_code}</dd>
                </div>
              )}
              {exception.box_number !== null && (
                <div>
                  <dt className="text-slate-500 dark:text-slate-400">Box</dt>
                  <dd className="font-bold">#{exception.box_number}</dd>
                </div>
              )}
            </dl>

            {Object.keys(exception.details).length > 0 && (
              <dl className="mb-4 rounded-xl bg-slate-100 p-3 text-base dark:bg-slate-800">
                {Object.entries(exception.details).map(([key, value]) => (
                  <div key={key} className="flex flex-wrap justify-between gap-3 py-0.5">
                    <dt className="text-slate-500 dark:text-slate-400">
                      {key.replace(/_/g, ' ')}
                    </dt>
                    <dd className="font-mono font-bold">{String(value)}</dd>
                  </div>
                ))}
              </dl>
            )}

            {exception.status === 'resolved' ? (
              <Banner
                tone="ok"
                title={`${exception.resolution?.replace(/_/g, ' ')} by ${exception.resolved_by_name}`}
              >
                {exception.resolution_note}
              </Banner>
            ) : !isOps ? (
              <p className="text-base text-slate-500 dark:text-slate-400">
                {t('exceptions.awaiting_ops')}
              </p>
            ) : isActive ? (
              <div className="space-y-3">
                <div className="grid gap-2">
                  {options.map((option) => (
                    <button
                      key={option.value}
                      type="button"
                      onClick={() => setResolution(option.value)}
                      className={`rounded-xl border-2 p-3 text-left ${
                        resolution === option.value
                          ? 'border-blue-600 bg-blue-50 dark:bg-blue-950'
                          : 'border-slate-300 dark:border-slate-700'
                      }`}
                    >
                      <p className="text-lg font-bold">{option.label}</p>
                      {option.help && (
                        <p className="mt-0.5 text-sm text-slate-500 dark:text-slate-400">
                          {option.help}
                        </p>
                      )}
                    </button>
                  ))}
                </div>

                <textarea
                  className="input"
                  rows={3}
                  value={note}
                  onChange={(event) => setNote(event.target.value)}
                  placeholder={t('exceptions.decision_note')}
                />

                {error && (
                  <Banner tone="bad" title={errorText(error).title}>
                    {error.hint}
                  </Banner>
                )}

                {/* Says which of the two requirements is still missing. The
                    button styles disabled as 40% opacity, which on a bright
                    warehouse floor is not a difference anyone reads — so a tap
                    on it looked like the app doing nothing. */}
                {!chosen ? (
                  <p className="text-base text-slate-500 dark:text-slate-400">
                    Choose an outcome above.
                  </p>
                ) : note.trim().length < 3 ? (
                  <p className="text-base text-slate-500 dark:text-slate-400">
                    Say what was decided and why — this is what the audit trail keeps.
                  </p>
                ) : null}

                <div className="flex gap-3">
                  <button
                    type="button"
                    className="btn-ghost flex-1"
                    disabled={resolve.isPending || escalate.isPending}
                    onClick={() => {
                      setError(null)
                      reset()
                    }}
                  >
                    {t('common.cancel')}
                  </button>
                  <button
                    type="button"
                    className="btn-primary flex-1"
                    disabled={!chosen || note.trim().length < 3 || resolve.isPending}
                    onClick={() => {
                      setError(null)
                      setDone(null)
                      resolve.mutate(exception.id)
                    }}
                  >
                    {resolve.isPending ? 'Confirming…' : t('common.confirm')}
                  </button>
                </div>

                <button
                  type="button"
                  className="btn-ghost w-full"
                  disabled={escalate.isPending || resolve.isPending}
                  onClick={() => {
                    setError(null)
                    setDone(null)
                    escalate.mutate(exception.id)
                  }}
                >
                  {escalate.isPending ? 'Sending…' : t('exceptions.email_superadmin')}
                </button>
              </div>
            ) : (
              <button
                type="button"
                className="btn-primary w-full"
                onClick={() => {
                  setActive(exception.id)
                  setResolution('')
                  setNote('')
                  setError(null)
                }}
              >
                {t('exceptions.decide')}
              </button>
            )}
          </Card>
        )
      })}
    </div>
  )
}
