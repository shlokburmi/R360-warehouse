-- 0014_photo_retention.sql
--
-- PRD §8 Data Privacy / DPDP Act 2023. 0006_storage.sql made both buckets
-- private and then said:
--
--     "Retention is handled by a scheduled job, not by users."
--
-- There was no such job, so identity photos were kept forever. Private forever
-- is still forever, and the DPDP Act's storage-limitation principle is about
-- how long personal data is held, not who can see it.
--
-- The rule implemented here follows from DECISIONS.md §2 rather than inventing
-- a second number: a photo is force-re-captured after
-- ID_PHOTO_REVALIDATION_DAYS because by then it verifies nothing. A photo that
-- verifies nothing has no purpose left, and data held without a purpose is
-- exactly what the Act asks you not to hold. So **the photo is deleted at the
-- moment it expires** — one threshold, no window in which a photo is both
-- useless and retained.

-- ===========================================================================
-- THE RECORD THAT A PHOTO WAS DESTROYED
--
-- PRD §7 says nothing is deleted, only reversed. A retention purge is the one
-- place that cannot hold — the whole point is that the bytes stop existing.
-- What survives is the fact: this visitor had a photo, it was captured then,
-- and it was destroyed at this time for this reason.
--
-- Note that `id_photo_captured_at` is cleared along with the path, because
-- `visitors_photo_consistent` requires the two to travel together. That is the
-- right behaviour anyway: with no capture date the gate treats the visitor as
-- needing a fresh photo, which is precisely what an expired photo means.
-- ===========================================================================

alter table visitors
  add column id_photo_purged_at    timestamptz,
  add column id_photo_purge_reason text,
  -- Kept separately from id_photo_captured_at, which is cleared by the purge.
  -- Without this, "how old was the photo we destroyed?" becomes unanswerable,
  -- and that is the first question a data-protection audit asks.
  add column id_photo_purged_age_days int;

alter table visitors add constraint visitors_purge_consistent
  check (num_nonnulls(id_photo_purged_at, id_photo_purge_reason) <> 1);

comment on column visitors.id_photo_purged_at is
  'When the identity photo was destroyed under the retention policy. The bytes '
  'are gone; this is the audit record that they existed. See PRD §8.';

-- A purged visitor must not silently look like one who never had a photo, or
-- the retention job cannot be shown to have run. This index makes the
-- reporting view cheap and the sweep''s candidate scan cheap for the same cost.
create index visitors_photo_retention_idx
  on visitors (id_photo_captured_at)
  where id_photo_path is not null;

-- ===========================================================================
-- RETENTION POSTURE
--
-- Half of a retention policy is being able to demonstrate it ran. This view is
-- what an Ops Manager or an auditor looks at: how many photos are held, how old
-- the oldest is, and whether anything is overdue for destruction.
--
-- `overdue` should be zero whenever the worker is alive. A non-zero value that
-- stays non-zero is the signal that the sweep has stopped, which is otherwise
-- an entirely silent failure — nothing breaks when data is *not* deleted.
-- ===========================================================================

create or replace function identity_photo_cutoff(p_retention_days int)
returns timestamptz
language sql
stable
as $$
  select now() - make_interval(days => greatest(p_retention_days, 1));
$$;

-- A function rather than a view, because `overdue` depends on the retention
-- window, which is configuration and lives in the backend. A view would have to
-- read it from a session GUC that every request would then have to set, or
-- hardcode a second copy of the number that could drift from the real one.
-- Taking it as an argument keeps one source of truth.
create or replace function photo_retention_status(p_retention_days int)
returns table (
  photos_held        int,
  photos_purged      int,
  oldest_held_at     timestamptz,
  last_purge_at      timestamptz,
  overdue            int,
  retained_for_block int
)
language sql
stable
security invoker
as $$
  select
    count(*) filter (where id_photo_path is not null)::int,
    count(*) filter (where id_photo_purged_at is not null)::int,
    min(id_photo_captured_at) filter (where id_photo_path is not null),
    max(id_photo_purged_at),
    count(*) filter (
      where id_photo_path is not null
        and not is_blocked
        and id_photo_captured_at < identity_photo_cutoff(p_retention_days)
    )::int,
    count(*) filter (where is_blocked and id_photo_path is not null)::int
  from visitors;
$$;

grant execute on function photo_retention_status(int) to authenticated;

comment on function photo_retention_status(int) is
  'Retention posture for identity photos (PRD §8). `overdue` staying above zero '
  'means the retention sweep has stopped — a failure nothing else surfaces, '
  'because nothing breaks when data is not deleted.';

-- ===========================================================================
-- BLOCKED VISITORS KEEP THEIR PHOTO
--
-- Purpose limitation cuts both ways: a photo is deleted when its purpose ends,
-- and kept while the purpose lasts. A block is enforced on the mobile number,
-- and a number is trivially borrowed — so for a blocked visitor the photo is
-- still doing the job it was captured for, which is letting a guard confirm the
-- person at the gate is the person who was blocked.
--
-- Enforced here rather than only in the sweep, so a future second caller cannot
-- quietly bypass it.
-- ===========================================================================

create or replace function purge_identity_photo(
  p_visitor_id uuid,
  p_reason     text default 'retention'
)
returns boolean
language plpgsql
volatile
security definer
set search_path = public
as $$
declare
  v_captured timestamptz;
  v_blocked  boolean;
  v_path     text;
begin
  select id_photo_captured_at, is_blocked, id_photo_path
    into v_captured, v_blocked, v_path
    from visitors
   where id = p_visitor_id;

  if v_path is null then
    return false;  -- already purged, or never had one
  end if;

  if v_blocked then
    raise exception 'Visitor % is blocked; the identity photo is still in use.',
      p_visitor_id
      using hint = 'Unblock the visitor first if the photo should be destroyed.';
  end if;

  update visitors
     set id_photo_path         = null,
         id_photo_captured_at  = null,
         id_photo_purged_at    = now(),
         id_photo_purge_reason = p_reason,
         id_photo_purged_age_days =
           case when v_captured is null then null
                else extract(day from now() - v_captured)::int end
   where id = p_visitor_id;

  return true;
end;
$$;

revoke all on function purge_identity_photo(uuid, text) from public, anon, authenticated;

comment on function purge_identity_photo(uuid, text) is
  'Records the destruction of an identity photo. Deleting the object from '
  'storage is the caller''s job and must happen first — see '
  'app/services/retention.py for why that order is not arbitrary.';
