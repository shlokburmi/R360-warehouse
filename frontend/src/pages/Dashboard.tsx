import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Link, useNavigate } from 'react-router-dom'
import { get } from '@/lib/api'
import { Card, EmptyState, Spinner, Stat, StatusChip } from '@/components/ui'
import type { Dashboard } from '@/types'

function waited(seconds: number | null): string {
  if (seconds === null) return '—'
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m`
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

/**
 * PRD §5.8 — Ops Manager dashboard.
 *
 * The tiles are ordered by what blocks someone else: approvals first (a driver
 * is waiting at a gate), then held boxes (an offloader is standing still), then
 * throughput. Volume numbers are last because nobody is blocked by them.
 */
export function DashboardPage() {
  const { t } = useTranslation()
  const navigate = useNavigate()

  const dashboard = useQuery({
    queryKey: ['dashboard'],
    queryFn: () => get<Dashboard>('/dashboard'),
    refetchInterval: 20_000,
  })

  if (dashboard.isLoading) return <Spinner label={t('dashboard.loading')} />
  if (!dashboard.data) return null

  const { counters, activity, open_exceptions: openExceptions } = dashboard.data

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-black">{t('dashboard.title')}</h1>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat
          label={t('dashboard.awaiting_approval')}
          value={counters.pending_approvals}
          tone={counters.sla_breached > 0 ? 'bad' : 'warn'}
          onClick={() => navigate('/approvals')}
        />
        <Stat
          label={t('dashboard.boxes_held')}
          value={counters.held_boxes}
          tone="bad"
          onClick={() => navigate('/exceptions')}
        />
        <Stat
          label={t('dashboard.open_exceptions')}
          value={counters.open_exceptions}
          tone="warn"
          onClick={() => navigate('/exceptions')}
        />
        <Stat label={t('dashboard.trucks_onsite')} value={counters.trucks_onsite} />
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label={t('dashboard.trucks_today')} value={counters.trucks_today} />
        <Stat label={t('dashboard.scans_today')} value={counters.scans_today} />
        <Stat label={t('dashboard.boxes_closed')} value={counters.boxes_closed_today} />
        <Stat label={t('dashboard.sla_breached')} value={counters.sla_breached} tone="bad" />
      </div>

      <Card
        title={t('dashboard.active_trucks')}
        action={
          <Link to="/entries" className="text-base font-semibold text-blue-600">
            {t('dashboard.view_all')}
          </Link>
        }
      >
        {activity.length === 0 ? (
          <EmptyState title={t('dashboard.nothing_onsite')} />
        ) : (
          <ul className="divide-y divide-slate-200 dark:divide-slate-800">
            {activity.map((item) => (
              <li key={item.id} className="flex items-center justify-between gap-3 py-3">
                <div className="min-w-0">
                  <p className="font-bold">
                    {item.vehicle_number}
                    {item.held_boxes > 0 && (
                      <span className="ml-2 chip bg-bad-bg text-bad dark:bg-bad-darkbg dark:text-bad-dark">
                        {t('dashboard.n_held', { count: item.held_boxes })}
                      </span>
                    )}
                  </p>
                  <p className="truncate text-sm text-slate-500 dark:text-slate-400">
                    {item.entry_code} · {item.vendor_name}
                    {item.status === 'pending_approval' &&
                      ` · ${t('dashboard.waiting_for', { time: waited(item.waiting_seconds) })}`}
                  </p>
                </div>
                <StatusChip status={item.status} />
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card
        title={t('dashboard.open_exceptions')}
        action={
          <Link to="/exceptions" className="text-base font-semibold text-blue-600">
            {t('dashboard.manage')}
          </Link>
        }
      >
        {openExceptions.length === 0 ? (
          <EmptyState title={t('dashboard.no_open_exceptions')} />
        ) : (
          <ul className="divide-y divide-slate-200 dark:divide-slate-800">
            {openExceptions.map((exception) => (
              <li key={exception.id} className="flex items-center justify-between gap-3 py-3">
                <div className="min-w-0">
                  <p className="truncate font-bold">{exception.title}</p>
                  <p className="truncate text-sm text-slate-500 dark:text-slate-400">
                    {exception.exception_code} · {exception.vendor_name ?? t('dashboard.no_vendor')}
                  </p>
                </div>
                <StatusChip status={exception.status} />
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  )
}
