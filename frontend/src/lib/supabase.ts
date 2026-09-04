import { createClient } from '@supabase/supabase-js'

const url = import.meta.env.VITE_SUPABASE_URL
const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY

if (!url || !anonKey) {
  throw new Error(
    'VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY must be set. Copy .env.example to .env.local.',
  )
}

/**
 * Every read and write still goes through the FastAPI backend, which
 * enforces the control points — Supabase itself is used for authentication,
 * and (since useRealtimeInvalidate) for realtime change notifications, which
 * are read-only and RLS-scoped exactly like a REST read would be.
 *
 * `persistSession` keeps a guard signed in across a phone restart; the token
 * itself is short-lived and refreshed automatically, so a lost device is
 * revoked by disabling the account rather than by hoping the tab was closed.
 */
export const supabase = createClient(url, anonKey, {
  auth: {
    persistSession: true,
    autoRefreshToken: true,
    detectSessionInUrl: false,
    storageKey: 'r360-warehouse-auth',
  },
})

export async function getAccessToken(): Promise<string | null> {
  const { data } = await supabase.auth.getSession()
  return data.session?.access_token ?? null
}

type Listener = () => void
const forcedSignOutListeners = new Set<Listener>()

/** AuthProvider subscribes once, to clear its React state when a forced
 * sign-out happens somewhere with no React context to update directly
 * (api.ts's 401 handler). Returns an unsubscribe function. */
export function onForcedSignOut(listener: Listener): () => void {
  forcedSignOutListeners.add(listener)
  return () => forcedSignOutListeners.delete(listener)
}

/**
 * Sign out of this device, guaranteed, regardless of connectivity.
 *
 * `supabase.auth.signOut()` tries to revoke the session on the server
 * first, and only clears the *locally persisted* session if that network
 * call succeeds (or comes back 401/403/404) — a plain connectivity failure
 * (weak signal, timeout) leaves the stale session in storage untouched, and
 * `scope: 'local'` does not change this (it only changes what the server
 * call is asked to revoke, not whether that call is attempted at all).
 *
 * Called from two places that don't share React state — a person tapping
 * "Sign out", and api.ts reacting to a confirmed 401 — so local cleanup
 * happens here rather than being trusted to Supabase's own SIGNED_OUT
 * event, which depends on that same network call having succeeded.
 */
export async function forceLocalSignOut(): Promise<void> {
  try {
    await supabase.auth.signOut({ scope: 'local' })
  } catch {
    // Best-effort — local cleanup below happens either way.
  }
  window.localStorage.removeItem('r360-warehouse-auth')
  forcedSignOutListeners.forEach((listener) => listener())
}
