-- 0026_realtime_publication.sql
--
-- Adds the tables behind every "live" screen to Supabase's realtime
-- publication. Confirmed via `select * from pg_publication_tables where
-- pubname = 'supabase_realtime'` that this publication currently has zero
-- tables in it — every screen in this app has been polling
-- (react-query refetchInterval, 10-20s) since Phase 1, with no push
-- mechanism at all. This is purely additive: no RLS changes needed here,
-- because Realtime enforces each table's existing SELECT policy per
-- subscriber, and every table below already has a permissive `using (true)`
-- policy (or, for `notifications`, the existing per-recipient one) — the
-- same policies that already govern the regular REST reads these pages do.
--
-- What this does NOT do: replace polling. The frontend hook that consumes
-- this (useRealtimeInvalidate) invalidates the same react-query caches the
-- interval timers already invalidate — it just does it the moment a change
-- happens instead of waiting for the next tick. Polling stays as the
-- fallback if a socket drops, which is why no `refetchInterval` is being
-- removed anywhere, only lengthened where it was a "hot" query.
alter publication supabase_realtime add table
  gate_entries,
  vendors,
  boxes,
  pickups,
  batches,
  batch_load_approvals,
  notifications;
