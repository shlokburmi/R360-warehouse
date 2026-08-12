import { type ReactNode } from 'react'
import { NavLink, useNavigate } from 'react-router-dom'
import { useAuth } from '@/hooks/useAuth'
import { useOnline, usePendingScans } from '@/hooks/useOnline'
import { flushQueue } from '@/lib/offlineQueue'
import { GradientBackdrop, ThemeToggle } from '@/components/ui'
import { cn } from '@/lib/utils'

const NAV: { page: string; to: string; label: string }[] = [
  { page: 'dashboard', to: '/dashboard', label: 'Dashboard' },
  { page: 'approvals', to: '/approvals', label: 'Approvals' },
  { page: 'gate-entry', to: '/gate-entry', label: 'Gate Entry' },
  { page: 'box-counting', to: '/entries', label: 'Trucks' },
  { page: 'putaway', to: '/putaway', label: 'Putaway' },
  { page: 'invoice-matching', to: '/invoice-matching', label: 'Matching' },
  { page: 'packing', to: '/packing', label: 'Packing' },
  { page: 'batches', to: '/batches', label: 'Out-Scan' },
  { page: 'pickup', to: '/pickup', label: 'Pickup' },
  { page: 'stock', to: '/stock', label: 'Stock' },
  { page: 'exceptions', to: '/exceptions', label: 'Exceptions' },
  { page: 'reports', to: '/reports', label: 'Reports' },
  { page: 'admin', to: '/admin', label: 'Staff' },
]

export function Layout({ children }: { children: ReactNode }) {
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
        <div className="mx-auto flex max-w-5xl items-center gap-3 px-4 py-3">
          <div className="min-w-0 flex-1">
            <p className="truncate bg-gradient-to-r from-blue-700 via-purple-600 to-pink-600 bg-clip-text text-lg font-black text-transparent dark:from-blue-300 dark:via-purple-300 dark:to-pink-300">
              R360 Warehouse
            </p>
            <p className="truncate text-sm text-slate-600 dark:text-slate-400">
              {me?.full_name} · {me?.role_label}
            </p>
          </div>

          {/* Connection state is permanently visible, not a transient toast.
              Someone scanning 200 units needs to know at a glance whether their
              work is landing or queuing. */}
          {!online && (
            <span className="chip bg-warn-bg text-warn dark:bg-warn-darkbg dark:text-warn-dark">
              Offline
            </span>
          )}

          {pending > 0 && (
            <button
              type="button"
              onClick={() => void flushQueue()}
              className="chip bg-info-bg text-info dark:bg-info-darkbg dark:text-info-dark"
              title="Tap to retry sync"
            >
              {pending} to sync
            </button>
          )}

          <ThemeToggle />

          <button
            type="button"
            onClick={async () => {
              await signOut()
              navigate('/login')
            }}
            className="rounded-xl px-3 py-2 text-base font-semibold text-slate-700 transition-colors hover:bg-white/70 dark:text-slate-300 dark:hover:bg-white/10"
          >
            Sign out
          </button>
        </div>

        {items.length > 1 && (
          <nav className="mx-auto max-w-5xl overflow-x-auto px-2 pb-2">
            <ul className="flex gap-1">
              {items.map((item) => (
                <li key={item.to}>
                  <NavLink
                    to={item.to}
                    className={({ isActive }) =>
                      cn('nav-pill', isActive ? 'nav-pill-active' : 'nav-pill-idle')
                    }
                  >
                    {item.label}
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
