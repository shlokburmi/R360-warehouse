import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, get, post } from '@/lib/api'
import { useErrorText } from '@/hooks/useErrorText'
import { Banner, Card, Field, Spinner } from '@/components/ui'
import type { Invoice, PurchaseOrder, PurchaseOrderLine } from '@/types'

/**
 * PRD §5.4 (extended) — Admin books an invoice against a received PO line,
 * from the dashboard.
 */
export function InvoicesPage() {
  const { t } = useTranslation()
  const errorText = useErrorText()
  const queryClient = useQueryClient()

  const [poId, setPoId] = useState('')
  const [lineId, setLineId] = useState('')
  const [invoiceNumber, setInvoiceNumber] = useState('')
  const [units, setUnits] = useState('')
  const [customerName, setCustomerName] = useState('')
  const [error, setError] = useState<ApiError | null>(null)

  const purchaseOrders = useQuery({
    queryKey: ['purchase-orders', 'open'],
    queryFn: () => get<PurchaseOrder[]>('/purchase-orders?open_only=false'),
  })

  const lines = useQuery({
    queryKey: ['po-lines', poId],
    queryFn: () => get<PurchaseOrderLine[]>(`/purchase-orders/${poId}/lines`),
    enabled: Boolean(poId),
  })

  const invoices = useQuery({
    queryKey: ['invoices', 'recent'],
    queryFn: () => get<Invoice[]>('/invoices'),
  })

  const selectedLine = lines.data?.find((l) => l.id === lineId)

  function resetForm() {
    setPoId('')
    setLineId('')
    setInvoiceNumber('')
    setUnits('')
    setCustomerName('')
  }

  const createInvoice = useMutation({
    mutationFn: () =>
      post<Invoice>('/invoices', {
        invoice_number: invoiceNumber,
        purchase_order_line_id: lineId,
        units: Number(units),
        customer_name: customerName || null,
      }),
    onSuccess: () => {
      setError(null)
      resetForm()
      void queryClient.invalidateQueries({ queryKey: ['invoices'] })
    },
    onError: (err) => setError(err as ApiError),
  })

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-black">{t('invoices.title')}</h1>

      {error && (
        <Banner tone={error.isControlPoint ? 'bad' : 'warn'} title={errorText(error).title}>
          {error.hint}
        </Banner>
      )}

      <Card title={t('invoices.new')} subtitle={t('invoices.new_hint')}>
        <form
          className="space-y-3"
          onSubmit={(event) => {
            event.preventDefault()
            createInvoice.mutate()
          }}
        >
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
              <select
                className="input"
                value={lineId}
                onChange={(event) => setLineId(event.target.value)}
              >
                <option value="">{t('invoices.choose_product')}</option>
                {lines.data?.map((line) => (
                  <option key={line.id} value={line.id}>
                    {line.sku} — {line.description ?? ''}
                  </option>
                ))}
              </select>
            </Field>
          )}

          <Field label={t('invoices.invoice_number')} required>
            <input
              className="input font-mono uppercase"
              placeholder="INV-2026-0001"
              value={invoiceNumber}
              onChange={(event) => setInvoiceNumber(event.target.value.toUpperCase())}
              autoCapitalize="characters"
              autoCorrect="off"
              spellCheck={false}
              required
            />
          </Field>

          <Field label={t('invoices.units')} required>
            <input
              className="input"
              type="number"
              inputMode="numeric"
              min={1}
              value={units}
              onChange={(event) => setUnits(event.target.value)}
              required
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
            <p className="text-sm text-slate-500 dark:text-slate-400">
              {t('invoices.received_so_far', {
                received: selectedLine.received_units,
                expected: selectedLine.expected_units,
              })}
            </p>
          )}

          <button
            type="submit"
            className="btn-primary w-full"
            disabled={
              !poId ||
              !lineId ||
              invoiceNumber.trim().length < 3 ||
              !units ||
              Number(units) < 1 ||
              createInvoice.isPending
            }
          >
            {createInvoice.isPending ? t('invoices.creating') : t('invoices.create')}
          </button>
        </form>
      </Card>

      <Card title={t('invoices.recent')}>
        {invoices.isLoading ? (
          <Spinner />
        ) : invoices.data && invoices.data.length > 0 ? (
          <ul className="divide-y divide-slate-200 dark:divide-slate-800">
            {invoices.data.slice(0, 20).map((invoice) => (
              <li
                key={invoice.invoice_id}
                className="flex items-center justify-between gap-3 py-3"
              >
                <div className="min-w-0">
                  <p className="font-mono font-bold">{invoice.invoice_number}</p>
                  <p className="truncate text-sm text-slate-500 dark:text-slate-400">
                    {invoice.sku} · {invoice.units} units
                    {invoice.customer_name ? ` · ${invoice.customer_name}` : ''}
                  </p>
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-base text-slate-500">{t('invoices.none_yet')}</p>
        )}
      </Card>
    </div>
  )
}
