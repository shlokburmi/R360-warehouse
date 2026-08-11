import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { get, post, ApiError } from '@/lib/api'
import { Banner, Card, Field, StatusChip } from '@/components/ui'
import {
  PersonFields,
  blankPerson,
  type PersonDraft,
} from '@/components/PersonFields'
import type { GateEntry, PurchaseOrder, Vendor } from '@/types'

/**
 * PRD §5.1 — Gate Entry (Security Guard).
 *
 * The target is under two minutes per truck, which shapes almost every choice
 * here: the visitor lookup fires on the tenth digit of the mobile so a returning
 * driver's name arrives before the guard finishes typing it, and the photo step
 * only appears when it is actually required.
 */
export function GateEntryPage() {
  const navigate = useNavigate()

  const [vehicle, setVehicle] = useState('')
  const [vendorId, setVendorId] = useState('')
  const [poId, setPoId] = useState('')
  const [transporter, setTransporter] = useState('')
  const [persons, setPersons] = useState<PersonDraft[]>([blankPerson('driver')])
  const [error, setError] = useState<ApiError | null>(null)
  const [submitted, setSubmitted] = useState<GateEntry | null>(null)

  const vendors = useQuery({
    queryKey: ['vendors'],
    queryFn: () => get<Vendor[]>('/vendors'),
  })

  const purchaseOrders = useQuery({
    queryKey: ['purchase-orders', vendorId],
    queryFn: () => get<PurchaseOrder[]>(`/purchase-orders?vendor_id=${vendorId}`),
    enabled: Boolean(vendorId),
  })

  const submit = useMutation({
    mutationFn: () =>
      post<GateEntry>('/gate/entries', {
        vehicle_number: vehicle.toUpperCase().replace(/\s/g, ''),
        vendor_id: vendorId,
        purchase_order_id: poId || null,
        transporter_name: transporter || null,
        persons: persons.map((p) => ({
          full_name: p.full_name.trim(),
          mobile: p.mobile,
          visitor_role: p.visitor_role,
          id_photo_path: p.id_photo_path ?? null,
        })),
      }),
    onSuccess: (entry) => {
      setSubmitted(entry)
      setError(null)
    },
    onError: (err) => setError(err as ApiError),
  })

  const driverCount = persons.filter((p) => p.visitor_role === 'driver').length
  const blocked = persons.some((p) => p.lookup?.is_blocked)

  const valid =
    vehicle.trim().length >= 4 &&
    vendorId &&
    driverCount === 1 &&
    !blocked &&
    persons.every(
      (p) =>
        p.full_name.trim().length >= 2 &&
        /^[6-9]\d{9}$/.test(p.mobile.replace(/\D/g, '')) &&
        // Photo is only demanded when the lookup says so (DECISIONS.md §2).
        (!p.lookup?.photo_required || Boolean(p.id_photo_path)),
    )

  if (submitted) {
    return (
      <div className="space-y-4">
        <Banner tone="warn" title="Sent for approval — gate stays locked">
          Waiting for the Ops Manager to approve <strong>{submitted.entry_code}</strong>. The
          vehicle cannot enter until they do.
        </Banner>

        <Card title={submitted.entry_code} action={<StatusChip status={submitted.status} />}>
          <dl className="grid grid-cols-2 gap-3 text-base">
            <div>
              <dt className="text-slate-500 dark:text-slate-400">Vehicle</dt>
              <dd className="font-bold">{submitted.vehicle_number}</dd>
            </div>
            <div>
              <dt className="text-slate-500 dark:text-slate-400">Vendor</dt>
              <dd className="font-bold">{submitted.vendor_name}</dd>
            </div>
            <div>
              <dt className="text-slate-500 dark:text-slate-400">PO</dt>
              <dd className="font-bold">{submitted.po_number ?? '—'}</dd>
            </div>
            <div>
              <dt className="text-slate-500 dark:text-slate-400">People</dt>
              <dd className="font-bold">{submitted.persons.length}</dd>
            </div>
          </dl>
        </Card>

        <div className="flex gap-3">
          <button
            type="button"
            className="btn-ghost flex-1"
            onClick={() => {
              setSubmitted(null)
              setVehicle('')
              setPoId('')
              setTransporter('')
              setPersons([blankPerson('driver')])
            }}
          >
            Register another truck
          </button>
          <button
            type="button"
            className="btn-primary flex-1"
            onClick={() => navigate('/entries')}
          >
            View trucks
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-black">Gate Entry</h1>

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

        <Field label="Vendor" required>
          <select
            className="input"
            value={vendorId}
            onChange={(event) => {
              setVendorId(event.target.value)
              setPoId('')
            }}
          >
            <option value="">Select vendor…</option>
            {vendors.data?.map((vendor) => (
              <option key={vendor.id} value={vendor.id}>
                {vendor.name}
              </option>
            ))}
          </select>
        </Field>

        <Field
          label="Purchase order"
          hint="Needed before stickers can be issued — the PO is where expected quantities come from."
        >
          <select
            className="input"
            value={poId}
            onChange={(event) => setPoId(event.target.value)}
            disabled={!vendorId}
          >
            <option value="">
              {vendorId ? 'Select PO…' : 'Choose a vendor first'}
            </option>
            {purchaseOrders.data?.map((po) => (
              <option key={po.id} value={po.id}>
                {po.po_number} · {po.expected_units} units
              </option>
            ))}
          </select>
        </Field>

        <Field label="Transporter">
          <input
            className="input"
            value={transporter}
            onChange={(event) => setTransporter(event.target.value)}
            placeholder="Optional"
          />
        </Field>
      </Card>

      <PersonFields persons={persons} setPersons={setPersons} />

      {driverCount !== 1 && persons.length > 0 && (
        <Banner tone="warn" title="Exactly one person must be marked as the driver" />
      )}

      <button
        type="button"
        className="btn-primary w-full"
        disabled={!valid || submit.isPending}
        onClick={() => submit.mutate()}
      >
        {submit.isPending ? 'Sending…' : 'SEND FOR APPROVAL'}
      </button>

      <p className="pb-6 text-center text-base text-slate-500 dark:text-slate-400">
        The gate stays locked until the Ops Manager approves.
      </p>
    </div>
  )
}
