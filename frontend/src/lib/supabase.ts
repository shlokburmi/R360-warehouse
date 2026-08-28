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
