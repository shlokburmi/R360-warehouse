import { useEffect, useRef } from 'react'
import { useQueryClient, type QueryKey } from '@tanstack/react-query'
import { supabase } from '@/lib/supabase'

/**
 * Push instead of waiting for the next poll.
 *
 * Every "live" screen in this app already refetches on a react-query
 * `refetchInterval` (10-20s) — this doesn't replace that, it just makes the
 * refetch happen the moment something actually changes, via Supabase
 * Realtime on the table's own Postgres changefeed (0026_realtime_publication.sql).
 * The interval timer stays as the fallback if a socket drops or a change
 * lands while the tab was backgrounded and the browser throttled the
 * websocket — belt and braces, not a replacement.
 *
 * Deliberately dumb: it doesn't try to read the changed row and patch the
 * cache in place. It just invalidates, and react-query does the actual
 * refetch through the same FastAPI endpoint (and therefore the same RLS/
 * business-rule path) every other read on the page already uses. That is
 * what keeps this additive rather than a second source of truth.
 */
export function useRealtimeInvalidate(
  table: string,
  invalidate: QueryKey[],
  filter?: string,
) {
  const queryClient = useQueryClient()

  // Supabase's client dedupes channels by topic name — if two components
  // subscribe to the same table+filter, the second `.channel()` call would
  // return the first's already-subscribed channel object, and calling `.on()`
  // on an already-subscribed channel throws (an uncaught error, so it takes
  // the whole app down). A per-instance id keeps every hook instance's
  // channel topic unique, even when several subscribe to the same table.
  const instanceId = useRef(Math.random().toString(36).slice(2))

  useEffect(() => {
    const channel = supabase
      .channel(`rt:${table}:${filter ?? 'all'}:${instanceId.current}`)
      .on(
        'postgres_changes',
        { event: '*', schema: 'public', table, filter },
        () => {
          invalidate.forEach((key) => void queryClient.invalidateQueries({ queryKey: key }))
        },
      )
      .subscribe()

    return () => void supabase.removeChannel(channel)
    // `invalidate` is a fresh array every render by design (callers pass an
    // inline literal); keying off `table`/`filter` is what actually
    // identifies "which subscription is this", so re-subscribing on every
    // render would create and tear down a channel constantly for no reason.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [table, filter, queryClient])
}
