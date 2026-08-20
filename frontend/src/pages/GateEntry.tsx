import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { get, post, ApiError } from '@/lib/api'
import { useErrorText } from '@/hooks/useErrorText'
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
  const { t } = useTranslation()
  const errorText = useErrorText()
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
        <Banner tone="warn" title={t('gate.sent_for_approval')}>
          Waiting for the Ops Manager to approve <strong>{submitted.entry_code}</strong>. The
          vehicle cannot enter until they do.
        </Banner>

        <Card title={submitted.entry_code} action={<StatusChip status={submitted.status} />}>
          <dl className="grid grid-cols-1 gap-3 text-base sm:grid-cols-2">
            <div>
              <dt className="text-slate-500 dark:text-slate-400">{t('gate.vehicle')}</dt>
              <dd className="font-bold">{submitted.vehicle_number}</dd>
            </div>
            <div>
              <dt className="text-slate-500 dark:text-slate-400">{t('gate.vendor')}</dt>
              <dd className="font-bold">{submitted.vendor_name}</dd>
            </div>
            <div>
              <dt className="text-slate-500 dark:text-slate-400">PO</dt>
              <dd className="font-bold">{submitted.po_number ?? '—'}</dd>
            </div>
            <div>
              <dt className="text-slate-500 dark:text-slate-400">{t('gate.people')}</dt>
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
            {t('gate.register_another')}
          </button>
          <button
            type="button"
            className="btn-primary flex-1"
            onClick={() => navigate('/entries')}
          >
            {t('gate.view_trucks')}
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-black">{t('gate.title')}</h1>

      {error && (
        <Banner tone="bad" title={errorText(error).title}>
          {error.hint}
        </Banner>
      )}

      <Card title={t('gate.vehicle')}>
        <Field label={t('gate.vehicle_number')} required>
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

        <Field label={t('gate.vendor')} required>
          <select
            className="input"
            value={vendorId}
            onChange={(event) => {
              setVendorId(event.target.value)
              setPoId('')
            }}
          >
            <option value="">{t('gate.select_vendor')}</option>
            {vendors.data?.map((vendor) => (
              <option key={vendor.id} value={vendor.id}>
                {vendor.name}
              </option>
            ))}
          </select>
        </Field>

        <Field
          label={t('gate.purchase_order')}
          hint={t('gate.po_hint')}
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

        <Field label={t('gate.transporter')}>
          <input
            className="input"
            value={transporter}
            onChange={(event) => setTransporter(event.target.value)}
            placeholder={t('common.optional_label')}
          />
        </Field>
      </Card>

      <PersonFields persons={persons} setPersons={setPersons} />

      {driverCount !== 1 && persons.length > 0 && (
        <Banner tone="warn" title={t('gate.one_driver')} />
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
        {t('gate.stays_locked')}
      </p>
    </div>
  )
}
