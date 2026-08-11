import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, get, post } from '@/lib/api'
import { Scanner } from '@/components/Scanner'
import { Banner, Card, EmptyState, Field, Spinner, StatusChip } from '@/components/ui'
import type { BoxPutawayStatus, Location, PutawayResult, PutawayTask } from '@/types'

/**
 * PRD Step 6 — Segregation and putaway (Phase 2).
 *
 * The screen answers one question at a time: which box, then which rack, then
 * how many. Rack labels are scanned rather than typed wherever possible, because
 * `A-01-04-02-03` is five fields of opportunity to fat-finger a digit and lose a
 * carton for a fortnight.
 *
 * Damaged units are kept visually separate throughout, and the server refuses to
 * put them anywhere but a quarantine rack.
 */
export function PutawayPage() {
  const queryClient = useQueryClient()

  const [activeBoxId, setActiveBoxId] = useState<string | null>(null)
  const [locationCode, setLocationCode] = useState('')
  const [resolved, setResolved] = useState<Location | null>(null)
  const [units, setUnits] = useState('')
  const [disposition, setDisposition] = useState<'stock' | 'quarantine'>('stock')
  const [error, setError] = useState<ApiError | null>(null)
  const [result, setResult] = useState<PutawayResult | null>(null)

  const queue = useQuery({
    queryKey: ['putaway-queue'],
    queryFn: () => get<PutawayTask[]>('/putaway/queue'),
    refetchInterval: 20_000,
  })

  const boxStatus = useQuery({
    queryKey: ['box-putaway', activeBoxId],
    queryFn: () => get<BoxPutawayStatus>(`/boxes/${activeBoxId}/putaway`),
    enabled: Boolean(activeBoxId),
  })

  function reset() {
    setLocationCode('')
    setResolved(null)
    setUnits('')
    setError(null)
  }

  async function lookupLocation(code: string) {
    setError(null)
    setResolved(null)
    try {
      const location = await get<Location>(
        `/locations/resolve?code=${encodeURIComponent(code)}`,
      )
      setResolved(location)
      setLocationCode(location.code)

      // The rack itself tells us what kind of goods it holds, so the operator
      // never has to remember which zone is quarantine.
      setDisposition(location.is_quarantine ? 'quarantine' : 'stock')

      // Default to everything that is left for that disposition — the common
      // case is "this whole box goes here", and typing the number again is
      // wasted effort that also invites a typo.
      const remaining = location.is_quarantine
        ? boxStatus.data?.quarantine_remaining
        : boxStatus.data?.stock_remaining
      if (remaining) setUnits(String(remaining))
    } catch (err) {
      setError(err as ApiError)
    }
  }

  const place = useMutation({
    mutationFn: () =>
      post<PutawayResult>(`/boxes/${activeBoxId}/putaway`, {
        location_code: locationCode,
        units: Number(units),
        disposition,
      }),
    onSuccess: (data) => {
      setResult(data)
      reset()
      void queryClient.invalidateQueries({ queryKey: ['putaway-queue'] })
      void queryClient.invalidateQueries({ queryKey: ['box-putaway', activeBoxId] })
      void queryClient.invalidateQueries({ queryKey: ['stock'] })
      if (data.complete) setActiveBoxId(null)
    },
    onError: (err) => setError(err as ApiError),
  })

  if (queue.isLoading) return <Spinner label="Loading putaway list…" />

  const active = boxStatus.data
  const remaining = resolved?.is_quarantine
    ? (active?.quarantine_remaining ?? 0)
    : (active?.stock_remaining ?? 0)

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-black">Putaway</h1>

      {error && (
        <Banner tone={error.isControlPoint ? 'bad' : 'warn'} title={error.message}>
          {error.hint}
        </Banner>
      )}

      {result && (
        <Banner tone="ok" title={result.message}>
          {result.complete && 'Move the empty carton to the outside rack.'}
        </Banner>
      )}

      {!activeBoxId ? (
        <>
          {queue.data?.length === 0 ? (
            <EmptyState
              title="Nothing to put away"
              hint="Boxes appear here once the inbound team has verified their counts."
            />
          ) : (
            queue.data?.map((task) => (
              <Card
                key={task.box_id}
                title={`Box ${task.box_number} · ${task.sku ?? ''}`}
                subtitle={`${task.entry_code} · ${task.vendor_name} · ${task.vehicle_number}`}
                action={<StatusChip status={task.box_status} />}
              >
                <p className="mb-3 text-base">
                  {task.description}
                </p>
                <div className="mb-4 flex gap-4 text-base">
                  {task.stock_remaining > 0 && (
                    <span className="font-bold">{task.stock_remaining} to shelve</span>
                  )}
                  {task.quarantine_remaining > 0 && (
                    <span className="font-bold text-bad dark:text-bad-dark">
                      {task.quarantine_remaining} damaged → quarantine
                    </span>
                  )}
                </div>
                <button
                  type="button"
                  className="btn-primary w-full"
                  onClick={() => {
                    setActiveBoxId(task.box_id)
                    setResult(null)
                    reset()
                  }}
                >
                  Put this box away
                </button>
              </Card>
            ))
          )}
        </>
      ) : (
        <>
          <Card
            title={`Box ${active?.box_number ?? ''} · ${active?.sku ?? ''}`}
            subtitle={active?.entry_code}
            action={
              <button
                type="button"
                className="text-base font-semibold text-slate-500"
                onClick={() => {
                  setActiveBoxId(null)
                  reset()
                }}
              >
                Back
              </button>
            }
          >
            <dl className="grid grid-cols-2 gap-3 text-base sm:grid-cols-4">
              <div>
                <dt className="text-slate-500 dark:text-slate-400">Good units</dt>
                <dd className="text-2xl font-black tabular-nums">
                  {active?.stock_remaining ?? 0}
                </dd>
              </div>
              <div>
                <dt className="text-slate-500 dark:text-slate-400">Damaged</dt>
                <dd className="text-2xl font-black tabular-nums text-bad dark:text-bad-dark">
                  {active?.quarantine_remaining ?? 0}
                </dd>
              </div>
              <div>
                <dt className="text-slate-500 dark:text-slate-400">Already placed</dt>
                <dd className="text-2xl font-black tabular-nums">
                  {(active?.stock_placed ?? 0) + (active?.quarantine_placed ?? 0)}
                </dd>
              </div>
              <div>
                <dt className="text-slate-500 dark:text-slate-400">Arrived</dt>
                <dd className="text-2xl font-black tabular-nums">
                  {active?.scanned_units ?? 0}
                </dd>
              </div>
            </dl>
          </Card>

          <Card title="Scan the rack label" subtitle="Or type the location code">
            <Scanner onScan={(code) => void lookupLocation(code)} />

            <form
              className="mt-3 flex gap-2"
              onSubmit={(event) => {
                event.preventDefault()
                void lookupLocation(locationCode)
              }}
            >
              <input
                className="input font-mono uppercase"
                placeholder="A-01-04-02-03"
                value={locationCode}
                onChange={(event) => setLocationCode(event.target.value.toUpperCase())}
                autoCapitalize="characters"
                autoCorrect="off"
                spellCheck={false}
                aria-label="Location code"
              />
              <button
                type="submit"
                className="btn-ghost"
                disabled={locationCode.trim().length < 3}
              >
                Find
              </button>
            </form>
          </Card>

          {resolved && (
            <Card
              title={resolved.code}
              subtitle={resolved.description ?? `Zone ${resolved.zone}`}
            >
              {resolved.is_quarantine ? (
                <div className="mb-4">
                  <Banner tone="warn" title="Quarantine location">
                    Only damaged units belong here.
                  </Banner>
                </div>
              ) : (
                <div className="mb-4">
                  <Banner tone="ok" title="Stock location">
                    Good units only — damaged units are refused here.
                  </Banner>
                </div>
              )}

              {remaining <= 0 ? (
                <Banner
                  tone="bad"
                  title={`No ${disposition} units left to place from this box`}
                >
                  {resolved.is_quarantine
                    ? 'This box has no damaged units.'
                    : 'This box has no good units left.'}
                </Banner>
              ) : (
                <>
                  <Field
                    label={`Units to place here (up to ${remaining})`}
                    required
                    hint="A box can be split across several racks if one bin is full."
                  >
                    <input
                      className="input text-center text-3xl font-black"
                      type="number"
                      inputMode="numeric"
                      min={1}
                      max={remaining}
                      value={units}
                      onChange={(event) => setUnits(event.target.value)}
                    />
                  </Field>

                  <button
                    type="button"
                    className="btn-success w-full"
                    disabled={
                      !units ||
                      Number(units) < 1 ||
                      Number(units) > remaining ||
                      place.isPending
                    }
                    onClick={() => place.mutate()}
                  >
                    {place.isPending
                      ? 'Recording…'
                      : `Place ${units || 0} unit(s) at ${resolved.code}`}
                  </button>
                </>
              )}
            </Card>
          )}

          <PutawayHistory boxId={activeBoxId} />
        </>
      )}
    </div>
  )
}

function PutawayHistory({ boxId }: { boxId: string }) {
  const history = useQuery({
    queryKey: ['putaway-history', boxId],
    queryFn: () =>
      get<
        {
          id: string
          location_code: string
          is_quarantine: boolean
          units: number
          disposition: string
          moved_at: string
          moved_by_name: string | null
        }[]
      >(`/boxes/${boxId}/putaway/history`),
  })

  if (!history.data || history.data.length === 0) return null

  return (
    <Card title="Already placed from this box">
      <ul className="divide-y divide-slate-200 dark:divide-slate-800">
        {history.data.map((row) => (
          <li key={row.id} className="flex items-center justify-between gap-3 py-3">
            <div>
              <p className="font-mono font-bold">{row.location_code}</p>
              <p className="text-sm text-slate-500 dark:text-slate-400">
                {row.moved_by_name} · {new Date(row.moved_at).toLocaleString()}
              </p>
            </div>
            <span
              className={`chip ${
                row.is_quarantine
                  ? 'bg-bad-bg text-bad dark:bg-bad-darkbg dark:text-bad-dark'
                  : 'bg-ok-bg text-ok dark:bg-ok-darkbg dark:text-ok-dark'
              }`}
            >
              {row.units} {row.disposition}
            </span>
          </li>
        ))}
      </ul>
    </Card>
  )
}
