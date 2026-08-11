import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { get } from '@/lib/api'
import { Card, EmptyState, Spinner } from '@/components/ui'

/**
 * PRD §5.10 — Reports.
 *
 * Every report is CSV-exportable, because the honest answer to "can I get this
 * into Excel" is yes and pretending otherwise just produces screenshots pasted
 * into WhatsApp.
 */
const REPORTS = [
  { key: 'vendor-accuracy', label: 'Vendor accuracy', path: '/reports/vendor-accuracy' },
  { key: 'exception-log', label: 'Exception log', path: '/reports/exception-log' },
  { key: 'gate-register', label: 'Gate register (in)', path: '/reports/gate-register' },
  {
    key: 'outbound-register',
    label: 'Gate register (out)',
    path: '/reports/outbound-register',
  },
  {
    key: 'packer-productivity',
    label: 'Packer productivity',
    path: '/reports/packer-productivity',
  },
  { key: 'daily-activity', label: 'Daily activity', path: '/reports/daily-activity' },
  {
    key: 'operator-productivity',
    label: 'Operator productivity',
    path: '/reports/operator-productivity',
  },
] as const

type ReportKey = (typeof REPORTS)[number]['key']

function toCsv(rows: Record<string, unknown>[]): string {
  if (rows.length === 0) return ''
  const headers = Object.keys(rows[0])

  const escape = (value: unknown) => {
    const text = value === null || value === undefined ? '' : String(value)
    // Quote anything containing a delimiter, a quote or a newline — the note
    // fields on exceptions routinely contain commas.
    return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
  }

  return [
    headers.join(','),
    ...rows.map((row) => headers.map((header) => escape(row[header])).join(',')),
  ].join('\n')
}

function download(filename: string, csv: string) {
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  URL.revokeObjectURL(url)
}

export function ReportsPage() {
  const [active, setActive] = useState<ReportKey>('vendor-accuracy')
  const report = REPORTS.find((r) => r.key === active)!

  const data = useQuery({
    queryKey: ['report', active],
    queryFn: () => get<Record<string, unknown> | Record<string, unknown>[]>(report.path),
  })

  const rows = Array.isArray(data.data) ? data.data : data.data ? [data.data] : []

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-black">Reports</h1>

      <div className="flex flex-wrap gap-2">
        {REPORTS.map((item) => (
          <button
            key={item.key}
            type="button"
            onClick={() => setActive(item.key)}
            className={`rounded-xl px-4 py-2 text-base font-bold ${
              active === item.key
                ? 'bg-blue-600 text-white'
                : 'border-2 border-slate-300 dark:border-slate-700'
            }`}
          >
            {item.label}
          </button>
        ))}
      </div>

      <Card
        title={report.label}
        action={
          rows.length > 0 && (
            <button
              type="button"
              className="btn-ghost"
              onClick={() =>
                download(
                  `${report.key}-${new Date().toISOString().slice(0, 10)}.csv`,
                  toCsv(rows),
                )
              }
            >
              Export CSV
            </button>
          )
        }
      >
        {data.isLoading ? (
          <Spinner />
        ) : rows.length === 0 ? (
          <EmptyState title="No data for this report yet" />
        ) : (
          <div className="-mx-5 overflow-x-auto px-5">
            <table className="w-full text-left text-base">
              <thead>
                <tr className="border-b-2 border-slate-200 dark:border-slate-800">
                  {Object.keys(rows[0]).map((header) => (
                    <th
                      key={header}
                      className="whitespace-nowrap px-2 py-2 text-sm font-bold uppercase tracking-wide text-slate-500"
                    >
                      {header.replace(/_/g, ' ')}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((row, index) => (
                  <tr
                    key={index}
                    className="border-b border-slate-100 dark:border-slate-800/60"
                  >
                    {Object.keys(rows[0]).map((header) => (
                      <td key={header} className="whitespace-nowrap px-2 py-2">
                        {row[header] === null || row[header] === undefined
                          ? '—'
                          : String(row[header])}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  )
}
