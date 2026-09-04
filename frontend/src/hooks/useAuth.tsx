import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import type { Session } from '@supabase/supabase-js'
import { forceLocalSignOut, onForcedSignOut, supabase } from '@/lib/supabase'
import { get } from '@/lib/api'

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
  useEffect(() => {
    if (!session) {
      setLoading(false)
      return
    }

    let active = true
    setLoading(true)

    get<Me>('/me')
      .then((profile) => {
        if (!active) return
        setMe(profile)
        setError(null)
      })
      .catch((err: Error) => {
        if (!active) return
        setMe(null)
        setError(err.message)
      })
      .finally(() => {
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
