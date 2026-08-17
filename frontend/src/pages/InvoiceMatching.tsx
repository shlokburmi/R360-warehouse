import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ApiError, get, post } from '@/lib/api'
import { Scanner } from '@/components/Scanner'
import { BadgeScan } from '@/components/BadgeScan'
import { OrderNoScanner, type OrderNoReading } from '@/components/OrderNoScanner'
import { Banner, Card } from '@/components/ui'
import type { AttributionResult, Invoice, OrderNoResult } from '@/types'

/**
 * PRD §5.4 — Invoice matching (Invoice Matching Ladies #1 and #2).
 * CONTROL POINT 5, first half.
 *
 * The physical process is: take an invoice, find the product, put the product on
 * top of the invoice, confirm they match, scan your badge. The screen follows
 * exactly that order and shows one step at a time — this is a station where
 * someone is holding a box in one hand.
 */
type Step = 'scan_invoice' | 'read_order_no' | 'confirm_match' | 'scan_badge' | 'done'

export function InvoiceMatchingPage() {
  const queryClient = useQueryClient()

  const [step, setStep] = useState<Step>('scan_invoice')
  const [invoice, setInvoice] = useState<Invoice | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [result, setResult] = useState<AttributionResult | null>(null)

  function restart() {
    setStep('scan_invoice')
    setInvoice(null)
    setError(null)
  }

  async function lookup(invoiceNumber: string) {
    setError(null)
    try {
      const found = await get<Invoice>(
        `/invoices/lookup?invoice_number=${encodeURIComponent(invoiceNumber)}`,
      )
      setInvoice(found)
      setResult(null)
      // An Order No already on file is not read again. Re-reading it could only
      // either agree (no gain) or disagree (a conflict the server refuses), so
      // the second scan costs the matcher time to buy nothing.
      setStep(found.order_no ? 'confirm_match' : 'read_order_no')
    } catch (err) {
      setError(err as ApiError)
    }
  }

  /**
   * The Order No is metadata, not a control point, so a failure here must not
   * strand the invoice. On error the matcher is moved on to the match check
   * anyway and the message is shown — stopping CONTROL POINT 5 because a camera
   * could not read a printed field would be the wrong trade.
   */
  const saveOrderNo = useMutation({
    mutationFn: (reading: OrderNoReading) =>
      post<OrderNoResult>('/invoices/order-no', {
        invoice_number: invoice?.invoice_number,
        ...reading,
      }),
    onSuccess: (data) => {
      setInvoice(data.invoice)
      setError(null)
      setStep('confirm_match')
    },
    onError: (err) => {
      setError(err as ApiError)
      setStep('confirm_match')
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
      <h1 className="text-2xl font-black">Invoice Matching</h1>

      {error && (
        <Banner tone={error.isControlPoint ? 'bad' : 'warn'} title={error.message}>
          {error.hint}
        </Banner>
      )}

      {step === 'scan_invoice' && (
        <Card title="1 · Scan the invoice" subtitle="Scan or type the invoice number">
          <Scanner onScan={(code) => void lookup(code)} />
          <ManualInvoice onSubmit={(value) => void lookup(value)} />
        </Card>
      )}

      {step === 'read_order_no' && invoice && (
        <Card
          title="2 · Read the Order No"
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
            onClick={() => setStep('confirm_match')}
            disabled={saveOrderNo.isPending}
          >
            Skip — no Order No on this challan
          </button>
        </Card>
      )}

      {step === 'confirm_match' && invoice && (
        <>
          <Card title={invoice.invoice_number} subtitle={invoice.customer_name ?? undefined}>
            <dl className="grid grid-cols-2 gap-3 text-base">
              <div>
                <dt className="text-slate-500 dark:text-slate-400">Product</dt>
                <dd className="text-lg font-bold">{invoice.sku}</dd>
                <dd className="text-base">{invoice.description}</dd>
              </div>
              <div>
                <dt className="text-slate-500 dark:text-slate-400">Quantity</dt>
                <dd className="text-4xl font-black tabular-nums">{invoice.units}</dd>
              </div>
              {invoice.order_no && (
                <div className="col-span-2">
                  <dt className="text-slate-500 dark:text-slate-400">Order No</dt>
                  <dd className="font-mono text-lg font-bold tracking-wide">
                    {invoice.order_no}
                  </dd>
                </div>
              )}
            </dl>

            {invoice.suggested_locations.length > 0 && (
              <div className="mt-4 rounded-xl bg-info-bg p-3 dark:bg-info-darkbg">
                <p className="text-sm font-semibold uppercase tracking-wide text-info dark:text-info-dark">
                  Stock is at
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

          <Card title="3 · Confirm the product matches">
            <p className="mb-4 text-base">
              Fetch the product, place it on top of the invoice, and check the SKU and
              quantity above match what you are holding.
            </p>
            <div className="flex gap-3">
              <button type="button" className="btn-ghost flex-1" onClick={restart}>
                Doesn't match
              </button>
              <button
                type="button"
                className="btn-success flex-1"
                onClick={() => setStep('scan_badge')}
              >
                It matches
              </button>
            </div>
          </Card>
        </>
      )}

      {step === 'scan_badge' && invoice && (
        <Card title="4 · Scan your badge" subtitle={invoice.invoice_number}>
          <BadgeScan
            label="Confirm you verified this invoice"
            busy={verify.isPending}
            onBadge={(code) => verify.mutate(code)}
          />
          <button type="button" className="btn-ghost mt-3 w-full" onClick={restart}>
            Cancel
          </button>
        </Card>
      )}

      {step === 'done' && result && (
        <>
          <Banner tone="ok" title="Invoice verified — ready for packing">
            {result.invoice.invoice_number} verified by {result.who.full_name}. Call a
            packing lady to collect it.
          </Banner>
          <button type="button" className="btn-primary w-full" onClick={restart}>
            Next invoice
          </button>
        </>
      )}
    </div>
  )
}

function ManualInvoice({ onSubmit }: { onSubmit: (value: string) => void }) {
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
        placeholder="INV-2026-0001"
        value={value}
        onChange={(event) => setValue(event.target.value.toUpperCase())}
        autoCapitalize="characters"
        autoCorrect="off"
        spellCheck={false}
        aria-label="Invoice number"
      />
      <button type="submit" className="btn-primary" disabled={value.trim().length < 3}>
        Find
      </button>
    </form>
  )
}
