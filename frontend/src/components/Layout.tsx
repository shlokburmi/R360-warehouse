import { type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { NavLink, useNavigate } from 'react-router-dom'
import { useAuth } from '@/hooks/useAuth'
import { useOnline, usePendingScans } from '@/hooks/useOnline'
import { flushQueue } from '@/lib/offlineQueue'
import { GradientBackdrop, LanguageToggle, ThemeToggle } from '@/components/ui'
import { cn } from '@/lib/utils'

const NAV: { page: string; to: string; key: string }[] = [
  { page: 'dashboard', to: '/dashboard', key: 'nav.dashboard' },
  { page: 'approvals', to: '/approvals', key: 'nav.approvals' },
  { page: 'gate-entry', to: '/gate-entry', key: 'nav.gate_entry' },
  { page: 'box-counting', to: '/entries', key: 'nav.trucks' },
  { page: 'putaway', to: '/putaway', key: 'nav.putaway' },
  { page: 'invoices', to: '/invoices', key: 'nav.invoices' },
  { page: 'invoice-matching', to: '/invoice-matching', key: 'nav.matching' },
  { page: 'packing', to: '/packing', key: 'nav.packing' },
  { page: 'batches', to: '/batches', key: 'nav.out_scan' },
  { page: 'loading', to: '/loading', key: 'nav.loading' },
  { page: 'pickup', to: '/pickup', key: 'nav.pickup' },
  { page: 'stock', to: '/stock', key: 'nav.stock' },
  { page: 'exceptions', to: '/exceptions', key: 'nav.exceptions' },
  { page: 'reports', to: '/reports', key: 'nav.reports' },
  { page: 'admin', to: '/admin', key: 'nav.staff' },
]

export function Layout({ children }: { children: ReactNode }) {
  const { t } = useTranslation()
  const { me, signOut } = useAuth()
  const navigate = useNavigate()
  const online = useOnline()
  const pending = usePendingScans()

  const items = NAV.filter((item) => me?.allowed_pages.includes(item.page))

  return (
    <div className="relative min-h-screen">
      {/* Mounted once, here, rather than per page — a backdrop that re-animated
          on every navigation would be noticeable in exactly the way a backdrop
          should not be. */}
      <GradientBackdrop />

      <header className="sticky top-0 z-30 border-b border-white/40 bg-white/60 backdrop-blur-xl dark:border-white/10 dark:bg-black/40">
        {/* Wraps rather than compresses. Adding the language switch put six
              controls on this row, and on a 360px phone the identity block was
              being squeezed to a few characters. Letting the controls drop to a
              second line keeps every target thumb-sized, which matters more here
              than a single-line header. */}
          <div className="mx-auto flex max-w-5xl flex-wrap items-center gap-x-2 gap-y-2 px-3 py-3 sm:gap-x-3 sm:px-4">
          <div className="min-w-0 flex-[1_1_60%] sm:flex-1">
            <p className="truncate bg-gradient-to-r from-blue-700 via-purple-600 to-pink-600 bg-clip-text text-lg font-black text-transparent dark:from-blue-300 dark:via-purple-300 dark:to-pink-300">
              {t('app.name')}
            </p>
            <p className="truncate text-sm text-slate-600 dark:text-slate-400">
              {me?.full_name} · {me ? t(`roles.${me.role}`, { defaultValue: me.role_label }) : ''}
            </p>
          </div>

          {/* Connection state is permanently visible, not a transient toast.
              Someone scanning 200 units needs to know at a glance whether their
              work is landing or queuing. */}
          {!online && (
            <span className="chip bg-warn-bg text-warn dark:bg-warn-darkbg dark:text-warn-dark">
              {t('common.offline')}
            </span>
          )}

          {pending > 0 && (
            <button
              type="button"
              onClick={() => void flushQueue()}
              className="chip bg-info-bg text-info dark:bg-info-darkbg dark:text-info-dark"
              title={t('common.sync_now')}
            >
              {t('common.pending_scans', { count: pending })}
            </button>
          )}

          <LanguageToggle />
          <ThemeToggle />

          <button
            type="button"
            onClick={async () => {
              await signOut()
              navigate('/login')
            }}
            className="rounded-xl px-3 py-2 text-base font-semibold text-slate-700 transition-colors hover:bg-white/70 dark:text-slate-300 dark:hover:bg-white/10"
          >
            {t('common.sign_out')}
          </button>
        </div>

        {/* Wraps rather than scrolls. Thirteen destinations do not fit one line
            below about 1100px, and a horizontal scroller clipped the last pill
            mid-word — which looks like a bug, not an affordance. Wrapping costs a
            second row of header on a phone and guarantees nothing is ever cut. */}
        {items.length > 1 && (
          <nav className="mx-auto max-w-5xl px-2 pb-2">
            <ul className="flex flex-wrap gap-1">
              {items.map((item) => (
                <li key={item.to}>
                  <NavLink
                    to={item.to}
                    className={({ isActive }) =>
                      cn('nav-pill', isActive ? 'nav-pill-active' : 'nav-pill-idle')
                    }
                  >
                    {t(item.key)}
                  </NavLink>
                </li>
              ))}
            </ul>
          </nav>
        )}
      </header>

      <main className="mx-auto max-w-5xl space-y-4 px-4 py-5">{children}</main>
    </div>
  )
}
