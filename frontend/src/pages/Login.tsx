import { useState, type FormEvent } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { useAuth } from '@/hooks/useAuth'
import {
  AnimatedGroup,
  Banner,
  GradientBackdrop,
  ThemeToggle,
  heroTransitionVariants,
} from '@/components/ui'

/**
 * The one screen that gets the full hero treatment.
 *
 * Everywhere else in this app someone is mid-task and the gradient is a
 * backdrop; here it is the subject. This is also the only screen where a 1.5s
 * staggered reveal costs nothing — nobody is standing at a gate waiting on it,
 * and it is the first thing a new user ever sees.
 *
 * The auth logic below is unchanged from before the restyle. The form is still
 * a plain uncontrolled-ish submit with the same three states (idle, busy,
 * error), because the sign-in path is the last place worth introducing new
 * moving parts for a visual refresh.
 */
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
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden p-4">
      <GradientBackdrop />

      {/* Login has no header, so the toggle floats. Someone starting a night
          shift should not have to sign in under a white screen first. */}
      <ThemeToggle className="absolute right-4 top-4" />

      <AnimatedGroup
        variants={{
          container: {
            visible: { transition: { staggerChildren: 0.08, delayChildren: 0.2 } },
          },
          ...heroTransitionVariants,
        }}
        className="w-full max-w-md"
      >
        <div className="mb-8 text-center">
          <h1 className="bg-gradient-to-r from-blue-700 via-purple-600 to-pink-600 bg-clip-text text-4xl font-black tracking-tight text-transparent sm:text-5xl dark:from-blue-300 dark:via-purple-300 dark:to-pink-300">
            R360 Warehouse
          </h1>
          {/* Says what the system is for rather than "Sign in to continue".
              A guard on their first shift learns more from one line here than
              from the label on the button below it. */}
          <p className="mx-auto mt-3 max-w-sm text-lg text-slate-700 dark:text-slate-300">
            Gate to gate, every box counted and attributable.
          </p>
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

          {/* The reference design's gradient-ringed CTA. The ring is a wrapper
              rather than a border on the button itself, because a gradient
              cannot be a border-color — it has to be a sheet with the button
              inset over it. */}
          <div className="btn-gradient-ring">
            <button type="submit" className="btn-primary w-full" disabled={busy}>
              {busy ? 'Signing in…' : 'Sign In'}
            </button>
          </div>
        </form>

        {import.meta.env.DEV && (
          <div className="card mt-4 text-sm">
            <p className="font-bold">Demo accounts (local only)</p>
            <p className="mt-1 text-slate-600 dark:text-slate-400">
              guard@r360.local · boopathi@r360.local · offload@r360.local ·
              inbound@r360.local · admin@r360.local
              <br />
              Password: <code>Warehouse@123</code>
            </p>
          </div>
        )}
      </AnimatedGroup>
    </div>
  )
}
