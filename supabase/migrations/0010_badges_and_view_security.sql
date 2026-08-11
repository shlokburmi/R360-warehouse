-- 0010_badges_and_view_security.sql
--
-- Two related corrections, both found by driving the Phase 3 flow over HTTP.
--
-- 1. Badge resolution could not work. `profiles_select_self` lets a non-Ops user
--    see only their own row, so a matcher's session could identify her own badge
--    and nobody else's. On a shared station tablet — the normal case — that
--    makes the whole attribution mechanism unusable.
--
-- 2. Views were silently bypassing RLS. A view runs with its owner's privileges
--    unless `security_invoker` is set, and these views are owned by postgres, so
--    every policy on the underlying tables was being ignored for anyone who
--    queried them. Nothing sensitive leaked (the views only expose operational
--    aggregates) but "RLS is enforced" has to be true everywhere or it is not a
--    thing you can rely on.
--
-- The fix for (1) is not "let staff read the profiles table" on its own, because
-- that would expose badge codes — the one field that must not be readable, since
-- reading it is equivalent to holding the badge. So: names and roles become
-- readable, badge codes and mobile numbers become unreadable at the *column*
-- level, and badges are resolved through a narrow definer function that returns
-- the holder without ever returning the code.

-- ===========================================================================
-- PROFILES: readable names, unreadable badges
-- ===========================================================================

-- Column-level privileges only take effect if the table-level SELECT grant is
-- removed first — a table-wide grant subsumes any column grant.
revoke select on profiles from authenticated;

grant select (
  id, full_name, employee_code, role, is_active, badge_active,
  is_backup_approver, created_at, updated_at
) on profiles to authenticated;

-- Deliberately NOT granted: `badge_code` and `mobile`.
--
-- `badge_code` is the sensitive one. Being able to read another person's badge
-- code is equivalent to being able to present their badge, which would let one
-- packer attribute their work to another — or let a single person satisfy both
-- halves of CONTROL POINT 5 alone. It is readable only through
-- resolve_badge_holder() below, which takes a code and never returns one.
comment on column profiles.badge_code is
  'Attribution token. Not readable by `authenticated` — see resolve_badge_holder().';

-- Names and roles are visible to all staff. They are on badges, on rosters and
-- on the wall; treating them as secret would break every screen that shows
-- "verified by X" while protecting nothing.
create policy profiles_select_staff on profiles
  for select to authenticated
  using (true);

drop policy if exists profiles_select_self on profiles;

-- ===========================================================================
-- BADGE RESOLUTION
-- ===========================================================================

-- SECURITY DEFINER so a station session can identify a badge holder without
-- being able to read the badge column at all. Takes a code, returns a person.
-- There is no inverse: nothing in the system will tell you a person's code.
--
-- Not enumerable in practice — codes are 64 bits of randomness — and it returns
-- the active flags so the caller can say "that badge was deactivated" rather
-- than the unhelpful "not recognised".
create or replace function resolve_badge_holder(p_badge_code text)
returns table (
  id uuid,
  full_name text,
  role text,
  employee_code text,
  badge_active boolean,
  is_active boolean
)
language sql
stable
security definer
set search_path = public
as $$
  select p.id, p.full_name, p.role::text, p.employee_code, p.badge_active, p.is_active
    from profiles p
   where p.badge_code = btrim(p_badge_code);
$$;

revoke all on function resolve_badge_holder(text) from public, anon;
grant execute on function resolve_badge_holder(text) to authenticated;

-- ===========================================================================
-- VIEWS RESPECT THE CALLER'S RLS
-- ===========================================================================

-- With profiles now readable by staff, every table these views touch is already
-- covered by a policy that permits the read, so switching them to the caller's
-- privileges changes no legitimate behaviour — it just removes a bypass.
alter view v_warehouse_counts    set (security_invoker = true);
alter view v_vendor_accuracy     set (security_invoker = true);
alter view v_box_putaway_status  set (security_invoker = true);
alter view v_putaway_queue       set (security_invoker = true);
alter view v_stock_by_location   set (security_invoker = true);
alter view v_invoice_status      set (security_invoker = true);
alter view v_batch_status        set (security_invoker = true);
