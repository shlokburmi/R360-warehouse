import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import type { Session } from '@supabase/supabase-js'
import { supabase } from '@/lib/supabase'
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

    supabase.auth.getSession().then(({ data }) => {
      if (active) setSession(data.session)
    })

    const { data: subscription } = supabase.auth.onAuthStateChange((_event, next) => {
      setSession(next)
      if (!next) setMe(null)
    })

    return () => {
      active = false
      subscription.subscription.unsubscribe()
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
        const { error: signInError } = await supabase.auth.signInWithPassword({
          email: email.trim(),
          password,
        })
        if (signInError) {
          throw new Error(
            signInError.message === 'Invalid login credentials'
              ? 'Wrong email or password.'
              : signInError.message,
          )
        }
      },
      async signOut() {
        await supabase.auth.signOut()
        setMe(null)
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
