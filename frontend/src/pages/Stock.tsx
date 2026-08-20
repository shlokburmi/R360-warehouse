import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import { get } from '@/lib/api'
import { Card, EmptyState, Spinner } from '@/components/ui'
import type { StockRow } from '@/types'

const ZONES = [
  { code: '', labelKey: 'stock.zone_all' },
  { code: 'A', labelKey: 'stock.zone_a' },
  { code: 'B', labelKey: 'stock.zone_b' },
  { code: 'C', labelKey: 'stock.zone_c' },
  { code: 'Q', labelKey: 'stock.zone_q' },
]

/**
 * Where things are (Phase 2).
 *
 * Grouped by SKU rather than by rack, because the question people actually ask
 * is "where are the powerbanks", not "what is in bin 4". Quarantine rows are
 * marked so nobody picks from them by accident.
 */
export function StockPage() {
  const { t } = useTranslation()
  const [sku, setSku] = useState('')
  const [zone, setZone] = useState('')

  const stock = useQuery({
    queryKey: ['stock', sku, zone],
    queryFn: () => {
      const params = new URLSearchParams()
      if (sku.trim()) params.set('sku', sku.trim())
      if (zone) params.set('zone', zone)
      const qs = params.toString()
      return get<StockRow[]>(`/stock${qs ? `?${qs}` : ''}`)
    },
  })

  const bySku = new Map<string, StockRow[]>()
  for (const row of stock.data ?? []) {
    const list = bySku.get(row.sku)
    if (list) list.push(row)
    else bySku.set(row.sku, [row])
  }

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-black">{t('stock.title')}</h1>

      <Card>
        <div className="flex flex-wrap gap-3">
          <input
            className="input flex-1 font-mono uppercase"
            placeholder={t('stock.filter_by_sku')}
            value={sku}
            onChange={(event) => setSku(event.target.value.toUpperCase())}
            autoCapitalize="characters"
            autoCorrect="off"
            spellCheck={false}
            aria-label={t('stock.sku')}
          />
          <select
            className="input sm:w-56"
            value={zone}
            onChange={(event) => setZone(event.target.value)}
            aria-label={t('stock.zone')}
          >
            {ZONES.map((z) => (
              <option key={z.code} value={z.code}>
                {t(z.labelKey)}
              </option>
            ))}
          </select>
        </div>
      </Card>

      {stock.isLoading ? (
        <Spinner />
      ) : bySku.size === 0 ? (
        <EmptyState
          title={t('stock.nothing_yet')}
          hint={t('stock.nothing_yet_hint')}
        />
      ) : (
        [...bySku.entries()].map(([skuCode, rows]) => {
          const total = rows.reduce((sum, r) => sum + r.units, 0)
          return (
            <Card
              key={skuCode}
              title={skuCode}
              subtitle={rows[0].description}
              action={
                <span className="text-2xl font-black tabular-nums">{total}</span>
              }
            >
              <ul className="divide-y divide-slate-200 dark:divide-slate-800">
                {rows.map((row) => (
                  <li
                    key={row.location_code}
                    className="flex items-center justify-between gap-3 py-2"
                  >
                    <div className="min-w-0">
                      <p className="font-mono font-bold">{row.location_code}</p>
                      {row.last_movement && (
                        <p className="text-sm text-slate-500 dark:text-slate-400">
                          {new Date(row.last_movement).toLocaleDateString()}
                        </p>
                      )}
                    </div>
                    <span
                      className={`chip ${
                        row.is_quarantine
                          ? 'bg-bad-bg text-bad dark:bg-bad-darkbg dark:text-bad-dark'
                          : 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300'
                      }`}
                    >
                      {row.units} {row.is_quarantine ? t('stock.quarantined') : t('stock.units')}
                    </span>
                  </li>
                ))}
              </ul>
            </Card>
          )
        })
      )}
    </div>
  )
}
