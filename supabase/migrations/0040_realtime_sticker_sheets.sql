-- 0040_realtime_sticker_sheets.sql
--
-- Adds sticker_sheets to the realtime publication 0026 set up.
--
-- The gap this closes, reported from the floor: Ops issues the unit sticker
-- sheet on one phone, and the packer's already-open scanning page on another
-- phone keeps showing "waiting for Ops to issue stickers" until it is
-- reloaded by hand. Both halves of why:
--
--   * sticker_sheets was not in the publication, so issuing a sheet produced
--     no changefeed event for useRealtimeInvalidate to react to; and
--   * the `['sheets', entryId]` query on both scanning screens had no
--     refetchInterval either (unlike every other live query there), so
--     nothing brought it back on a timer as a fallback.
--
-- So the one row whose appearance unblocks the next person in the process was
-- the one row nobody was told about. Whether a sheet exists is also not
-- sensitive — `sheets_read` (0005_rls.sql) is `using (true)` for every
-- authenticated user, which is what makes it safe to publish; realtime
-- applies the subscriber's own RLS regardless.
--
-- `stickers` itself is deliberately NOT published. A sheet is one row per
-- issuance; its stickers are one row per box or per unit, which on a large
-- PO is hundreds of inserts in a single transaction — a changefeed storm to
-- announce a fact the sheet row already carries.

-- Guarded rather than a bare `alter publication ... add table`, which is not
-- re-runnable: adding a table that is already a member fails outright with
-- 42710 ("already member of publication"), and there is no
-- `add table if not exists` for publications. These migrations get pasted
-- into the SQL editor by hand, where re-running one is completely normal —
-- and a hard error on an already-correct database looks like a failure
-- rather than the no-op it actually is. Every other repeatable statement in
-- this repo is written the same way (`drop policy if exists`, `create or
-- replace function`); this is that same property for a publication.
do $$
begin
  if not exists (
    select 1 from pg_publication_tables
     where pubname = 'supabase_realtime'
       and schemaname = 'public'
       and tablename = 'sticker_sheets'
  ) then
    alter publication supabase_realtime add table sticker_sheets;
  end if;
end;
$$;
