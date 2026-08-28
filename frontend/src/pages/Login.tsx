import { useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Navigate, useLocation } from 'react-router-dom'
import { useAuth } from '@/hooks/useAuth'
import {
  AnimatedGroup,
  Banner,
  GradientBackdrop,
  LanguageToggle,
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
  const { t } = useTranslation()
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
      <ThemeToggle className="absolute right-3 top-3 sm:right-4 sm:top-4" />

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
            {t('app.name')}
          </h1>
          {/* Says what the system is for rather than "Sign in to continue".
              A guard on their first shift learns more from one line here than
              from the label on the button below it. */}
          <p className="mx-auto mt-3 max-w-sm text-lg text-slate-700 dark:text-slate-300">
            {t('login.hero_tagline')}
          </p>
        </div>

        {/* Above the form, not below it and not in a menu. Someone who cannot
            read English cannot be asked to read an English label to find this,
            so it is the first interactive thing on the screen and each option is
            written in its own script. */}
        <div className="card mb-4">
          <p className="label mb-2">{t('login.choose_language')}</p>
          <LanguageToggle variant="cards" />
          <p className="mt-2 text-sm text-slate-600 dark:text-slate-400">
            {t('login.language_hint')}
          </p>
        </div>

        <form onSubmit={submit} className="card space-y-4">
          {error && <Banner tone="bad" title={error} />}

          <div>
            <label htmlFor="email" className="label">
              {t('login.email')}
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
              {t('login.password')}
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
              {busy ? t('login.signing_in') : t('login.sign_in')}
            </button>
          </div>
        </form>

        {import.meta.env.DEV && (
          <div className="card mt-4 text-sm">
            <p className="font-bold">{t('login.demo_accounts')}</p>
            <p className="mt-1 text-slate-600 dark:text-slate-400">
              guard@r360.local · <code>Guard@2026!</code>
              <br />
              boopathi@r360.local · <code>OpsMgr@2026!</code>
              <br />
              offload@r360.local · <code>Offload@2026!</code>
              <br />
              store@r360.local · <code>Store@2026!</code>
              <br />
              match1@r360.local · <code>Match1@2026!</code>
              <br />
              pack1@r360.local · <code>Pack1@2026!</code>
              <br />
              pack2@r360.local · <code>Pack2@2026!</code>
              <br />
              admin@r360.local · <code>Adm!n#2026$Xk9Qz</code>
            </p>
          </div>
        )}
      </AnimatedGroup>
    </div>
  )
}
