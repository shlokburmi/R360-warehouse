import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, get, post, postControlPoint } from '@/lib/api'
import { useErrorText } from '@/hooks/useErrorText'
import { useScanning } from '@/hooks/useScanning'
import { useRealtimeInvalidate } from '@/hooks/useRealtimeInvalidate'
import { cleanVehicleNumber, VEHICLE_RE } from '@/lib/validation'
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
import type { AwaitingPickup, ExitRequestResult, Pickup, PickupVerifyResult } from '@/types'

/**
 * PRD §5.7 and Step 10 — Pickup verification and gate exit. CONTROL POINT 7.
 *
 * The last hard stop. Everything before this can be corrected inside the
 * building; once the vehicle is on the road a missing carton is somebody else's
 * problem and nobody's record. So the release button does not exist until every
 * released carton has been physically scanned onto the vehicle.
 */
export function PickupPage() {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [openPickupId, setOpenPickupId] = useState<string | null>(null)
  const [registering, setRegistering] = useState<AwaitingPickup | null>(null)

  const awaiting = useQuery({
    queryKey: ['awaiting-pickup'],
    queryFn: () => get<AwaitingPickup[]>('/pickups/awaiting'),
    refetchInterval: 20_000,
  })

  useRealtimeInvalidate('batches', [['awaiting-pickup']])
  useRealtimeInvalidate('pickups', [['pickups']])

  const active = useQuery({
    queryKey: ['pickups'],
    queryFn: () =>
      get<Pickup[]>(
        '/pickups?status=registered&status=verifying&status=verified&status=exit_pending',
      ),
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
      <h1 className="text-2xl font-black">{t('pickup.title')}</h1>

      {active.data && active.data.length > 0 && (
        <Card title={t('pickup.vehicles_onsite')}>
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
        title={t('pickup.waiting')}
        subtitle={t('pickup.register_hint')}
      >
        {awaiting.data?.length === 0 ? (
          <EmptyState
            title={t('pickup.none')}
            hint={t('pickup.none_hint')}
          />
        ) : (
          <ul className="divide-y divide-slate-200 dark:divide-slate-800">
            {awaiting.data?.map((batch) => (
              <li key={batch.batch_id} className="flex items-center justify-between gap-3 py-3">
                <div className="min-w-0">
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
                  {t('pickup.register_vehicle')}
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
  const { t } = useTranslation()
  const errorText = useErrorText()
  const [vehicle, setVehicle] = useState('')
  const [courier, setCourier] = useState('')
  const [persons, setPersons] = useState<PersonDraft[]>([blankPerson('driver')])
  const [error, setError] = useState<ApiError | null>(null)

  const submit = useMutation({
    mutationFn: () =>
      post<Pickup>('/pickups', {
        batch_id: batch.batch_id,
        vehicle_number: vehicle,
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
  const blocked = persons.some((p) => p.lookup?.is_blocked)

  const problems: string[] = []
  if (!VEHICLE_RE.test(vehicle)) problems.push(t('gate.problem_vehicle'))
  if (driverCount !== 1) problems.push(t('gate.one_driver'))
  if (blocked) problems.push(t('gate.problem_blocked'))
  persons.forEach((p, index) => {
    const label = p.visitor_role === 'driver' ? t('person.driver') : `#${index + 1}`
    if (p.full_name.trim().length < 2) problems.push(t('gate.problem_name', { who: label }))
    if (!/^[6-9]\d{9}$/.test(p.mobile.replace(/\D/g, '')))
      problems.push(t('gate.problem_mobile', { who: label }))
    if (p.lookup?.photo_required && !p.id_photo_path)
      problems.push(t('gate.problem_photo', { who: label }))
  })

  const valid = problems.length === 0

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-2xl font-black">{t('pickup.register_vehicle_title')}</h1>
          <p className="text-base text-slate-500 dark:text-slate-400">
            Collecting {batch.batch_code} · {batch.carton_count} cartons
          </p>
        </div>
        <button type="button" className="text-base font-semibold text-slate-500" onClick={onCancel}>
          {t('common.cancel')}
        </button>
      </div>

      {error && (
        <Banner tone="bad" title={errorText(error).title}>
          {error.hint}
        </Banner>
      )}

      <Card title={t('gate.vehicle')}>
        <Field
          label={t('gate.vehicle_number')}
          required
          hint={t('gate.vehicle_format_hint')}
        >
          <input
            className="input font-mono uppercase"
            value={vehicle}
            onChange={(event) => setVehicle(cleanVehicleNumber(event.target.value))}
            placeholder="KA01AB1234"
            maxLength={10}
            autoCapitalize="characters"
            autoCorrect="off"
            spellCheck={false}
          />
          {vehicle.length === 10 && !VEHICLE_RE.test(vehicle) && (
            <p className="mt-1 text-sm font-semibold text-bad dark:text-bad-dark">
              {t('gate.problem_vehicle')}
            </p>
          )}
        </Field>
        <Field label={t('pickup.courier')}>
          <input
            className="input"
            value={courier}
            onChange={(event) => setCourier(event.target.value)}
            placeholder={t('common.optional_label')}
          />
        </Field>
      </Card>

      <PersonFields persons={persons} setPersons={setPersons} />

      {problems.length > 0 && (
        <Banner tone="warn" title={t('gate.cant_submit_yet')}>
          <ul className="list-disc space-y-0.5 pl-4">
            {problems.map((problem) => (
              <li key={problem}>{problem}</li>
            ))}
          </ul>
        </Banner>
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
  const { t } = useTranslation()
  const errorText = useErrorText()
  const queryClient = useQueryClient()
  const [error, setError] = useState<ApiError | null>(null)
  const [verifyResult, setVerifyResult] = useState<PickupVerifyResult | null>(null)

  const pickup = useQuery({
    queryKey: ['pickup', pickupId],
    queryFn: () => get<Pickup>(`/pickups/${pickupId}`),
    refetchInterval: 10_000,
  })

  useRealtimeInvalidate('pickups', [['pickup', pickupId]], `id=eq.${pickupId}`)

  // Same scan loop as every other scanning page, so gate exit gets offline
  // queueing and idempotent replay without any extra work. There is no
  // physical carton label to scan any more, so each tap submits the carton's
  // own invoice number — fn_scan_resolve already falls back to matching it
  // directly when no sticker exists.
  const { feedback, submit, busy } = useScanning(pickupId, 'gate_exit')

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

  const requestExit = useMutation({
    mutationFn: () => post<ExitRequestResult>(`/pickups/${pickupId}/request-exit`),
    onSuccess: () => {
      setError(null)
      void queryClient.invalidateQueries({ queryKey: ['pickup', pickupId] })
      void queryClient.invalidateQueries({ queryKey: ['pickups'] })
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
  if (!pickup.data) return <Banner tone="bad" title={t('pickup.not_found')} />

  const p = pickup.data
  const loading = p.status === 'registered' || p.status === 'verifying'

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-2xl font-black">{p.vehicle_number}</h1>
          <p className="text-base text-slate-500 dark:text-slate-400">
            {p.pickup_code} · {p.batch_code}
            {p.courier_name ? ` · ${p.courier_name}` : ''}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <StatusChip status={p.status} />
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

      {verifyResult && !verifyResult.verified && (
        <Banner tone="bad" title={verifyResult.message}>
          {t('pickup.cannot_leave')}
          notified.
        </Banner>
      )}

      <ProgressCounter
        label={t('pickup.cartons_loaded')}
        scanned={p.verified_cartons}
        total={p.released_cartons}
      />

      {loading && feedback.length > 0 && (
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

      {/* CONTROL POINT 7 passing is no longer enough on its own: the guard asks,
          Admin decides, and only then does the gate open. If Admin sent it back,
          the reason is shown here rather than in a notification the guard may
          never have seen. */}
      {p.status === 'verified' && (
        <>
          <Banner
            tone={p.exit_rejected_note ? 'warn' : 'ok'}
            title={
              p.exit_rejected_note
                ? t('exitapproval.held', { note: p.exit_rejected_note })
                : `All ${p.released_cartons} cartons verified present`
            }
          >
            {p.verified_by_name}
          </Banner>
          <button
            type="button"
            className="btn-primary w-full"
            disabled={requestExit.isPending}
            onClick={() => requestExit.mutate()}
          >
            {t('exitapproval.request')}
          </button>
        </>
      )}

      {p.status === 'exit_pending' && (
        <>
          <Banner tone="info" title={t('exitapproval.requested')}>
            {p.exit_requested_at &&
              t('exitapproval.waiting_since', {
                time: new Date(p.exit_requested_at).toLocaleTimeString(),
              })}
          </Banner>

          {p.exit_approved_at ? (
            <>
              <Banner tone="ok" title={t('exitapproval.approved_open_gate')}>
                {p.exit_approved_by_name}
              </Banner>
              <button
                type="button"
                className="btn-success w-full"
                disabled={release.isPending}
                onClick={() => release.mutate()}
              >
                {t('pickup.open_gate_out')}
              </button>
            </>
          ) : null}
        </>
      )}

      {p.status === 'departed' && (
        <Banner tone="ok" title={p.message}>
          Released by {p.released_by_name}.
        </Banner>
      )}

      <Card title={t('pickup.people')}>
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
                  {t('approvals.id_on_file')}
                </span>
              )}
            </li>
          ))}
        </ul>
      </Card>

      <Card title={t('batches.cartons_in_batch')} subtitle={loading ? t('pickup.scan_each') : undefined}>
        <ul className="divide-y divide-slate-200 dark:divide-slate-800">
          {p.cartons.map((carton) => (
            <li key={carton.invoice_id} className="flex items-center justify-between gap-3 py-3">
              <div className="min-w-0">
                <p className="font-bold">{carton.invoice_number}</p>
                <p className="truncate text-sm text-slate-500 dark:text-slate-400">
                  {carton.customer_name}
                </p>
              </div>
              {carton.exit_scanned_at ? (
                <span className="chip bg-ok-bg text-ok dark:bg-ok-darkbg dark:text-ok-dark">
                  loaded
                </span>
              ) : loading ? (
                <button
                  type="button"
                  className="btn-primary shrink-0"
                  disabled={busy}
                  onClick={() => void submit(carton.invoice_number)}
                >
                  {t('pickup.mark_loaded')}
                </button>
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
