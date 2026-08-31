import { getAccessToken, supabase } from './supabase'

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
    return this.code === 'network'
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

export async function api<T>(path: string, options: Options = {}): Promise<T> {
  const token = await getAccessToken()

  if (!token) {
    throw new ApiError('Your session has ended. Please sign in again.', 401, 'no_session')
  }

  let response: Response
  try {
    response = await fetch(`${BASE}${path}`, {
      method: options.method ?? 'GET',
      headers: {
        Authorization: `Bearer ${token}`,
        ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      },
      body: options.body ? JSON.stringify(options.body) : undefined,
      signal: options.signal,
    })
  } catch {
    // fetch only rejects on a genuine network failure. Distinguishing this from
    // a server error matters: the scanning pages queue on network errors and
    // surface everything else immediately.
    throw new ApiError('No connection. Your work is saved on this device.', 0, 'network')
  }

  if (response.status === 401) {
    await supabase.auth.signOut()
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
