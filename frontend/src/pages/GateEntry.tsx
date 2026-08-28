import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { get, post, ApiError } from '@/lib/api'
import { useErrorText } from '@/hooks/useErrorText'
import { cleanVehicleNumber, VEHICLE_RE, PO_REFERENCE_RE } from '@/lib/validation'
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
  const queryClient = useQueryClient()

  const [vehicle, setVehicle] = useState('')
  const [vendorId, setVendorId] = useState('')
  const [poId, setPoId] = useState('')
  const [poNote, setPoNote] = useState('')
  const [transporter, setTransporter] = useState('')
  const [persons, setPersons] = useState<PersonDraft[]>([blankPerson('driver')])
  const [error, setError] = useState<ApiError | null>(null)
  const [submitted, setSubmitted] = useState<GateEntry | null>(null)

  // A vendor not yet in the system: the guard proposes it (name + optional
  // mobile), it's saved as a real-but-unconfirmed vendor row (`is_active =
  // false`), and Ops confirms it as part of deciding this same gate entry —
  // no separate approval screen needed for it.
  const [addingVendor, setAddingVendor] = useState(false)
  const [newVendorName, setNewVendorName] = useState('')
  const [newVendorMobile, setNewVendorMobile] = useState('')

  const proposeVendor = useMutation({
    mutationFn: () =>
      post<{ id: string; name: string }>('/gate/vendors/propose', {
        name: newVendorName.trim(),
        mobile: newVendorMobile ? newVendorMobile.replace(/\D/g, '') : null,
      }),
    onSuccess: (vendor) => {
      void queryClient.invalidateQueries({ queryKey: ['vendors'] })
      setVendorId(vendor.id)
      setPoId('')
      setAddingVendor(false)
      setNewVendorName('')
      setNewVendorMobile('')
      setError(null)
    },
    onError: (err) => setError(err as ApiError),
  })

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
        vehicle_number: vehicle,
        vendor_id: vendorId,
        purchase_order_id: poId || null,
        po_reference_note: poNote.trim().toUpperCase() || null,
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

  // One boolean disabling a button with no explanation left the guard unable
  // to tell which of ~6 conditions was unmet. Each one is named here instead,
  // so "why can't I submit" always has a visible answer.
  const problems: string[] = []
  if (!VEHICLE_RE.test(vehicle)) problems.push(t('gate.problem_vehicle'))
  if (!vendorId) problems.push(t('gate.problem_vendor'))
  if (driverCount !== 1) problems.push(t('gate.one_driver'))
  if (blocked) problems.push(t('gate.problem_blocked'))
  if (poNote.trim() && !PO_REFERENCE_RE.test(poNote.trim().toUpperCase()))
    problems.push(t('gate.problem_po_note'))
  persons.forEach((p, index) => {
    const label = p.visitor_role === 'driver' ? t('person.driver') : `#${index + 1}`
    if (p.full_name.trim().length < 2) problems.push(t('gate.problem_name', { who: label }))
    if (!/^[6-9]\d{9}$/.test(p.mobile.replace(/\D/g, '')))
      problems.push(t('gate.problem_mobile', { who: label }))
    if (p.lookup?.photo_required && !p.id_photo_path)
      problems.push(t('gate.problem_photo', { who: label }))
  })

  const valid = problems.length === 0

  if (submitted) {
    return (
      <div className="space-y-4">
        <Banner tone="warn" title={t('gate.sent_for_approval')}>
          Waiting for Admin to approve <strong>{submitted.entry_code}</strong>. The
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
              setPoNote('')
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

        <Field label={t('gate.vendor')} required>
          {!addingVendor ? (
            <select
              className="input"
              value={vendorId}
              onChange={(event) => {
                if (event.target.value === '__new__') {
                  setAddingVendor(true)
                  return
                }
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
              <option value="__new__">{t('gate.add_new_vendor')}</option>
            </select>
          ) : (
            <div className="space-y-2 rounded-xl border-2 border-dashed border-slate-300 p-3 dark:border-slate-700">
              <input
                className="input"
                value={newVendorName}
                onChange={(event) => setNewVendorName(event.target.value)}
                placeholder={t('gate.new_vendor_name')}
                autoFocus
              />
              <input
                className="input font-mono"
                type="tel"
                inputMode="numeric"
                maxLength={10}
                value={newVendorMobile}
                onChange={(event) =>
                  setNewVendorMobile(event.target.value.replace(/\D/g, '').slice(0, 10))
                }
                placeholder={t('gate.new_vendor_mobile')}
              />
              <p className="text-sm text-slate-500 dark:text-slate-400">
                {t('gate.new_vendor_hint')}
              </p>
              <div className="flex gap-2">
                <button
                  type="button"
                  className="btn-ghost flex-1"
                  onClick={() => {
                    setAddingVendor(false)
                    setNewVendorName('')
                    setNewVendorMobile('')
                  }}
                >
                  {t('common.cancel')}
                </button>
                <button
                  type="button"
                  className="btn-primary flex-1"
                  disabled={newVendorName.trim().length < 2 || proposeVendor.isPending}
                  onClick={() => proposeVendor.mutate()}
                >
                  {proposeVendor.isPending ? t('common.saving') : t('common.add')}
                </button>
              </div>
            </div>
          )}
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

        {/* Always visible, regardless of whether a PO got picked from the
            dropdown above — it was previously hidden the moment any PO was
            selected there, with no obvious way back to it, which is exactly
            what looked like "I can't type a PO" from the outside. */}
        <Field label={t('gate.po_reference_note')} hint={t('gate.po_reference_note_hint')}>
          <input
            className="input font-mono uppercase"
            value={poNote}
            onChange={(event) => setPoNote(event.target.value.toUpperCase())}
            placeholder="PO-2026-0001"
            maxLength={12}
            disabled={!vendorId}
            autoCapitalize="characters"
            autoCorrect="off"
            spellCheck={false}
          />
          {poNote.trim().length > 0 && !PO_REFERENCE_RE.test(poNote.trim()) && (
            <p className="mt-1 text-sm font-semibold text-bad dark:text-bad-dark">
              {t('gate.problem_po_note')}
            </p>
          )}
        </Field>

        <Field label={t('gate.transporter')} hint={t('gate.transporter_hint')}>
          <input
            className="input"
            value={transporter}
            onChange={(event) => setTransporter(event.target.value)}
            placeholder={t('common.optional_label')}
          />
        </Field>
      </Card>

      <PersonFields persons={persons} setPersons={setPersons} />

      {problems.length > 0 && persons.length > 0 && (
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
        {submit.isPending ? 'Sending…' : 'SEND FOR APPROVAL'}
      </button>

      <p className="pb-6 text-center text-base text-slate-500 dark:text-slate-400">
        {t('gate.stays_locked')}
      </p>
    </div>
  )
}
