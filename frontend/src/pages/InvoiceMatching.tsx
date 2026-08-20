import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ApiError, get, post } from '@/lib/api'
import { useErrorText } from '@/hooks/useErrorText'
import { CodeCapture } from '@/components/CodeCapture'
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
  const { t } = useTranslation()
  const errorText = useErrorText()
  const queryClient = useQueryClient()

  const [step, setStep] = useState<Step>('scan_invoice')
  const [invoice, setInvoice] = useState<Invoice | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [result, setResult] = useState<AttributionResult | null>(null)
  /**
   * An Order No read off an uploaded file that has no invoice yet.
   *
   * Held rather than discarded: the read succeeded, and throwing it away would
   * make the operator read the same page twice. As soon as the invoice is
   * identified by any route, this is written to it and step 2 is skipped.
   */
  const [pendingOrderNo, setPendingOrderNo] = useState<string | null>(null)
  /** Progress the operator should see that is not a failure. */
  const [notice, setNotice] = useState<string | null>(null)

  function restart() {
    setStep('scan_invoice')
    setInvoice(null)
    setError(null)
    setPendingOrderNo(null)
    setNotice(null)
  }

  /**
   * Resolve an invoice from whichever identifier the operator produced.
   *
   * A camera scan gives an invoice number; an uploaded challan gives an Order No.
   * Both land here so the rest of the flow does not care which route was taken.
   */
  async function lookup(value: string, by: 'invoice_number' | 'order_no' = 'invoice_number') {
    setError(null)
    try {
      const found = await get<Invoice>(`/invoices/lookup?${by}=${encodeURIComponent(value)}`)
      setResult(null)

      // A read from an uploaded file that had no invoice yet: now that one is
      // identified, write it. This is the whole point of holding it — the page was
      // already read successfully, so asking the operator to read it again on
      // step 2 would be busywork.
      if (pendingOrderNo && !found.order_no) {
        const saved = await post<OrderNoResult>('/invoices/order-no', {
          invoice_number: found.invoice_number,
          order_no: pendingOrderNo,
          source: 'ocr',
          raw_text: null,
          confidence: null,
          was_corrected: false,
        })
        setInvoice(saved.invoice)
        setNotice(t('matching.order_no_saved', { value: pendingOrderNo }))
        setPendingOrderNo(null)
        setStep('confirm_match')
        void queryClient.invalidateQueries({ queryKey: ['invoices'] })
        return
      }

      setInvoice(found)
      setNotice(null)
      // An Order No already on file is not read again. Re-reading it could only
      // either agree (no gain) or disagree (a conflict the server refuses), so
      // the second scan costs the matcher time to buy nothing.
      setStep(found.order_no ? 'confirm_match' : 'read_order_no')
    } catch (err) {
      setError(err as ApiError)
    }
  }

  /**
   * An Order No came off an uploaded file. Try to resolve the invoice from it.
   *
   * If some earlier import already booked an invoice against this order, this
   * finishes step 1 outright. If not — the ordinary case while `invoices.order_no`
   * is only ever populated by this feature — the value is held and the operator is
   * asked for the invoice number, which is a smaller ask than re-reading the page.
   */
  async function orderNoFromFile(orderNo: string) {
    setError(null)
    setPendingOrderNo(orderNo)
    try {
      const found = await get<Invoice>(
        `/invoices/lookup?order_no=${encodeURIComponent(orderNo)}`,
      )
      setInvoice(found)
      setResult(null)
      setPendingOrderNo(null)
      setNotice(null)
      setStep('confirm_match')
    } catch {
      // Deliberately not surfaced as an error. Nothing went wrong: the file was
      // read, and the only missing piece is which invoice it belongs to.
      setNotice(t('matching.now_identify'))
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
      <h1 className="text-2xl font-black">{t('matching.title')}</h1>

      {error && (
        <Banner tone={error.isControlPoint ? 'bad' : 'warn'} title={errorText(error).title}>
          {error.hint}
        </Banner>
      )}

      {notice && (
        <Banner tone="info" title={notice}>
          {pendingOrderNo && t('matching.order_no_read', { value: pendingOrderNo })}
        </Banner>
      )}

      {step === 'scan_invoice' && (
        <Card title={t('matching.step1')} subtitle={t('matching.step1_hint')}>
          <CodeCapture
            onInvoiceNumber={(code) => void lookup(code)}
            onOrderNo={(orderNo) => void orderNoFromFile(orderNo)}
          />
          <ManualInvoice onSubmit={(value) => void lookup(value)} />
        </Card>
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
            onClick={() => setStep('confirm_match')}
            disabled={saveOrderNo.isPending}
          >
            {t('matching.skip_order_no')}
          </button>
        </Card>
      )}

      {step === 'confirm_match' && invoice && (
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

          <Card title={t('matching.step3')}>
            <p className="mb-4 text-base">
              {t('matching.fetch_product')}
              quantity above match what you are holding.
            </p>
            <div className="flex gap-3">
              <button type="button" className="btn-ghost flex-1" onClick={restart}>
                {t('matching.doesnt_match')}
              </button>
              <button
                type="button"
                className="btn-success flex-1"
                onClick={() => setStep('scan_badge')}
              >
                {t('matching.it_matches')}
              </button>
            </div>
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
        placeholder="INV-2026-0001"
        value={value}
        onChange={(event) => setValue(event.target.value.toUpperCase())}
        autoCapitalize="characters"
        autoCorrect="off"
        spellCheck={false}
        aria-label={t('matching.invoice_number')}
      />
      <button type="submit" className="btn-primary" disabled={value.trim().length < 3}>
        Find
      </button>
    </form>
  )
}
