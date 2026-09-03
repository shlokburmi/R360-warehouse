import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, get, post } from '@/lib/api'
import { useErrorText } from '@/hooks/useErrorText'
import { useScanning } from '@/hooks/useScanning'
import { BadgeScan } from '@/components/BadgeScan'
import { Scanner } from '@/components/Scanner'
import { OrderNoScanner, type OrderNoReading } from '@/components/OrderNoScanner'
import { Banner, Card, Field, ProgressCounter } from '@/components/ui'
import type {
  AttributionResult,
  Invoice,
  MatchingState,
  PurchaseOrder,
  PurchaseOrderLine,
} from '@/types'

/**
 * PRD §5.4/§7 — Invoice matching, done by whichever Packer physically has the
 * invoice (0035_packer_invoice_creation.sql — this used to be a separate
 * Invoice Matcher role's job; a Packer now does both this and the packing
 * step, on different invoices, since CONTROL POINT 5 is enforced by identity
 * — verifier != packer — not by which role holds the scanner).
 * CONTROL POINT 5, first half.
 *
 * The physical process is: take the printed invoice, scan it (OCR reads the
 * Order No — there is no manual invoice-number entry anywhere in this
 * dashboard, and no barcode to scan; Admin's printing happens entirely
 * outside this app), find the product, put it on top of the invoice, scan
 * every unit sticker to confirm it, scan your own badge. The screen follows
 * exactly that order and shows one step at a time — this is a station where
 * someone is holding a box in one hand.
 *
 * The unit scan (`scan_units`) is additive to the equivalent check at packing
 * (CG3, DECISIONS.md) — a second, independent product-in-hand confirmation,
 * required before the badge scan can succeed (fn_matching_units_complete,
 * 0024_matching_unit_scan.sql).
 */
type Step =
  | 'scan_invoice'
  | 'create_from_order_no'
  | 'read_order_no'
  | 'scan_units'
  | 'scan_badge'
  | 'done'

/** An Order No read that matched no existing invoice — held while the
 * operator fills in PO/product/units to create one from it. */
type PendingCreate = {
  orderNo: string
  rawText: string
  confidence: number | null
  wasCorrected: boolean
}

export function InvoiceMatchingPage() {
  const { t } = useTranslation()
  const errorText = useErrorText()
  const queryClient = useQueryClient()

  const [step, setStep] = useState<Step>('scan_invoice')
  const [invoice, setInvoice] = useState<Invoice | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [result, setResult] = useState<AttributionResult | null>(null)
  const [pendingCreate, setPendingCreate] = useState<PendingCreate | null>(null)
  /** Why the last OCR attempt didn't produce anything to look up. */
  const [scanNotice, setScanNotice] = useState<string | null>(null)

  function restart() {
    setStep('scan_invoice')
    setInvoice(null)
    setError(null)
    setPendingCreate(null)
    setScanNotice(null)
  }

  const invoiceId = invoice?.invoice_id ?? ''

  // The unit scan that must complete before the badge scan is allowed
  // (fn_matching_units_complete, 0024) — same scanning loop as every other
  // station, so the offline queue and its idempotent replay apply unchanged.
  const matching = useQuery({
    queryKey: ['matching-state', invoiceId],
    queryFn: () => get<MatchingState>(`/invoices/${invoiceId}/matching`),
    enabled: Boolean(invoiceId) && step === 'scan_units',
  })
  const scanning = useScanning(invoiceId, 'match_unit')

  // Move on the moment every unit is confirmed, whether that happened on this
  // scan or — resuming an interrupted match — was already true when the page
  // loaded.
  useEffect(() => {
    if (step === 'scan_units' && matching.data?.ready_to_verify) {
      setStep('scan_badge')
    }
  }, [step, matching.data?.ready_to_verify])

  /** Resolve an invoice already on file by its (scanned-Order-No-derived) number. */
  async function lookupByNumber(invoiceNumber: string) {
    setError(null)
    try {
      const found = await get<Invoice>(
        `/invoices/lookup?invoice_number=${encodeURIComponent(invoiceNumber)}`,
      )
      setInvoice(found)
      setResult(null)
      setScanNotice(null)
      // A legacy invoice from before this feature could still lack an
      // Order No — everything created via the scan-to-create flow always has
      // one, so this branch only matters for those older rows.
      setStep(found.order_no ? 'scan_units' : 'read_order_no')
    } catch (err) {
      setError(err as ApiError)
    }
  }

  /**
   * The Order No just came off a camera or uploaded photo. Find the invoice
   * it belongs to, or — the ordinary case, since every invoice starts life
   * as exactly this scan — start creating one from it.
   */
  async function handleOrderNoScan(reading: OrderNoReading) {
    setError(null)
    if (!reading.order_no) {
      setScanNotice(t('matching.ocr_miss'))
      return
    }
    setScanNotice(null)
    try {
      const found = await get<Invoice>(
        `/invoices/lookup?order_no=${encodeURIComponent(reading.order_no)}`,
      )
      setInvoice(found)
      setResult(null)
      setStep('scan_units')
    } catch {
      // Not a failure — this Order No simply has no invoice yet, which is the
      // normal case. Move to creating one from exactly what was just read.
      setPendingCreate({
        orderNo: reading.order_no,
        rawText: reading.raw_text,
        confidence: reading.confidence,
        wasCorrected: reading.was_corrected,
      })
      setStep('create_from_order_no')
    }
  }

  const createInvoice = useMutation({
    mutationFn: (body: {
      purchase_order_line_id: string
      units: number
      customer_name: string | null
    }) =>
      post<Invoice>('/invoices/from-order-no', {
        order_no: pendingCreate!.orderNo,
        raw_text: pendingCreate!.rawText,
        confidence: pendingCreate!.confidence,
        was_corrected: pendingCreate!.wasCorrected,
        source: 'ocr',
        ...body,
      }),
    onSuccess: (created) => {
      setError(null)
      setInvoice(created)
      setPendingCreate(null)
      setStep('scan_units')
      void queryClient.invalidateQueries({ queryKey: ['invoices'] })
    },
    onError: (err) => setError(err as ApiError),
  })

  /**
   * The Order No is metadata, not a control point, so a failure here must not
   * strand the invoice. On error the matcher is moved on to the match check
   * anyway and the message is shown — stopping CONTROL POINT 5 because a camera
   * could not read a printed field would be the wrong trade.
   */
  const saveOrderNo = useMutation({
    mutationFn: (reading: OrderNoReading) =>
      post<{ invoice: Invoice }>('/invoices/order-no', {
        invoice_number: invoice?.invoice_number,
        ...reading,
      }),
    onSuccess: (data) => {
      setInvoice(data.invoice)
      setError(null)
      setStep('scan_units')
    },
    onError: (err) => {
      setError(err as ApiError)
      setStep('scan_units')
    },
  })

  const verify = useMutation({
    mutationFn: (badgeCode: string) =>
      post<AttributionResult>('/invoices/verify', {
        invoice_number: invoice?.invoice_number,
        badge_code: badgeCode,
      }),
    onSuccess: (data) => {
      setResult(data)
      setError(null)
      setStep('done')
      void queryClient.invalidateQueries({ queryKey: ['invoices'] })
    },
    onError: (err) => setError(err as ApiError),
  })

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-black">{t('matching.title')}</h1>

      {error && (
        <Banner tone={error.isControlPoint ? 'bad' : 'warn'} title={errorText(error).title}>
          {error.hint}
        </Banner>
      )}

      {step === 'scan_invoice' && (
        <Card title={t('matching.step1')} subtitle={t('matching.step1_hint')}>
          <OrderNoScanner busy={false} onConfirm={(reading) => void handleOrderNoScan(reading)} />
          {scanNotice && (
            <p className="mt-2 text-sm font-semibold text-warn dark:text-warn-dark">
              {scanNotice}
            </p>
          )}
          <ManualInvoice onSubmit={(value) => void lookupByNumber(value)} />
        </Card>
      )}

      {step === 'create_from_order_no' && pendingCreate && (
        <CreateInvoiceCard
          orderNo={pendingCreate.orderNo}
          pending={createInvoice.isPending}
          onCreate={(body) => createInvoice.mutate(body)}
          onCancel={restart}
        />
      )}

      {step === 'read_order_no' && invoice && (
        <Card
          title={t('matching.step2')}
          subtitle={`Invoice ${invoice.invoice_number} — top-right of the challan`}
        >
          <OrderNoScanner
            invoiceNumber={invoice.invoice_number}
            busy={saveOrderNo.isPending}
            onConfirm={(reading) => saveOrderNo.mutate(reading)}
          />
          <button
            type="button"
            className="btn-ghost mt-3 w-full text-sm"
            onClick={() => setStep('scan_units')}
            disabled={saveOrderNo.isPending}
          >
            {t('matching.skip_order_no')}
          </button>
        </Card>
      )}

      {step === 'scan_units' && invoice && (
        <>
          <Card title={invoice.invoice_number} subtitle={invoice.customer_name ?? undefined}>
            <dl className="grid grid-cols-1 gap-3 text-base sm:grid-cols-2">
              <div>
                <dt className="text-slate-500 dark:text-slate-400">{t('matching.product')}</dt>
                <dd className="text-lg font-bold">{invoice.sku}</dd>
                <dd className="text-base">{invoice.description}</dd>
              </div>
              <div>
                <dt className="text-slate-500 dark:text-slate-400">{t('matching.quantity')}</dt>
                <dd className="text-4xl font-black tabular-nums">{invoice.units}</dd>
              </div>
              {invoice.order_no && (
                <div className="col-span-2">
                  <dt className="text-slate-500 dark:text-slate-400">{t('matching.order_no')}</dt>
                  <dd className="font-mono text-lg font-bold tracking-wide">
                    {invoice.order_no}
                  </dd>
                </div>
              )}
            </dl>

            {invoice.suggested_locations.length > 0 && (
              <div className="mt-4 rounded-xl bg-info-bg p-3 dark:bg-info-darkbg">
                <p className="text-sm font-semibold uppercase tracking-wide text-info dark:text-info-dark">
                  {t('matching.stock_is_at')}
                </p>
                <ul className="mt-1 flex flex-wrap gap-3">
                  {invoice.suggested_locations.map((loc) => (
                    <li key={loc.location_code} className="font-mono text-lg font-bold">
                      {loc.location_code}
                      <span className="ml-1 text-base font-normal">({loc.units})</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </Card>

          <ProgressCounter
            scanned={matching.data?.matched_units ?? 0}
            total={matching.data?.required_units ?? invoice.units}
            label={t('matching.units_confirmed')}
          />

          <Card title={t('matching.step3')} subtitle={t('matching.step3_hint')}>
            <Scanner onScan={(code) => void scanning.submit(code)} paused={scanning.busy} />

            {scanning.feedback.length > 0 && (
              <ul className="mt-3 space-y-2">
                {scanning.feedback.map((item) => (
                  <li key={item.id}>
                    <Banner tone={item.tone} title={item.code}>
                      {item.message}
                    </Banner>
                  </li>
                ))}
              </ul>
            )}

            <button type="button" className="btn-ghost mt-3 w-full" onClick={restart}>
              {t('matching.doesnt_match')}
            </button>
          </Card>
        </>
      )}

      {step === 'scan_badge' && invoice && (
        <Card title={t('matching.step4')} subtitle={invoice.invoice_number}>
          <BadgeScan
            label={t('matching.confirm_verified')}
            busy={verify.isPending}
            onBadge={(code) => verify.mutate(code)}
          />
          <button type="button" className="btn-ghost mt-3 w-full" onClick={restart}>
            {t('common.cancel')}
          </button>
        </Card>
      )}

      {step === 'done' && result && (
        <>
          <Banner tone="ok" title={t('matching.verified')}>
            {result.invoice.invoice_number} verified by {result.who.full_name}. Call a
            packing lady to collect it.
          </Banner>
          <button type="button" className="btn-primary w-full" onClick={restart}>
            {t('matching.next_invoice')}
          </button>
        </>
      )}
    </div>
  )
}

function ManualInvoice({ onSubmit }: { onSubmit: (value: string) => void }) {
  const { t } = useTranslation()
  const [value, setValue] = useState('')

  return (
    <form
      className="mt-3 flex gap-2"
      onSubmit={(event) => {
        event.preventDefault()
        onSubmit(value.trim())
        setValue('')
      }}
    >
      <input
        className="input font-mono uppercase"
        placeholder="CP002458380_0001"
        value={value}
        onChange={(event) => setValue(event.target.value.toUpperCase())}
        autoCapitalize="characters"
        autoCorrect="off"
        spellCheck={false}
        aria-label={t('matching.invoice_number')}
      />
      <button type="submit" className="btn-primary" disabled={value.trim().length < 3}>
        {t('matching.find')}
      </button>
    </form>
  )
}

/**
 * The rest of what an invoice needs, once the Order No has been scanned: the
 * PO/product it's against and how many units. These stay manually picked —
 * unlike the Order No, there's no fixed format OCR could lock onto for them.
 */
function CreateInvoiceCard({
  orderNo,
  pending,
  onCreate,
  onCancel,
}: {
  orderNo: string
  pending: boolean
  onCreate: (body: {
    purchase_order_line_id: string
    units: number
    customer_name: string | null
  }) => void
  onCancel: () => void
}) {
  const { t } = useTranslation()
  const [poId, setPoId] = useState('')
  const [lineId, setLineId] = useState('')
  const [units, setUnits] = useState('')
  const [customerName, setCustomerName] = useState('')

  const purchaseOrders = useQuery({
    queryKey: ['purchase-orders', 'open'],
    queryFn: () => get<PurchaseOrder[]>('/purchase-orders?open_only=false'),
  })

  const lines = useQuery({
    queryKey: ['po-lines', poId],
    queryFn: () => get<PurchaseOrderLine[]>(`/purchase-orders/${poId}/lines`),
    enabled: Boolean(poId),
  })

  const selectedLine = lines.data?.find((l) => l.id === lineId)

  return (
    <Card title={t('matching.new_invoice_title')} subtitle={t('matching.new_invoice_hint')}>
      <p className="mb-3 font-mono text-lg font-bold tracking-wide">{orderNo}</p>

      <Field label={t('invoices.po')} required>
        <select
          className="input"
          value={poId}
          onChange={(event) => {
            setPoId(event.target.value)
            setLineId('')
          }}
        >
          <option value="">{t('invoices.choose_po')}</option>
          {purchaseOrders.data?.map((po) => (
            <option key={po.id} value={po.id}>
              {po.po_number} — {po.vendor_name}
            </option>
          ))}
        </select>
      </Field>

      {poId && (
        <Field label={t('invoices.product')} required>
          <select className="input" value={lineId} onChange={(event) => setLineId(event.target.value)}>
            <option value="">{t('invoices.choose_product')}</option>
            {lines.data?.map((line) => (
              <option key={line.id} value={line.id}>
                {line.sku} — {line.description ?? ''}
              </option>
            ))}
          </select>
        </Field>
      )}

      <Field label={t('invoices.units')} required>
        <input
          className="input"
          type="number"
          inputMode="numeric"
          min={1}
          value={units}
          onChange={(event) => setUnits(event.target.value)}
        />
      </Field>

      <Field label={t('invoices.customer')}>
        <input
          className="input"
          value={customerName}
          onChange={(event) => setCustomerName(event.target.value)}
        />
      </Field>

      {selectedLine && (
        <p className="mb-3 text-sm text-slate-500 dark:text-slate-400">
          {t('invoices.received_so_far', {
            received: selectedLine.received_units,
            expected: selectedLine.expected_units,
          })}
        </p>
      )}

      <div className="flex flex-col gap-2 sm:flex-row">
        <button
          type="button"
          className="btn-primary flex-1"
          disabled={!lineId || !units || Number(units) < 1 || pending}
          onClick={() =>
            onCreate({
              purchase_order_line_id: lineId,
              units: Number(units),
              customer_name: customerName.trim() || null,
            })
          }
        >
          {pending ? t('invoices.creating') : t('invoices.create')}
        </button>
        <button type="button" className="btn-ghost flex-1" onClick={onCancel} disabled={pending}>
          {t('common.cancel')}
        </button>
      </div>
    </Card>
  )
}
