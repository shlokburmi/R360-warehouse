import { useState, type FormEvent } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { useAuth } from '@/hooks/useAuth'
import { Banner } from '@/components/ui'

export function LoginPage() {
  const { session, signIn } = useAuth()
  const location = useLocation()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  if (session) {
    const from = (location.state as { from?: { pathname: string } })?.from?.pathname ?? '/'
    return <Navigate to={from} replace />
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await signIn(email, password)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <div className="w-full max-w-md">
        <div className="mb-6 text-center">
          <h1 className="text-3xl font-black">R360 Warehouse</h1>
          <p className="mt-1 text-slate-500 dark:text-slate-400">Sign in to continue</p>
        </div>

        <form onSubmit={submit} className="card space-y-4">
          {error && <Banner tone="bad" title={error} />}

          <div>
            <label htmlFor="email" className="label">
              Email
            </label>
            <input
              id="email"
              type="email"
              className="input"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              autoComplete="username"
              autoCapitalize="none"
              required
            />
          </div>

          <div>
            <label htmlFor="password" className="label">
              Password
            </label>
            <input
              id="password"
              type="password"
              className="input"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete="current-password"
              required
            />
          </div>

          <button type="submit" className="btn-primary w-full" disabled={busy}>
            {busy ? 'Signing in…' : 'Sign In'}
          </button>
        </form>

        {import.meta.env.DEV && (
          <div className="mt-4 rounded-xl border border-slate-200 p-4 text-sm dark:border-slate-800">
            <p className="font-bold">Demo accounts (local only)</p>
            <p className="mt-1 text-slate-500 dark:text-slate-400">
              guard@r360.local · boopathi@r360.local · offload@r360.local ·
              inbound@r360.local · admin@r360.local
              <br />
              Password: <code>Warehouse@123</code>
            </p>
          </div>
        )}
      </div>
    </div>
  )
}
