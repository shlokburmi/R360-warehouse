import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import type { Session } from '@supabase/supabase-js'
import { forceLocalSignOut, onForcedSignOut, supabase } from '@/lib/supabase'
import { ApiError, get } from '@/lib/api'

export type Me = {
  id: string
  full_name: string
  role: string
  role_label: string
  employee_code: string | null
  email: string | null
  allowed_pages: string[]
}

type AuthState = {
  session: Session | null
  me: Me | null
  loading: boolean
  error: string | null
  signIn: (email: string, password: string) => Promise<void>
  signOut: () => Promise<void>
  can: (page: string) => boolean
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null)
  const [me, setMe] = useState<Me | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true

    supabase.auth
      .getSession()
      .then(({ data }) => {
        if (active) setSession(data.session)
      })
      .catch(() => {
        // A rejected getSession() (Supabase can trigger a network refresh
        // internally when the stored token has expired) must not leave
        // `session` stuck at its initial `null` forever with `loading`
        // never resolving — that reads as "stuck loading", not the actual
        // "couldn't confirm you're signed in, try again" it is. Treating it
        // as no-session at least reaches the login screen instead of a
        // permanent spinner.
        if (active) setSession(null)
      })

    const { data: subscription } = supabase.auth.onAuthStateChange((_event, next) => {
      setSession(next)
      if (!next) setMe(null)
    })

    // A forced sign-out from outside this component (api.ts reacting to a
    // confirmed 401) has no React state to clear directly — it notifies
    // this listener instead of relying on Supabase's own SIGNED_OUT event,
    // which (like signOut() below) depends on a network call that a forced
    // sign-out is specifically triggered by having already failed.
    const unsubscribeForced = onForcedSignOut(() => {
      if (active) {
        setSession(null)
        setMe(null)
      }
    })

    return () => {
      active = false
      subscription.subscription.unsubscribe()
      unsubscribeForced()
    }
  }, [])

  // The profile — role, name, which pages exist — comes from the API, not from
  // the JWT. Roles can change mid-shift and a token issued eight hours ago
  // should not be what decides what someone can see.
  //
  // `loading` deliberately does NOT get set back to true here on a rerun of
  // this effect. Supabase's client refreshes the auth token automatically —
  // notably, when a backgrounded tab regains focus, which is exactly what
  // happens every time a file/photo picker opens and closes over this app.
  // That refresh publishes a new `session` object for the *same* signed-in
  // user, re-running this effect. Setting `loading = true` here used to make
  // `Protected` (App.tsx) unmount the entire current page in favour of its
  // "Signing in…" spinner on every one of those routine refreshes — turning
  // "picked a photo to upload" into "the whole screen was reset" with no
  // error and no photo. `loading` now only ever reflects the *first* check
  // (its initial useState(true) default, resolved once below); a later
  // session change still refreches `/me` in the background, but does so
  // without tearing down whatever the operator is in the middle of.
  useEffect(() => {
    if (!session) {
      setLoading(false)
      return
    }

    let active = true

    /**
     * Fetch the profile, retrying a transient failure rather than treating
     * the first one as final.
     *
     * The backend is on Render's Free plan, which spins down on inactivity —
     * so the very first request after any idle period hits an instance that
     * is still waking up, and answers 502/503 (or nothing at all until the
     * timeout) for the first several seconds. One attempt was enough to send
     * `Protected` straight to its "Cannot load your profile" screen, which
     * says to sign out and ask an Admin — for what is really just a server
     * that needed another twenty seconds. Then Supabase's own automatic
     * token refresh would re-run this effect a minute later, quietly
     * succeed, and drop the operator on the dashboard, which is exactly the
     * "shows the error, then after some time redirects me" that was
     * reported.
     *
     * Only transient failures are retried: a network/timeout error or a 5xx.
     * A 4xx is a real answer about this account (a genuinely missing
     * profile, say) and retrying it would just delay the honest message.
     */
    const RETRY_DELAYS_MS = [1_000, 2_000, 4_000, 8_000, 15_000]

    async function loadProfile() {
      for (let attempt = 0; ; attempt++) {
        try {
          const profile = await get<Me>('/me')
          if (!active) return
          setMe(profile)
          setError(null)
          return
        } catch (err) {
          if (!active) return

          const apiError = err instanceof ApiError ? err : null
          const transient = apiError ? apiError.isOffline || apiError.status >= 500 : false

          if (!transient || attempt >= RETRY_DELAYS_MS.length) {
            // Not `setMe(null)`: a background refetch failing (a token
            // refresh landing during a flaky connection, say) must not
            // blank a page that was already working from the last
            // successful profile it has. `me` only ever goes back to null
            // via an explicit sign-out.
            setError((err as Error).message)
            return
          }

          await new Promise((resolve) => setTimeout(resolve, RETRY_DELAYS_MS[attempt]))
        }
      }
    }

    void loadProfile().finally(() => {
      if (active) setLoading(false)
    })

    return () => {
      active = false
    }
  }, [session])

  const value = useMemo<AuthState>(
    () => ({
      session,
      me,
      loading,
      error,
      async signIn(email, password) {
        setError(null)
        let signInError: { message: string } | null
        try {
          const result = await supabase.auth.signInWithPassword({
            email: email.trim(),
            password,
          })
          signInError = result.error
        } catch {
          // signInWithPassword only wraps a genuine AuthError into
          // `{ error }` — a plain network failure (weak signal, offline)
          // rejects the promise instead with the browser's raw "Failed to
          // fetch" TypeError, which is not a message anyone signing in on a
          // shaky mobile connection should have to interpret.
          throw new Error('No connection. Check your signal and try again.')
        }
        if (signInError) {
          throw new Error(
            signInError.message === 'Invalid login credentials'
              ? 'Wrong email or password.'
              : signInError.message,
          )
        }
      },
      async signOut() {
        // forceLocalSignOut() notifies the onForcedSignOut listener above,
        // which clears session/me — guaranteed, regardless of connectivity.
        await forceLocalSignOut()
      },
      can: (page: string) => me?.allowed_pages.includes(page) ?? false,
    }),
    [session, me, loading, error],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside AuthProvider')
  return context
}
