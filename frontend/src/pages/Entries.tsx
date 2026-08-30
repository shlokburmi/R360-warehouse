import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { ApiError, get, post } from '@/lib/api'
import { useAuth } from '@/hooks/useAuth'
import { useRealtimeInvalidate } from '@/hooks/useRealtimeInvalidate'
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
  const navigate = useNavigate()

  const entries = useQuery({
    queryKey: ['entries'],
    queryFn: () =>
      get<GateEntry[]>(
        '/gate/entries?status=approved&status=inside&status=counting&status=box_verified' +
          '&status=offloading&status=offloaded&status=pending_approval',
      ),
    refetchInterval: 20_000,
  })

  // This is what makes an Ops decision appear here without the guard waiting
  // out the 20s timer.
  useRealtimeInvalidate('gate_entries', [['entries']])
  // "X of Y boxes scanned" changes as boxes are scanned in, which updates
  // `boxes`/`stickers`, not `gate_entries` — needs its own subscription to
  // move live instead of waiting on the 20s poll.
  useRealtimeInvalidate('boxes', [['entries']])

  const admit = useMutation({
    mutationFn: (id: string) => post<GateEntry>(`/gate/entries/${id}/admit`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['entries'] }),
  })

  const [cancelling, setCancelling] = useState<string | null>(null)
  const [cancelReason, setCancelReason] = useState('')

  const cancel = useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: string }) =>
      post<GateEntry>(`/gate/entries/${id}/cancel`, { reason }),
    onSuccess: () => {
      setCancelling(null)
      setCancelReason('')
      void queryClient.invalidateQueries({ queryKey: ['entries'] })
    },
  })

  if (entries.isLoading) return <Spinner label={t('entries.loading')} />

  const isGuard = me?.role === 'security_guard'
  const isOps = me?.role === 'admin' || me?.role === 'ops_manager'
  const isOffloader = me?.role === 'offloading'
  // Box/unit sticker scanning moved to packer in the role split — offloading
  // now only does reconciliation (CONTROL POINT 4).
  const isPacker = me?.role === 'packer'

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
          // The whole card is a "view details" target — the specific action
          // links/buttons inside stop propagation so their own destination
          // wins instead of this one.
          className="cursor-pointer"
          onClick={() => navigate(`/entries/${entry.id}/boxes`)}
        >
          {entry.declared_box_count !== null && (
            <p className="mb-3 text-base text-slate-600 dark:text-slate-400">
              {entry.scanned_box_count} of {entry.declared_box_count} boxes scanned
            </p>
          )}

          <div className="flex flex-wrap gap-3" onClick={(event) => event.stopPropagation()}>
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

            {['inside', 'counting'].includes(entry.status) && (isGuard || isOps || isPacker) && (
              <Link to={`/entries/${entry.id}/boxes`} className="btn-primary flex-1">
                {t('entries.count_boxes')}
              </Link>
            )}

            {['box_verified', 'offloading'].includes(entry.status) &&
              (isPacker || isOps) && (
                <Link to={`/entries/${entry.id}/units`} className="btn-primary flex-1">
                  {t('entries.scan_units')}
                </Link>
              )}

            {entry.status === 'offloaded' && (isOffloader || isOps) && (
              <Link to={`/entries/${entry.id}/reconciliation`} className="btn-primary flex-1">
                {t('entries.verify_counts')}
              </Link>
            )}

            {isOps && ['inside', 'counting', 'box_verified', 'offloading'].includes(entry.status) && (
              <Link to={`/entries/${entry.id}/boxes`} className="btn-ghost">
                {t('entries.stickers')}
              </Link>
            )}

            {isOps &&
              ['approved', 'inside', 'counting', 'box_verified', 'offloading'].includes(
                entry.status,
              ) &&
              cancelling !== entry.id && (
                <button
                  type="button"
                  className="btn-ghost"
                  onClick={() => {
                    setCancelling(entry.id)
                    setCancelReason('')
                  }}
                >
                  {t('entries.cancel')}
                </button>
              )}
          </div>

          {cancelling === entry.id && (
            <div
              className="mt-3 space-y-3 rounded-xl border-2 border-dashed border-slate-300 p-3 dark:border-slate-700"
              onClick={(event) => event.stopPropagation()}
            >
              <textarea
                className="input"
                rows={2}
                value={cancelReason}
                onChange={(event) => setCancelReason(event.target.value)}
                placeholder={t('entries.cancel_reason')}
              />
              <div className="flex gap-3">
                <button
                  type="button"
                  className="btn-ghost flex-1"
                  onClick={() => setCancelling(null)}
                >
                  {t('common.cancel')}
                </button>
                <button
                  type="button"
                  className="btn-danger flex-1"
                  disabled={cancelReason.trim().length < 3 || cancel.isPending}
                  onClick={() => cancel.mutate({ id: entry.id, reason: cancelReason.trim() })}
                >
                  {t('entries.confirm_cancel')}
                </button>
              </div>
            </div>
          )}
        </Card>
      ))}
    </div>
  )
}
