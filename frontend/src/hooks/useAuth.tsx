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
  /** Pages the app will open for this user. Admin gets every page. */
  allowed_pages: string[]
  /** Pages that get a navigation pill — a shorter list for Admin. */
  nav_pages: string[]
  /** Only packers, matchers and admins ever carry an attribution badge. */
  can_hold_badge: boolean
}

type AuthState = {
  session: Session | null
  me: Me | null
  loading: boolean
  error: string | null
  /**
   * Why the profile could not be loaded, as the API said it.
   *
   * The API answers this question precisely — "Your account has no warehouse
   * profile yet", "This account has been deactivated", "No connection" — and
   * the screen that reports the failure used to render fixed copy instead
   * ("Sign out and sign in again"), throwing the answer away. Nobody could
   * tell a missing profile from a deactivated account from a dropped
   * connection, and only one of those three is fixed by signing out.
   */
  profileError: ApiError | null
  signIn: (email: string, password: string) => Promise<void>
  signOut: () => Promise<void>
  can: (page: string) => boolean
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null)
  const [me, setMe] = useState<Me | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [profileError, setProfileError] = useState<ApiError | null>(null)

  // Two facts, kept apart, because conflating them is what produced a "Cannot
  // load your profile" error on every correct sign-in:
  //
  //  * `sessionChecked` — has Supabase told us yet whether a session exists?
  //    Until it has, "no session" is not an answer, it is silence.
  //  * `profileLoading` — is a /me request in flight right now?
  //
  // `loading` used to be one piece of state set to false as soon as the first
  // render saw `session === null` — which is *always*, since getSession() is
  // async. It then stayed false forever, so from the moment a session appeared
  // to the moment /me answered, Protected saw `loading: false, session: set,
  // me: null` and rendered its "cannot load your profile" screen — for a
  // profile that was still perfectly on its way. Signing in showed the error,
  // then the dashboard a second or two later; on a cold-starting API, the error
  // sat there for the best part of a minute first.
  const [sessionChecked, setSessionChecked] = useState(false)
  const [profileLoading, setProfileLoading] = useState(false)

  // "We do not know who you are yet": either Supabase has not answered, or it
  // has and we are still fetching the profile for the session it gave us.
  //
  // The second clause is deliberately gated on `!me`. A background refetch —
  // Supabase refreshes the token whenever a backgrounded tab regains focus,
  // which happens every time a photo picker closes over this app — must not
  // tear down the page the operator is working on. With a profile already in
  // hand, a refresh is invisible.
  const loading = !sessionChecked || (session !== null && me === null && profileLoading)

  useEffect(() => {
    let active = true

    supabase.auth
      .getSession()
      .then(({ data }) => {
        if (active) {
          setSession(data.session)
          setSessionChecked(true)
        }
      })
      .catch(() => {
        // A rejected getSession() (Supabase can trigger a network refresh
        // internally when the stored token has expired) must not leave
        // `session` stuck at its initial `null` forever with `loading`
        // never resolving — that reads as "stuck loading", not the actual
        // "couldn't confirm you're signed in, try again" it is. Treating it
        // as no-session at least reaches the login screen instead of a
        // permanent spinner.
        if (active) {
          setSession(null)
          setSessionChecked(true)
        }
      })

    const { data: subscription } = supabase.auth.onAuthStateChange((_event, next) => {
      setSession(next)
      setSessionChecked(true)
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
  // `profileLoading` goes true for every fetch, including the background ones.
  // What keeps a background refetch from tearing down the page is the `!me`
  // clause in `loading` above, not this flag — Supabase refreshes the token
  // whenever a backgrounded tab regains focus (every time a photo picker closes
  // over this app), and that used to unmount whatever the operator was in the
  // middle of, losing the photo they had just taken.
  useEffect(() => {
    if (!session) {
      setProfileLoading(false)
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
    // ~80 seconds of patience, not ~30. Render's free plan spins the instance
    // down on inactivity and takes 50s or more to wake, and while it wakes it
    // answers 502 *immediately* — so the attempts fail fast and the only thing
    // pacing this loop is these delays. Five of them added up to 30s, which
    // gave up in the middle of a cold start and produced exactly the screen
    // this retry loop was added to prevent. The first sign-in of the morning is
    // the request most likely to hit it.
    const RETRY_DELAYS_MS = [1_000, 2_000, 4_000, 8_000, 15_000, 20_000, 30_000]

    async function loadProfile() {
      for (let attempt = 0; ; attempt++) {
        try {
          const profile = await get<Me>('/me')
          if (!active) return
          setMe(profile)
          setError(null)
          setProfileError(null)
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
            setProfileError(apiError)
            return
          }

          await new Promise((resolve) => setTimeout(resolve, RETRY_DELAYS_MS[attempt]))
        }
      }
    }

    setProfileLoading(true)
    void loadProfile().finally(() => {
      if (active) setProfileLoading(false)
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
      profileError,
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
      can: (page: string) => me?.allowed_pages?.includes(page) ?? false,
    }),
    [session, me, loading, error, profileError],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside AuthProvider')
  return context
}
