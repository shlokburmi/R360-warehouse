import { useEffect, useState, type ReactNode } from 'react'
import { NavLink, useNavigate } from 'react-router-dom'
import { useAuth } from '@/hooks/useAuth'
import { useOnline, usePendingScans } from '@/hooks/useOnline'
import { flushQueue } from '@/lib/offlineQueue'

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

function useDarkMode() {
  const [dark, setDark] = useState(() => {
    const stored = localStorage.getItem('r360-theme')
    // Defaults to dark: PRD §6 asks for a dark mode for outdoor use, and the
    // gate — where the app is used most — is outdoors.
    return stored ? stored === 'dark' : true
  })

  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark)
    localStorage.setItem('r360-theme', dark ? 'dark' : 'light')
  }, [dark])

  return [dark, setDark] as const
}

export function Layout({ children }: { children: ReactNode }) {
  const { me, signOut } = useAuth()
  const navigate = useNavigate()
  const online = useOnline()
  const pending = usePendingScans()
  const [dark, setDark] = useDarkMode()

  const items = NAV.filter((item) => me?.allowed_pages.includes(item.page))

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-30 border-b border-slate-200 bg-white/95 backdrop-blur dark:border-slate-800 dark:bg-slate-900/95">
        <div className="mx-auto flex max-w-5xl items-center gap-3 px-4 py-3">
          <div className="min-w-0 flex-1">
            <p className="truncate text-lg font-black">R360 Warehouse</p>
            <p className="truncate text-sm text-slate-500 dark:text-slate-400">
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

          <button
            type="button"
            onClick={() => setDark(!dark)}
            className="rounded-lg p-2 text-xl"
            aria-label={dark ? 'Switch to light mode' : 'Switch to dark mode'}
          >
            {dark ? '☀' : '☾'}
          </button>

          <button
            type="button"
            onClick={async () => {
              await signOut()
              navigate('/login')
            }}
            className="rounded-lg px-3 py-2 text-base font-semibold text-slate-600 dark:text-slate-300"
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
                      `block whitespace-nowrap rounded-lg px-4 py-2 text-base font-bold ${
                        isActive
                          ? 'bg-blue-600 text-white'
                          : 'text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800'
                      }`
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
