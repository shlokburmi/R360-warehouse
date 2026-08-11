import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, get, post, postControlPoint } from '@/lib/api'
import { Scanner } from '@/components/Scanner'
import { useScanning } from '@/hooks/useScanning'
import { PersonFields, type PersonDraft, blankPerson } from '@/components/PersonFields'
import {
  Banner,
  Card,
  EmptyState,
  Field,
  ProgressCounter,
  Spinner,
  StatusChip,
} from '@/components/ui'
import type { AwaitingPickup, Pickup, PickupVerifyResult } from '@/types'

/**
 * PRD §5.7 and Step 10 — Pickup verification and gate exit. CONTROL POINT 7.
 *
 * The last hard stop. Everything before this can be corrected inside the
 * building; once the vehicle is on the road a missing carton is somebody else's
 * problem and nobody's record. So the release button does not exist until every
 * released carton has been physically scanned onto the vehicle.
 */
export function PickupPage() {
  const queryClient = useQueryClient()
  const [openPickupId, setOpenPickupId] = useState<string | null>(null)
  const [registering, setRegistering] = useState<AwaitingPickup | null>(null)

  const awaiting = useQuery({
    queryKey: ['awaiting-pickup'],
    queryFn: () => get<AwaitingPickup[]>('/pickups/awaiting'),
    refetchInterval: 20_000,
  })

  const active = useQuery({
    queryKey: ['pickups'],
    queryFn: () => get<Pickup[]>('/pickups?status=registered&status=verifying&status=verified'),
    refetchInterval: 20_000,
  })

  if (awaiting.isLoading || active.isLoading) return <Spinner />

  if (openPickupId) {
    return (
      <PickupDetail
        pickupId={openPickupId}
        onBack={() => {
          setOpenPickupId(null)
          void queryClient.invalidateQueries({ queryKey: ['pickups'] })
          void queryClient.invalidateQueries({ queryKey: ['awaiting-pickup'] })
        }}
      />
    )
  }

  if (registering) {
    return (
      <RegisterPickup
        batch={registering}
        onCancel={() => setRegistering(null)}
        onDone={(pickup) => {
          setRegistering(null)
          setOpenPickupId(pickup.pickup_id)
          void queryClient.invalidateQueries({ queryKey: ['awaiting-pickup'] })
        }}
      />
    )
  }

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-black">Pickup & Gate Exit</h1>

      {active.data && active.data.length > 0 && (
        <Card title="Vehicles on site">
          <ul className="divide-y divide-slate-200 dark:divide-slate-800">
            {active.data.map((pickup) => (
              <li
                key={pickup.pickup_id}
                className="flex items-center justify-between gap-3 py-3"
              >
                <div className="min-w-0">
                  <p className="font-bold">{pickup.vehicle_number}</p>
                  <p className="truncate text-sm text-slate-500 dark:text-slate-400">
                    {pickup.pickup_code} · {pickup.batch_code} ·{' '}
                    {pickup.verified_cartons}/{pickup.released_cartons} loaded
                  </p>
                </div>
                <div className="flex items-center gap-3">
                  <StatusChip status={pickup.status} />
                  <button
                    type="button"
                    className="btn-primary"
                    onClick={() => setOpenPickupId(pickup.pickup_id)}
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
        title="Batches waiting for collection"
        subtitle="Register the vehicle when the courier arrives"
      >
        {awaiting.data?.length === 0 ? (
          <EmptyState
            title="Nothing waiting for pickup"
            hint="Batches appear here once Ops has released them."
          />
        ) : (
          <ul className="divide-y divide-slate-200 dark:divide-slate-800">
            {awaiting.data?.map((batch) => (
              <li key={batch.batch_id} className="flex items-center justify-between gap-3 py-3">
                <div>
                  <p className="font-bold">{batch.batch_code}</p>
                  <p className="text-sm text-slate-500 dark:text-slate-400">
                    {batch.carton_count} cartons · released by {batch.released_by_name}
                  </p>
                </div>
                <button
                  type="button"
                  className="btn-primary"
                  onClick={() => setRegistering(batch)}
                >
                  Register vehicle
                </button>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  )
}

function RegisterPickup({
  batch,
  onCancel,
  onDone,
}: {
  batch: AwaitingPickup
  onCancel: () => void
  onDone: (pickup: Pickup) => void
}) {
  const [vehicle, setVehicle] = useState('')
  const [courier, setCourier] = useState('')
  const [persons, setPersons] = useState<PersonDraft[]>([blankPerson('driver')])
  const [error, setError] = useState<ApiError | null>(null)

  const submit = useMutation({
    mutationFn: () =>
      post<Pickup>('/pickups', {
        batch_id: batch.batch_id,
        vehicle_number: vehicle.toUpperCase().replace(/\s/g, ''),
        courier_name: courier || null,
        persons: persons.map((p) => ({
          full_name: p.full_name.trim(),
          mobile: p.mobile,
          visitor_role: p.visitor_role,
          id_photo_path: p.id_photo_path ?? null,
        })),
      }),
    onSuccess: onDone,
    onError: (err) => setError(err as ApiError),
  })

  const driverCount = persons.filter((p) => p.visitor_role === 'driver').length
  const valid =
    vehicle.trim().length >= 4 &&
    driverCount === 1 &&
    !persons.some((p) => p.lookup?.is_blocked) &&
    persons.every(
      (p) =>
        p.full_name.trim().length >= 2 &&
        /^[6-9]\d{9}$/.test(p.mobile.replace(/\D/g, '')) &&
        (!p.lookup?.photo_required || Boolean(p.id_photo_path)),
    )

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-black">Register Vehicle</h1>
          <p className="text-base text-slate-500 dark:text-slate-400">
            Collecting {batch.batch_code} · {batch.carton_count} cartons
          </p>
        </div>
        <button type="button" className="text-base font-semibold text-slate-500" onClick={onCancel}>
          Cancel
        </button>
      </div>

      {error && (
        <Banner tone="bad" title={error.message}>
          {error.hint}
        </Banner>
      )}

      <Card title="Vehicle">
        <Field label="Vehicle number" required>
          <input
            className="input font-mono uppercase"
            value={vehicle}
            onChange={(event) => setVehicle(event.target.value.toUpperCase())}
            placeholder="KA01AB1234"
            autoCapitalize="characters"
            autoCorrect="off"
            spellCheck={false}
          />
        </Field>
        <Field label="Courier / customer">
          <input
            className="input"
            value={courier}
            onChange={(event) => setCourier(event.target.value)}
            placeholder="Optional"
          />
        </Field>
      </Card>

      <PersonFields persons={persons} setPersons={setPersons} />

      {driverCount !== 1 && (
        <Banner tone="warn" title="Exactly one person must be marked as the driver" />
      )}

      <button
        type="button"
        className="btn-primary w-full"
        disabled={!valid || submit.isPending}
        onClick={() => submit.mutate()}
      >
        {submit.isPending ? 'Registering…' : 'Register and start loading'}
      </button>
    </div>
  )
}

function PickupDetail({ pickupId, onBack }: { pickupId: string; onBack: () => void }) {
  const queryClient = useQueryClient()
  const [error, setError] = useState<ApiError | null>(null)
  const [verifyResult, setVerifyResult] = useState<PickupVerifyResult | null>(null)

  const pickup = useQuery({
    queryKey: ['pickup', pickupId],
    queryFn: () => get<Pickup>(`/pickups/${pickupId}`),
    refetchInterval: 10_000,
  })

  // Same scan loop as every other scanning page, so gate exit gets offline
  // queueing and idempotent replay without any extra work.
  const { feedback, submit } = useScanning(pickupId, 'gate_exit')

  const verify = useMutation({
    mutationFn: () =>
      postControlPoint<PickupVerifyResult>(`/pickups/${pickupId}/verify`),
    onSuccess: (data) => {
      setVerifyResult(data)
      setError(null)
      void queryClient.invalidateQueries({ queryKey: ['pickup', pickupId] })
    },
    onError: (err) => setError(err as ApiError),
  })

  const release = useMutation({
    mutationFn: () => post<Pickup>(`/pickups/${pickupId}/release`),
    onSuccess: () => {
      setError(null)
      void queryClient.invalidateQueries({ queryKey: ['pickup', pickupId] })
      void queryClient.invalidateQueries({ queryKey: ['pickups'] })
    },
    onError: (err) => setError(err as ApiError),
  })

  if (pickup.isLoading) return <Spinner />
  if (!pickup.data) return <Banner tone="bad" title="Pickup not found" />

  const p = pickup.data
  const loading = p.status === 'registered' || p.status === 'verifying'

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-black">{p.vehicle_number}</h1>
          <p className="text-base text-slate-500 dark:text-slate-400">
            {p.pickup_code} · {p.batch_code}
            {p.courier_name ? ` · ${p.courier_name}` : ''}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <StatusChip status={p.status} />
          <button type="button" className="text-base font-semibold text-slate-500" onClick={onBack}>
            Back
          </button>
        </div>
      </div>

      {error && (
        <Banner tone={error.isControlPoint ? 'bad' : 'warn'} title={error.message}>
          {error.hint}
        </Banner>
      )}

      {verifyResult && !verifyResult.verified && (
        <Banner tone="bad" title={verifyResult.message}>
          The vehicle cannot leave until every carton is accounted for. Ops has been
          notified.
        </Banner>
      )}

      <ProgressCounter
        label="Cartons loaded"
        scanned={p.verified_cartons}
        total={p.released_cartons}
      />

      {loading && (
        <Card title="Scan each carton as it is loaded">
          <Scanner onScan={(code) => void submit(code)} />

          {feedback.length > 0 && (
            <ul className="mt-4 space-y-2">
              {feedback.map((item) => (
                <li
                  key={item.id}
                  className={`flex items-center justify-between gap-3 rounded-lg px-3 py-2 text-base ${
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
        </Card>
      )}

      {loading && (
        <button
          type="button"
          className={p.remaining_cartons === 0 ? 'btn-success w-full' : 'btn-ghost w-full'}
          disabled={verify.isPending}
          onClick={() => verify.mutate()}
        >
          {p.remaining_cartons === 0
            ? 'All cartons loaded — verify'
            : `Verify (${p.remaining_cartons} still missing)`}
        </button>
      )}

      {p.status === 'verified' && (
        <>
          <Banner tone="ok" title={`All ${p.released_cartons} cartons verified present`}>
            Verified by {p.verified_by_name}. The vehicle may leave.
          </Banner>
          <button
            type="button"
            className="btn-success w-full"
            disabled={release.isPending}
            onClick={() => release.mutate()}
          >
            Open gate · record time out
          </button>
        </>
      )}

      {p.status === 'departed' && (
        <Banner tone="ok" title={p.message}>
          Released by {p.released_by_name}.
        </Banner>
      )}

      <Card title="People on the vehicle">
        <ul className="space-y-1 text-base">
          {p.persons.map((person) => (
            <li key={person.visitor_id} className="flex items-center gap-2">
              <span className="capitalize text-slate-500 dark:text-slate-400">
                {person.visitor_role}:
              </span>
              <span className="font-semibold">{person.full_name}</span>
              <span className="font-mono text-sm text-slate-500">{person.mobile}</span>
              {person.has_id_photo && (
                <span className="chip bg-ok-bg text-ok dark:bg-ok-darkbg dark:text-ok-dark">
                  ID on file
                </span>
              )}
            </li>
          ))}
        </ul>
      </Card>

      <Card title="Cartons in this batch">
        <ul className="divide-y divide-slate-200 dark:divide-slate-800">
          {p.cartons.map((carton) => (
            <li key={carton.invoice_id} className="flex items-center justify-between gap-3 py-3">
              <div className="min-w-0">
                <p className="font-bold">{carton.invoice_number}</p>
                <p className="truncate text-sm text-slate-500 dark:text-slate-400">
                  {carton.sku} × {carton.units} · {carton.customer_name}
                </p>
              </div>
              {carton.exit_scanned_at ? (
                <span className="chip bg-ok-bg text-ok dark:bg-ok-darkbg dark:text-ok-dark">
                  loaded
                </span>
              ) : (
                <span className="chip bg-warn-bg text-warn dark:bg-warn-darkbg dark:text-warn-dark">
                  not loaded
                </span>
              )}
            </li>
          ))}
        </ul>
      </Card>
    </div>
  )
}
