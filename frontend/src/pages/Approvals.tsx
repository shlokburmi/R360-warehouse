import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, get, post } from '@/lib/api'
import { Banner, Card, EmptyState, Spinner } from '@/components/ui'
import type { GateEntry } from '@/types'

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

  if (pending.isLoading) return <Spinner label="Loading approvals…" />

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-black">Pending Approvals</h1>

      {error && (
        <Banner tone="bad" title={error.message}>
          {error.hint}
        </Banner>
      )}

      {pending.data?.length === 0 && (
        <EmptyState title="Nothing waiting" hint="Every truck at the gate has been decided." />
      )}

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
                <Banner tone="bad" title="SLA breached — escalated to Admin">
                  The gate is still locked. Nothing has been auto-approved.
                </Banner>
              </div>
            )}

            <dl className="mb-4 grid grid-cols-2 gap-3 text-base">
              <div>
                <dt className="text-slate-500 dark:text-slate-400">Driver</dt>
                <dd className="font-bold">{driver?.full_name ?? '—'}</dd>
                <dd className="font-mono text-sm text-slate-500">{driver?.mobile}</dd>
              </div>
              <div>
                <dt className="text-slate-500 dark:text-slate-400">PO</dt>
                <dd className="font-bold">{entry.po_number ?? 'Not linked'}</dd>
              </div>
              <div>
                <dt className="text-slate-500 dark:text-slate-400">People on board</dt>
                <dd className="font-bold">{entry.persons.length}</dd>
              </div>
              <div>
                <dt className="text-slate-500 dark:text-slate-400">Registered by</dt>
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
                      ID on file
                    </span>
                  ) : (
                    <span className="chip bg-warn-bg text-warn dark:bg-warn-darkbg dark:text-warn-dark">
                      No photo
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
                  placeholder="Why is this being rejected? (required)"
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
                    Cancel
                  </button>
                  <button
                    type="button"
                    className="btn-danger flex-1"
                    disabled={note.trim().length < 3 || decide.isPending}
                    onClick={() => decide.mutate({ id: entry.id, approve: false })}
                  >
                    Confirm reject
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
                  Reject
                </button>
                <button
                  type="button"
                  className="btn-success flex-1"
                  disabled={decide.isPending}
                  onClick={() => decide.mutate({ id: entry.id, approve: true })}
                >
                  Approve
                </button>
              </div>
            )}
          </Card>
        )
      })}
    </div>
  )
}
