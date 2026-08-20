import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { ApiError, get, post } from '@/lib/api'
import { useAuth } from '@/hooks/useAuth'
import { Banner, Card, EmptyState, Spinner, StatusChip } from '@/components/ui'
import type { GateEntry } from '@/types'

/**
 * The trucks currently on site, and what each one needs next.
 *
 * Rather than a generic list with a detail page, each row states the single
 * next action for the signed-in role. On a warehouse floor the useful question
 * is never "what is the state of this entity" — it is "what do I do now".
 */
export function EntriesPage() {
  const { t } = useTranslation()
  const { me } = useAuth()
  const queryClient = useQueryClient()

  const entries = useQuery({
    queryKey: ['entries'],
    queryFn: () =>
      get<GateEntry[]>(
        '/gate/entries?status=approved&status=inside&status=counting&status=box_verified' +
          '&status=offloading&status=offloaded&status=pending_approval',
      ),
    refetchInterval: 20_000,
  })

  const admit = useMutation({
    mutationFn: (id: string) => post<GateEntry>(`/gate/entries/${id}/admit`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['entries'] }),
  })

  if (entries.isLoading) return <Spinner label={t('entries.loading')} />

  const isGuard = me?.role === 'security_guard'
  const isOps = me?.role === 'ops_manager' || me?.role === 'admin'
  const isOffloader = me?.role === 'offloading'
  const isInbound = me?.role === 'inbound'

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-black">{t('entries.title')}</h1>

      {admit.isError && (
        <Banner tone="bad" title={(admit.error as ApiError).message}>
          {(admit.error as ApiError).hint}
        </Banner>
      )}

      {entries.data?.length === 0 && (
        <EmptyState title={t('entries.none')} hint={t('entries.none_hint')} />
      )}

      {entries.data?.map((entry) => (
        <Card
          key={entry.id}
          title={entry.vehicle_number}
          subtitle={`${entry.entry_code} · ${entry.vendor_name}${
            entry.po_number ? ` · ${entry.po_number}` : ''
          }`}
          action={<StatusChip status={entry.status} />}
        >
          {entry.declared_box_count !== null && (
            <p className="mb-3 text-base text-slate-600 dark:text-slate-400">
              {entry.scanned_box_count} of {entry.declared_box_count} boxes scanned
            </p>
          )}

          <div className="flex flex-wrap gap-3">
            {entry.status === 'pending_approval' && (
              <span className="text-base text-slate-500 dark:text-slate-400">
                {t('entries.awaiting_ops')}
              </span>
            )}

            {entry.status === 'approved' && (isGuard || isOps) && (
              <button
                type="button"
                className="btn-success flex-1"
                disabled={admit.isPending}
                onClick={() => admit.mutate(entry.id)}
              >
                {t('entries.open_gate_in')}
              </button>
            )}

            {['inside', 'counting'].includes(entry.status) && (isGuard || isOps) && (
              <Link to={`/entries/${entry.id}/boxes`} className="btn-primary flex-1">
                {t('entries.count_boxes')}
              </Link>
            )}

            {['box_verified', 'offloading'].includes(entry.status) &&
              (isOffloader || isOps) && (
                <Link to={`/entries/${entry.id}/units`} className="btn-primary flex-1">
                  {t('entries.scan_units')}
                </Link>
              )}

            {entry.status === 'offloaded' && (isInbound || isOps) && (
              <Link to={`/entries/${entry.id}/reconciliation`} className="btn-primary flex-1">
                {t('entries.verify_counts')}
              </Link>
            )}

            {isOps && ['inside', 'counting', 'box_verified', 'offloading'].includes(entry.status) && (
              <Link to={`/entries/${entry.id}/boxes`} className="btn-ghost">
                {t('entries.stickers')}
              </Link>
            )}
          </div>
        </Card>
      ))}
    </div>
  )
}
