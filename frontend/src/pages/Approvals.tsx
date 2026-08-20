import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, get, post } from '@/lib/api'
import { useErrorText } from '@/hooks/useErrorText'
import { Banner, Card, EmptyState, Spinner } from '@/components/ui'
import type { ExitDecisionResult, GateEntry, LoadApproval, Pickup } from '@/types'

function waitedFor(seconds: number | null | undefined): string {
  if (!seconds || seconds < 0) return 'just now'
  const minutes = Math.floor(seconds / 60)
  if (minutes < 1) return 'under a minute'
  if (minutes < 60) return `${minutes} min`
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

/**
 * PRD §5.8 — the Ops approval queue. CONTROL POINT 1.
 *
 * Oldest first, because the truck that has waited longest is the one blocking a
 * yard. The wait time is shown in words rather than a timestamp: "22 min" is
 * actionable, "10:14" requires arithmetic.
 */
export function ApprovalsPage() {
  const { t } = useTranslation()
  const errorText = useErrorText()
  const queryClient = useQueryClient()
  const [rejecting, setRejecting] = useState<string | null>(null)
  const [note, setNote] = useState('')
  const [error, setError] = useState<ApiError | null>(null)

  const pending = useQuery({
    queryKey: ['pending-approvals'],
    queryFn: () => get<GateEntry[]>('/gate/entries/pending'),
    // A guard is standing at a gate waiting on this. Polling is cheap; a stale
    // queue costs a truck ten minutes.
    refetchInterval: 15_000,
  })

  const decide = useMutation({
    mutationFn: ({ id, approve }: { id: string; approve: boolean }) =>
      post<GateEntry>(`/gate/entries/${id}/decision`, {
        approve,
        note: approve ? note || null : note,
      }),
    onSuccess: () => {
      setRejecting(null)
      setNote('')
      setError(null)
      void queryClient.invalidateQueries({ queryKey: ['pending-approvals'] })
      void queryClient.invalidateQueries({ queryKey: ['dashboard'] })
    },
    onError: (err) => setError(err as ApiError),
  })

  if (pending.isLoading) return <Spinner label={t('approvals.loading')} />

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-black">{t('approvals.title')}</h1>

      {error && (
        <Banner tone="bad" title={errorText(error).title}>
          {error.hint}
        </Banner>
      )}

      {pending.data?.length === 0 && (
        <EmptyState title={t('approvals.none')} hint={t('approvals.none_hint')} />
      )}

      <CartonCountApprovals />
      <ExitApprovals />

      {pending.data?.map((entry) => {
        const driver = entry.persons.find((p) => p.visitor_role === 'driver')
        const waited = entry.requested_at
          ? (Date.now() - new Date(entry.requested_at).getTime()) / 1000
          : 0

        return (
          <Card
            key={entry.id}
            title={entry.vehicle_number}
            subtitle={`${entry.entry_code} · ${entry.vendor_name}`}
            action={
              <span
                className={`chip ${
                  entry.sla_breached
                    ? 'bg-bad-bg text-bad dark:bg-bad-darkbg dark:text-bad-dark'
                    : waited > 900
                      ? 'bg-warn-bg text-warn dark:bg-warn-darkbg dark:text-warn-dark'
                      : 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300'
                }`}
              >
                waiting {waitedFor(waited)}
              </span>
            }
          >
            {entry.sla_breached && (
              <div className="mb-4">
                <Banner tone="bad" title={t('approvals.sla_breached')}>
                  {t('approvals.gate_locked')}
                </Banner>
              </div>
            )}

            <dl className="mb-4 grid grid-cols-1 gap-3 text-base sm:grid-cols-2">
              <div>
                <dt className="text-slate-500 dark:text-slate-400">{t('person.driver')}</dt>
                <dd className="font-bold">{driver?.full_name ?? '—'}</dd>
                <dd className="font-mono text-sm text-slate-500">{driver?.mobile}</dd>
              </div>
              <div>
                <dt className="text-slate-500 dark:text-slate-400">PO</dt>
                <dd className="font-bold">{entry.po_number ?? 'Not linked'}</dd>
              </div>
              <div>
                <dt className="text-slate-500 dark:text-slate-400">{t('approvals.people_on_board')}</dt>
                <dd className="font-bold">{entry.persons.length}</dd>
              </div>
              <div>
                <dt className="text-slate-500 dark:text-slate-400">{t('approvals.registered_by')}</dt>
                <dd className="font-bold">{entry.requested_by_name}</dd>
              </div>
            </dl>

            <ul className="mb-4 space-y-1 text-base">
              {entry.persons.map((person) => (
                <li key={person.visitor_id} className="flex items-center gap-2">
                  <span className="capitalize text-slate-500 dark:text-slate-400">
                    {person.visitor_role}:
                  </span>
                  <span className="font-semibold">{person.full_name}</span>
                  {person.has_id_photo ? (
                    <span className="chip bg-ok-bg text-ok dark:bg-ok-darkbg dark:text-ok-dark">
                      {t('approvals.id_on_file')}
                    </span>
                  ) : (
                    <span className="chip bg-warn-bg text-warn dark:bg-warn-darkbg dark:text-warn-dark">
                      {t('approvals.no_photo')}
                    </span>
                  )}
                </li>
              ))}
            </ul>

            {rejecting === entry.id ? (
              <div className="space-y-3">
                <textarea
                  className="input"
                  rows={3}
                  value={note}
                  onChange={(event) => setNote(event.target.value)}
                  placeholder={t('approvals.reject_reason')}
                />
                <div className="flex gap-3">
                  <button
                    type="button"
                    className="btn-ghost flex-1"
                    onClick={() => {
                      setRejecting(null)
                      setNote('')
                    }}
                  >
                    {t('common.cancel')}
                  </button>
                  <button
                    type="button"
                    className="btn-danger flex-1"
                    disabled={note.trim().length < 3 || decide.isPending}
                    onClick={() => decide.mutate({ id: entry.id, approve: false })}
                  >
                    {t('approvals.confirm_reject')}
                  </button>
                </div>
              </div>
            ) : (
              <div className="flex gap-3">
                <button
                  type="button"
                  className="btn-ghost flex-1"
                  onClick={() => {
                    setRejecting(entry.id)
                    setNote('')
                  }}
                >
                  {t('approvals.reject')}
                </button>
                <button
                  type="button"
                  className="btn-success flex-1"
                  disabled={decide.isPending}
                  onClick={() => decide.mutate({ id: entry.id, approve: true })}
                >
                  {t('approvals.approve')}
                </button>
              </div>
            )}
          </Card>
        )
      })}
    </div>
  )
}


/**
 * Carton counts filed by a guard, waiting on a decision.
 *
 * The mismatch is the whole point of the screen, so it is stated in words rather
 * than left for the reader to spot by comparing two numbers. Approving a count
 * that does not match is allowed — short supply happens and Ops is entitled to
 * accept it — but it should never happen by accident.
 */
function CartonCountApprovals() {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [rejecting, setRejecting] = useState<string | null>(null)
  const [note, setNote] = useState('')

  const pending = useQuery({
    queryKey: ['loading', 'pending'],
    queryFn: () => get<LoadApproval[]>('/loading/pending'),
    refetchInterval: 15_000,
  })

  const decide = useMutation({
    mutationFn: ({ batchId, approve }: { batchId: string; approve: boolean }) =>
      post<LoadApproval>(`/loading/batches/${batchId}/decision`, {
        approve,
        note: approve ? note || null : note,
      }),
    onSuccess: () => {
      setRejecting(null)
      setNote('')
      void queryClient.invalidateQueries({ queryKey: ['loading'] })
      void queryClient.invalidateQueries({ queryKey: ['batches'] })
    },
  })

  if (!pending.data?.length) return null

  return (
    <>
      <h2 className="pt-2 text-xl font-black">{t('loading.pending_title')}</h2>

      {pending.data.map((approval) => (
        <Card
          key={approval.id}
          title={approval.batch_code}
          subtitle={t('loading.counted_by', { name: approval.counted_by_name })}
          action={
            <span
              className={`chip ${
                approval.matches
                  ? 'bg-ok-bg text-ok dark:bg-ok-darkbg dark:text-ok-dark'
                  : 'bg-bad-bg text-bad dark:bg-bad-darkbg dark:text-bad-dark'
              }`}
            >
              {approval.matches ? t('loading.count_matches') : t('loading.count_mismatch')}
            </span>
          }
        >
          <dl className="mb-4 grid grid-cols-2 gap-3 text-base">
            <div>
              <dt className="text-slate-600 dark:text-slate-400">{t('loading.you_counted')}</dt>
              <dd className="text-4xl font-black tabular-nums">{approval.counted_cartons}</dd>
            </div>
            <div>
              <dt className="text-slate-600 dark:text-slate-400">
                {t('loading.system_expects')}
              </dt>
              <dd className="text-4xl font-black tabular-nums">{approval.expected_cartons}</dd>
            </div>
          </dl>

          {rejecting === approval.batch_id ? (
            <div className="space-y-3">
              <textarea
                className="input"
                rows={3}
                value={note}
                onChange={(event) => setNote(event.target.value)}
                placeholder={t('loading.reject_reason')}
              />
              <div className="flex gap-3">
                <button
                  type="button"
                  className="btn-ghost flex-1"
                  onClick={() => {
                    setRejecting(null)
                    setNote('')
                  }}
                >
                  {t('common.cancel')}
                </button>
                <button
                  type="button"
                  className="btn-danger flex-1"
                  disabled={note.trim().length < 3 || decide.isPending}
                  onClick={() => decide.mutate({ batchId: approval.batch_id, approve: false })}
                >
                  {t('loading.reject_count')}
                </button>
              </div>
            </div>
          ) : (
            <div className="flex gap-3">
              <button
                type="button"
                className="btn-ghost flex-1"
                onClick={() => {
                  setRejecting(approval.batch_id)
                  setNote('')
                }}
              >
                {t('loading.reject_count')}
              </button>
              <button
                type="button"
                className="btn-success flex-1"
                disabled={decide.isPending}
                onClick={() => decide.mutate({ batchId: approval.batch_id, approve: true })}
              >
                {t('loading.approve_count')}
              </button>
            </div>
          )}
        </Card>
      ))}
    </>
  )
}

/**
 * Loaded vehicles waiting for permission to leave.
 *
 * Approving does not open the gate — the guard still performs the release, so the
 * gate opening stays attached to the person standing at it. This screen only
 * records that Ops is content for it to happen.
 */
function ExitApprovals() {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [holding, setHolding] = useState<string | null>(null)
  const [note, setNote] = useState('')

  const waiting = useQuery({
    queryKey: ['pickups', 'awaiting-exit'],
    queryFn: () => get<Pickup[]>('/pickups/awaiting-exit'),
    // A truck at a gate with the engine running is the most expensive thing in
    // this system to keep waiting, so this polls faster than the rest.
    refetchInterval: 10_000,
  })

  const decide = useMutation({
    mutationFn: ({ pickupId, approve }: { pickupId: string; approve: boolean }) =>
      post<ExitDecisionResult>(`/pickups/${pickupId}/exit-decision`, {
        approve,
        note: approve ? null : note,
      }),
    onSuccess: () => {
      setHolding(null)
      setNote('')
      void queryClient.invalidateQueries({ queryKey: ['pickups'] })
    },
  })

  if (!waiting.data?.length) return null

  return (
    <>
      <h2 className="pt-2 text-xl font-black">{t('exitapproval.title')}</h2>

      {waiting.data.map((pickup) => (
        <Card
          key={pickup.pickup_id}
          title={pickup.vehicle_number}
          subtitle={`${pickup.pickup_code} · ${pickup.batch_code}`}
          action={
            <span className="chip bg-warn-bg text-warn dark:bg-warn-darkbg dark:text-warn-dark">
              {waitedFor(pickup.exit_waiting_seconds ?? 0)}
            </span>
          }
        >
          <div className="mb-4">
            <Banner
              tone="ok"
              title={t('exitapproval.cartons_loaded', { count: pickup.verified_cartons })}
            >
              {pickup.verified_by_name}
            </Banner>
          </div>

          {holding === pickup.pickup_id ? (
            <div className="space-y-3">
              <textarea
                className="input"
                rows={3}
                value={note}
                onChange={(event) => setNote(event.target.value)}
                placeholder={t('exitapproval.hold_reason')}
              />
              <div className="flex gap-3">
                <button
                  type="button"
                  className="btn-ghost flex-1"
                  onClick={() => {
                    setHolding(null)
                    setNote('')
                  }}
                >
                  {t('common.cancel')}
                </button>
                <button
                  type="button"
                  className="btn-danger flex-1"
                  disabled={note.trim().length < 3 || decide.isPending}
                  onClick={() => decide.mutate({ pickupId: pickup.pickup_id, approve: false })}
                >
                  {t('exitapproval.hold')}
                </button>
              </div>
            </div>
          ) : (
            <div className="flex gap-3">
              <button
                type="button"
                className="btn-ghost flex-1"
                onClick={() => {
                  setHolding(pickup.pickup_id)
                  setNote('')
                }}
              >
                {t('exitapproval.hold')}
              </button>
              <button
                type="button"
                className="btn-success flex-1"
                disabled={decide.isPending}
                onClick={() => decide.mutate({ pickupId: pickup.pickup_id, approve: true })}
              >
                {t('exitapproval.approve')}
              </button>
            </div>
          )}
        </Card>
      ))}
    </>
  )
}
