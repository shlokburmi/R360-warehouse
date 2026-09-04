import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ApiError, get, post } from '@/lib/api'
import { useErrorText } from '@/hooks/useErrorText'
import { BadgeScan } from '@/components/BadgeScan'
import { OrderNoScanner, type OrderNoReading } from '@/components/OrderNoScanner'
import { Banner, Card } from '@/components/ui'
import type { AssignResult, Invoice } from '@/types'

/**
 * PRD §5.4/§7 — Invoice matching, done by whichever Packer physically has the
 * invoice (0035/0036 — this used to be a separate Invoice Matcher role's job
 * with its own product/quantity verification; a Packer now does this and the
 * packing step, on different invoices, since CONTROL POINT 5 is enforced by
 * identity — assigner != packer — not by which role holds the scanner, and
 * there is no more product/quantity tracking to verify at all).
 *
 * Three steps: scan the physical invoice (OCR reads the Order No, creating
 * the invoice from it if this is the first time it's been seen), hand it to
 * a different packing lady by scanning her badge, done. What's actually
 * inside the carton is Admin's separate ERP's concern, not this app's.
 */
type Step = 'scan' | 'assign' | 'done'

export function InvoiceMatchingPage() {
  const { t } = useTranslation()
  const errorText = useErrorText()
  const queryClient = useQueryClient()

  const [step, setStep] = useState<Step>('scan')
  const [invoice, setInvoice] = useState<Invoice | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [result, setResult] = useState<AssignResult | null>(null)
  const [scanNotice, setScanNotice] = useState<string | null>(null)

  function restart() {
    setStep('scan')
    setInvoice(null)
    setError(null)
    setResult(null)
    setScanNotice(null)
  }

  const createInvoice = useMutation({
    mutationFn: (reading: OrderNoReading) =>
      post<Invoice>('/invoices/from-order-no', {
        order_no: reading.order_no,
        raw_text: reading.raw_text,
        confidence: reading.confidence,
        was_corrected: reading.was_corrected,
        source: reading.source,
      }),
    onSuccess: (created) => {
      setError(null)
      setInvoice(created)
      setStep('assign')
      void queryClient.invalidateQueries({ queryKey: ['invoices'] })
    },
    onError: (err) => setError(err as ApiError),
  })

  /** The Order No just came off a camera or uploaded photo. Find the invoice
   * it belongs to, or — the ordinary case, since every invoice starts life as
   * exactly this scan — create one from it. */
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
      setStep(found.stage === 'open' ? 'assign' : 'done')
    } catch (err) {
      const apiErr = err as ApiError
      if (apiErr.code === 'unknown_order_no') {
        // The one expected failure — this Order No simply has no invoice
        // yet, the normal case. Create one from exactly what was just read.
        createInvoice.mutate(reading)
      } else {
        // Anything else (a network drop, an expired session, a server
        // error, or `ambiguous_order_no` — several invoices already share
        // this Order No, which a human has to resolve) is a real failure,
        // not "not found yet". Treating it as the latter used to silently
        // attempt to create a duplicate invoice instead of showing what
        // actually went wrong.
        setError(apiErr)
      }
    }
  }

  const assign = useMutation({
    mutationFn: (badgeCode: string) =>
      post<AssignResult>('/invoices/assign', {
        invoice_number: invoice?.invoice_number,
        badge_code: badgeCode,
      }),
    onSuccess: (data) => {
      setResult(data)
      setError(null)
      setStep('done')
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

      {step === 'scan' && (
        <Card title={t('matching.step1')} subtitle={t('matching.step1_hint')}>
          <OrderNoScanner
            busy={createInvoice.isPending}
            onConfirm={(reading) => void handleOrderNoScan(reading)}
          />
          {scanNotice && (
            <p className="mt-2 text-sm font-semibold text-warn dark:text-warn-dark">
              {scanNotice}
            </p>
          )}
        </Card>
      )}

      {step === 'assign' && invoice && (
        <Card title={invoice.invoice_number} subtitle={t('matching.step2')}>
          <BadgeScan
            label={t('matching.scan_packer_badge')}
            busy={assign.isPending}
            onBadge={(code) => assign.mutate(code)}
          />
          <button type="button" className="btn-ghost mt-3 w-full" onClick={restart}>
            {t('common.cancel')}
          </button>
        </Card>
      )}

      {step === 'done' && (
        <>
          <Banner tone="ok" title={t('matching.assigned_done')}>
            {result
              ? `${result.invoice.invoice_number} assigned to ${result.assigned_to.full_name}.`
              : invoice &&
                `${invoice.invoice_number} — ${t('matching.already_assigned', {
                  name: invoice.assigned_to_name ?? invoice.packed_by_name ?? '',
                })}`}
          </Banner>
          <button type="button" className="btn-primary w-full" onClick={restart}>
            {t('matching.next_invoice')}
          </button>
        </>
      )}
    </div>
  )
}
