import { forceLocalSignOut, getAccessToken, supabase } from './supabase'

const BASE = import.meta.env.VITE_API_URL ?? 'http://127.0.0.1:8000/api/v1'

/**
 * An error the backend produced deliberately — a control point refusal, a
 * validation failure, a role check. These carry a message written to be shown
 * to the operator verbatim, so pages render `error.message` rather than
 * inventing their own wording.
 */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string,
    readonly hint?: string,
    readonly details?: Record<string, unknown>,
  ) {
    super(message)
    this.name = 'ApiError'
  }

  /** A hard stop from PRD §4. These are never retried and never overridden. */
  get isControlPoint() {
    return this.code === 'control_point_failed'
  }

  get isOffline() {
    // A timeout is treated the same as an outright network failure here: on
    // the connection this app runs on, a request that took 20s and never
    // answered is not meaningfully different from one that was refused
    // immediately — both mean "the network is the problem right now", and
    // the scanning pages already know how to queue that for later.
    return this.code === 'network' || this.code === 'timeout'
  }
}

type Options = {
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE'
  body?: unknown
  signal?: AbortSignal
  /**
   * Status codes to treat as a normal response instead of throwing.
   *
   * The three control-point endpoints answer 409 with a full, meaningful body
   * — the box that was held, the exception code that was logged. That is a
   * result to render, not an error to catch, so the caller opts in to
   * receiving it.
   */
  allowStatus?: number[]
}

// `fetch` has no built-in timeout — on the connection this app actually runs
// on (a warehouse floor with patchy wifi; 0.00-20 KB/s readings turn up
// routinely in the field), a dead or crawling request otherwise hangs
// indefinitely with zero feedback. That is indistinguishable from "the app
// is broken" to whoever is staring at a spinner that never resolves — several
// reports that looked like a stuck scanner or a stuck sign-in turned out to
// be exactly this.
//
// 60s, not something shorter: the backend is on Render's Free plan, which
// spins down on inactivity and can take 50+ seconds to wake on the first
// request after any idle period (Render's own dashboard states this). A
// shorter timeout doesn't make that cold start faster — it just fails a
// request that was genuinely about to succeed, which is worse than the
// indefinite hang this was meant to fix. 60s clears that worst case with
// room to spare while still eventually giving up on a connection that is
// truly dead rather than just slow to wake something up.
const REQUEST_TIMEOUT_MS = 60_000

async function doFetch(path: string, token: string, options: Options): Promise<Response> {
  const timedOut = new AbortController()
  const timer = window.setTimeout(() => timedOut.abort(), REQUEST_TIMEOUT_MS)

  // Combine the caller's own signal (if any) with the timeout rather than
  // relying on AbortSignal.any — not supported on every Android WebView this
  // still has to run on. Nothing currently passes its own signal, but the
  // option stays honoured for whoever does next.
  const external = options.signal
  if (external) {
    if (external.aborted) timedOut.abort()
    else external.addEventListener('abort', () => timedOut.abort(), { once: true })
  }

  try {
    return await fetch(`${BASE}${path}`, {
      method: options.method ?? 'GET',
      headers: {
        Authorization: `Bearer ${token}`,
        ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      },
      body: options.body ? JSON.stringify(options.body) : undefined,
      signal: timedOut.signal,
    })
  } catch {
    // fetch rejects both on a genuine network failure and on our own timeout
    // abort — distinguished so the operator is told which one happened
    // rather than a single generic "no connection" that isn't quite true
    // when the request was merely too slow, not refused outright.
    if (timedOut.signal.aborted && !external?.aborted) {
      throw new ApiError(
        'That took too long to respond. Check your connection and try again.',
        0,
        'timeout',
      )
    }
    throw new ApiError('No connection. Your work is saved on this device.', 0, 'network')
  } finally {
    window.clearTimeout(timer)
  }
}

export async function api<T>(path: string, options: Options = {}): Promise<T> {
  const token = await getAccessToken()

  if (!token) {
    throw new ApiError('Your session has ended. Please sign in again.', 401, 'no_session')
  }

  let response = await doFetch(path, token, options)

  if (response.status === 401) {
    // A 401 does not necessarily mean the session is actually gone. Supabase
    // pauses its own background refresh ticker while the tab is hidden (the
    // auth-js client only runs it on a focused tab), and a client-side step
    // like taking a photo for OCR routinely backgrounds the tab for long
    // enough that the access token handed back by getAccessToken() above is
    // stale even though a perfectly good refresh token still exists. Try one
    // explicit refresh-and-retry before treating this as a real sign-out —
    // without it, a single stale token forces a full sign-out on a flow that
    // is guaranteed to hit the same race again on the very next attempt.
    let refreshed: Awaited<ReturnType<typeof supabase.auth.refreshSession>> | null = null
    try {
      refreshed = await supabase.auth.refreshSession()
    } catch {
      refreshed = null
    }

    if (refreshed && !refreshed.error && refreshed.data.session) {
      response = await doFetch(path, refreshed.data.session.access_token, options)
    }
  }

  if (response.status === 401) {
    // forceLocalSignOut() is guaranteed to clear the local session even if
    // its own network call fails, and notifies AuthProvider (which has no
    // presence in this module) so `session` actually updates and the app
    // redirects to login — a bare `supabase.auth.signOut()` here both risks
    // throwing unguarded on bad signal and, even when it doesn't throw,
    // leaves React state untouched.
    await forceLocalSignOut()
    throw new ApiError('Your session has expired. Please sign in again.', 401, 'expired')
  }

  if (response.status === 204) {
    return undefined as T
  }

  const payload = await response.json().catch(() => null)

  if (!response.ok && options.allowStatus?.includes(response.status)) {
    return payload as T
  }

  if (!response.ok) {
    // FastAPI's own validation errors and our AppError shape differ; normalise
    // both so callers only ever handle one thing.
    const err = payload?.error ?? payload?.detail?.error
    if (err) {
      throw new ApiError(err.message, response.status, err.code, err.hint, err)
    }

    if (Array.isArray(payload?.detail)) {
      const first = payload.detail[0]
      throw new ApiError(
        first?.msg ?? 'Please check the highlighted fields.',
        response.status,
        'validation',
      )
    }

    throw new ApiError(
      typeof payload?.detail === 'string' ? payload.detail : 'Something went wrong.',
      response.status,
      'unknown',
    )
  }

  return payload as T
}

export const get = <T,>(path: string, signal?: AbortSignal) => api<T>(path, { signal })
export const post = <T,>(path: string, body?: unknown) =>
  api<T>(path, { method: 'POST', body })
export const patch = <T,>(path: string, body?: unknown) =>
  api<T>(path, { method: 'PATCH', body })
export const del = <T,>(path: string) => api<T>(path, { method: 'DELETE' })

/** POST against a control point: a 409 comes back as data, not an exception. */
export const postControlPoint = <T,>(path: string, body?: unknown) =>
  api<T>(path, { method: 'POST', body, allowStatus: [409] })
