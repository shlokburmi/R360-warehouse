import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { get, post } from '@/lib/api'
import { useRealtimeInvalidate } from '@/hooks/useRealtimeInvalidate'
import { cn } from '@/lib/utils'
import type { AppNotification } from '@/types'

/**
 * PRD §9 real-time alerts, the in-app half — the backend has always written
 * these and emailed them (services/notifications.py); nothing in the
 * frontend read them back until this session. Now pushed via Supabase
 * Realtime on the `notifications` table (0026_realtime_publication.sql) —
 * the poll interval below is a long fallback, not the primary mechanism.
 *
 * Shown to every signed-in user, not just ops_manager/admin: those two are
 * the only roles notify_ops()/notify_admin() target with a *role-wide*
 * alert, but decide_entry (services/gate.py) already sends a *personal*
 * notification (recipient_id) to whichever guard submitted an entry, once
 * Ops decides it — any role can be on the receiving end of one of those.
 */
export function NotificationBell() {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const panelRef = useRef<HTMLDivElement>(null)

  const notifications = useQuery({
    queryKey: ['notifications'],
    queryFn: () => get<AppNotification[]>('/notifications'),
    refetchInterval: 60_000,
  })

  useRealtimeInvalidate('notifications', [['notifications']])

  const markRead = useMutation({
    mutationFn: (id: string) => post(`/notifications/${id}/read`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['notifications'] }),
  })

  // Close on a click outside the button+panel, same pattern a native <select>
  // gives you for free — this component has to do it by hand.
  useEffect(() => {
    if (!open) return
    function onClick(event: MouseEvent) {
      if (panelRef.current && !panelRef.current.contains(event.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [open])

  const items = notifications.data ?? []
  const count = items.length

  return (
    <div className="relative shrink-0" ref={panelRef}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-label={t('notifications.title')}
        aria-expanded={open}
        className="relative rounded-xl px-2.5 py-2 text-lg leading-none text-slate-600 transition-colors hover:bg-white/70 dark:text-slate-300 dark:hover:bg-white/10"
      >
        <span aria-hidden>🔔</span>
        {count > 0 && (
          <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-bad px-1 text-[10px] font-bold leading-none text-white">
            {count > 9 ? '9+' : count}
          </span>
        )}
      </button>

      {open && (
        <div
          className="absolute right-0 top-full z-40 mt-2 w-80 max-w-[calc(100vw-2rem)] overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-xl dark:border-white/10 dark:bg-slate-900"
        >
          <p className="border-b border-slate-200 px-4 py-3 text-sm font-bold uppercase tracking-wide text-slate-500 dark:border-white/10 dark:text-slate-400">
            {t('notifications.title')}
          </p>

          <ul className="max-h-96 overflow-y-auto">
            {items.length === 0 && (
              <li className="px-4 py-6 text-center text-sm text-slate-500 dark:text-slate-400">
                {t('notifications.empty')}
              </li>
            )}
            {items.map((item) => (
              <li
                key={item.id}
                className="flex items-start gap-1 border-b border-slate-100 px-4 py-3 last:border-0 dark:border-white/5"
              >
                <button
                  type="button"
                  onClick={() => markRead.mutate(item.id)}
                  disabled={markRead.isPending}
                  className={cn(
                    'min-w-0 flex-1 text-left transition-opacity',
                    markRead.isPending && 'opacity-50',
                  )}
                >
                  <p className="text-sm font-bold text-slate-900 dark:text-slate-100">
                    {item.title}
                  </p>
                  <p className="mt-0.5 text-sm text-slate-600 dark:text-slate-400">{item.body}</p>
                  <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">
                    {new Date(item.created_at).toLocaleTimeString([], {
                      hour: '2-digit',
                      minute: '2-digit',
                    })}{' '}
                    · {t('notifications.tap_to_dismiss')}
                  </p>
                </button>
                <button
                  type="button"
                  onClick={() => markRead.mutate(item.id)}
                  disabled={markRead.isPending}
                  aria-label={t('notifications.dismiss')}
                  className="shrink-0 rounded-full p-1 text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-600 dark:text-slate-500 dark:hover:bg-white/10 dark:hover:text-slate-300"
                >
                  <span aria-hidden>✕</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
