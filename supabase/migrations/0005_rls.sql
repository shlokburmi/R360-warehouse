-- 0005_rls.sql
-- PRD §8: role-based access control, enforced by the database.
--
-- These policies protect BOTH access paths — a direct PostgREST/supabase-js call
-- and a FastAPI request — because the API connects as a non-superuser and runs
-- every request with `SET LOCAL ROLE authenticated` plus verified JWT claims.
-- See docs/DECISIONS.md §B1. If the API ever connects with service_role, every
-- policy below silently stops applying, so that connection is confined to
-- migrations and background workers.

-- ===========================================================================
-- ROLE HELPERS
-- ===========================================================================

-- SECURITY DEFINER, because reading profiles to decide a profiles policy would
-- otherwise recurse. STABLE so the planner caches it per statement.
create or replace function auth_role()
returns user_role
language sql
stable
security definer
set search_path = public
as $$
  select role from profiles where id = auth.uid() and is_active;
$$;

-- Historically "ops or admin"; ops_manager was folded into admin when the
-- role model was consolidated to four roles, so this is now just admin. Kept
-- as its own function because every policy below still calls it by name.
create or replace function is_ops()
returns boolean language sql stable as $$
  select auth_role() = 'admin';
$$;

create or replace function is_admin()
returns boolean language sql stable as $$
  select auth_role() = 'admin';
$$;

create or replace function has_role(variadic roles user_role[])
returns boolean language sql stable as $$
  select auth_role() = any(roles);
$$;

-- ===========================================================================
-- BASE GRANTS
-- Nothing is readable by anon. DELETE is granted to nobody, anywhere.
-- ===========================================================================

do $$
declare t text;
begin
  foreach t in array array[
    'profiles', 'vendors', 'locations', 'purchase_orders', 'purchase_order_lines',
    'visitors', 'gate_entries', 'gate_entry_persons', 'sticker_sheets', 'stickers',
    'boxes', 'damage_photos', 'scan_events', 'inbound_reconciliations',
    'exceptions', 'notifications', 'putaways', 'invoices',
    'invoice_verifications', 'packing_records', 'batches', 'audit_log'
  ] loop
    execute format('alter table %I enable row level security', t);
    execute format('alter table %I force row level security', t);
    execute format('revoke all on %I from anon, authenticated', t);
    execute format('grant select, insert, update on %I to authenticated', t);
    execute format('revoke delete on %I from authenticated', t);
  end loop;
end;
$$;

-- audit_log and scan_events are never updated by anyone.
revoke update on audit_log, scan_events from authenticated;
revoke insert on audit_log from authenticated;

grant select on v_warehouse_counts, v_vendor_accuracy to authenticated;
grant execute on function next_code(text) to authenticated;
grant execute on function generate_badge_code() to authenticated;

-- ===========================================================================
-- PROFILES
-- ===========================================================================

create policy profiles_select_self on profiles
  for select to authenticated
  using (id = auth.uid() or is_ops());

-- A user may edit their own name and mobile. Role, badge and active flags are
-- deliberately not self-serviceable — a guard promoting themselves to
-- admin would defeat CP1 entirely.
create policy profiles_update_self on profiles
  for update to authenticated
  using (id = auth.uid())
  with check (
    id = auth.uid()
    and role = (select p.role from profiles p where p.id = auth.uid())
    and is_active = (select p.is_active from profiles p where p.id = auth.uid())
    and badge_active = (select p.badge_active from profiles p where p.id = auth.uid())
  );

create policy profiles_admin_all on profiles
  for all to authenticated
  using (is_admin()) with check (is_admin());

-- ===========================================================================
-- MASTER DATA — readable by all staff (needed to fill in forms), written by Ops
-- ===========================================================================

create policy vendors_read on vendors
  for select to authenticated using (true);
create policy vendors_write on vendors
  for all to authenticated using (is_ops()) with check (is_ops());

create policy locations_read on locations
  for select to authenticated using (true);
create policy locations_write on locations
  for all to authenticated using (is_ops()) with check (is_ops());

create policy po_read on purchase_orders
  for select to authenticated using (true);
create policy po_write on purchase_orders
  for all to authenticated using (is_ops()) with check (is_ops());

create policy po_lines_read on purchase_order_lines
  for select to authenticated using (true);
create policy po_lines_write on purchase_order_lines
  for all to authenticated using (is_ops()) with check (is_ops());

-- ===========================================================================
-- VISITORS
-- The row is visible to the gate and to Ops. The *photograph* is protected
-- separately by storage policies (0006) — guards can upload one and can never
-- read one back, including their own uploads.
-- ===========================================================================

create policy visitors_read on visitors
  for select to authenticated
  using (has_role('security_guard', 'admin'));

create policy visitors_insert on visitors
  for insert to authenticated
  with check (has_role('security_guard', 'admin'));

create policy visitors_update on visitors
  for update to authenticated
  using (has_role('security_guard', 'admin'))
  with check (has_role('security_guard', 'admin'));

-- ===========================================================================
-- GATE ENTRIES
-- Every operational role can read the queue — an offloader needs to know which
-- vehicle they are unloading. Only the gate creates entries; only Ops decides.
-- ===========================================================================

create policy gate_entries_read on gate_entries
  for select to authenticated using (true);

create policy gate_entries_insert on gate_entries
  for insert to authenticated
  with check (
    has_role('security_guard', 'admin')
    and requested_by = auth.uid()   -- you cannot file a request as someone else
  );

-- Guards drive the truck through the gate (submit, declare boxes); packers
-- drive the sticker-scanning steps; Admin drives the decisions. The
-- status-transition trigger in 0004 constrains what each of those updates is
-- actually allowed to change the row into.
create policy gate_entries_update_gate on gate_entries
  for update to authenticated
  using (has_role('security_guard') and status in ('draft', 'approved', 'inside'))
  with check (has_role('security_guard') and decided_by is distinct from auth.uid());

-- Packers apply and scan both box and unit stickers, and close out offloading
-- for the truck — so they need to advance the entry through 'counting' (CP2
-- complete), 'box_verified' and 'offloading' (CP3 complete).
create policy gate_entries_update_packer on gate_entries
  for update to authenticated
  using (has_role('packer') and status in ('counting', 'box_verified', 'offloading'))
  with check (has_role('packer'));

-- Offloading reconciles the inbound count (CONTROL POINT 4) — the one step in
-- this lifecycle still theirs, now that packers own the scanning.
create policy gate_entries_update_reconcile on gate_entries
  for update to authenticated
  using (has_role('offloading') and status in ('offloaded', 'reconciled'))
  with check (has_role('offloading'));

create policy gate_entries_update_ops on gate_entries
  for update to authenticated
  using (is_ops()) with check (is_ops());

create policy gate_persons_read on gate_entry_persons
  for select to authenticated
  using (has_role('security_guard', 'admin'));

create policy gate_persons_insert on gate_entry_persons
  for insert to authenticated
  with check (has_role('security_guard', 'admin'));

-- ===========================================================================
-- STICKERS — issued by Ops only. This is what makes CP2 meaningful: if the
-- floor could print its own stickers, "scanned == issued" would be circular.
-- ===========================================================================

create policy sheets_read on sticker_sheets
  for select to authenticated using (true);
create policy sheets_write on sticker_sheets
  for all to authenticated using (is_ops()) with check (is_ops());

create policy stickers_read on stickers
  for select to authenticated using (true);
create policy stickers_insert on stickers
  for insert to authenticated with check (is_ops());
-- Status advances to 'scanned' via the scan trigger, run as whoever scans
-- (packers apply and scan both box and unit stickers); voiding is an Admin act.
create policy stickers_update on stickers
  for update to authenticated
  using (is_ops() or has_role('packer'))
  with check (is_ops() or has_role('packer'));

-- ===========================================================================
-- BOXES
-- ===========================================================================

create policy boxes_read on boxes
  for select to authenticated using (true);
create policy boxes_insert on boxes
  for insert to authenticated with check (is_ops());
-- Packers scan units/close boxes; offloading still needs this for
-- fn_putaway_close_box, which marks a box 'emptied' as the putaway-inserting
-- user (DECISIONS.md Part D — adding a role to a step means revisiting every
-- table that step's triggers touch).
create policy boxes_update on boxes
  for update to authenticated
  using (has_role('packer', 'offloading', 'admin'))
  with check (has_role('packer', 'offloading', 'admin'));

create policy damage_photos_read on damage_photos
  for select to authenticated using (true);
create policy damage_photos_insert on damage_photos
  for insert to authenticated
  with check (has_role('packer', 'admin') and uploaded_by = auth.uid());

-- ===========================================================================
-- SCAN EVENTS
-- Append-only. The WITH CHECK pins each scan type to the role that performs it
-- in the physical process, so an offloader cannot verify boxes at the gate.
-- ===========================================================================

create policy scans_read on scan_events
  for select to authenticated using (true);

create policy scans_insert on scan_events
  for insert to authenticated
  with check (
    scanned_by = auth.uid()
    and (
      (scan_type = 'box_verify'  and has_role('security_guard', 'admin'))
      or (scan_type = 'unit_verify' and has_role('offloading', 'admin'))
      or (scan_type = 'out_scan'    and has_role('admin'))
      or (scan_type = 'gate_exit'   and has_role('security_guard', 'admin'))
    )
  );

-- ===========================================================================
-- INBOUND RECONCILIATION
-- ===========================================================================

create policy inbound_read on inbound_reconciliations
  for select to authenticated using (true);
create policy inbound_write on inbound_reconciliations
  for insert to authenticated
  with check (has_role('offloading', 'admin') and verified_by = auth.uid());

-- The offloading team must be able to update, not just insert. The recount loop
-- depends on it: submit a count, hit a mismatch, physically recount, submit
-- again. Restricting UPDATE to Ops would mean the second submission is refused
-- and the only way past CONTROL POINT 4 would be an Ops override — which is
-- precisely the manual override this system exists to eliminate.
-- Every revision is captured by the audit trigger, so the first count is not
-- lost when it is corrected.
create policy inbound_update on inbound_reconciliations
  for update to authenticated
  using (has_role('offloading', 'admin'))
  with check (has_role('offloading', 'admin') and verified_by = auth.uid());

-- ===========================================================================
-- EXCEPTIONS
-- Anyone on the floor can raise one — that is the point, the person who finds
-- the problem reports it. Only Ops/Admin can resolve one.
-- ===========================================================================

create policy exceptions_read on exceptions
  for select to authenticated using (true);

create policy exceptions_insert on exceptions
  for insert to authenticated with check (reported_by = auth.uid());

create policy exceptions_resolve on exceptions
  for update to authenticated using (is_ops()) with check (is_ops());

-- ===========================================================================
-- NOTIFICATIONS — you see your own, or your role's
-- ===========================================================================

create policy notifications_read on notifications
  for select to authenticated
  using (recipient_id = auth.uid() or recipient_role = auth_role() or is_admin());

create policy notifications_mark_read on notifications
  for update to authenticated
  using (recipient_id = auth.uid() or recipient_role = auth_role())
  with check (recipient_id = auth.uid() or recipient_role = auth_role());

create policy notifications_insert on notifications
  for insert to authenticated with check (true);

-- ===========================================================================
-- AUDIT LOG — readable by Ops and Admin, writable by nobody
-- ===========================================================================

create policy audit_read on audit_log
  for select to authenticated using (is_ops());

-- ===========================================================================
-- PHASE 2-4 TABLES
-- ===========================================================================

create policy putaways_read on putaways for select to authenticated using (true);
create policy putaways_insert on putaways for insert to authenticated
  with check (has_role('offloading', 'admin') and moved_by = auth.uid());

create policy invoices_read on invoices for select to authenticated using (true);
create policy invoices_write on invoices for all to authenticated
  using (is_ops()) with check (is_ops());

create policy inv_verif_read on invoice_verifications for select to authenticated using (true);
create policy inv_verif_insert on invoice_verifications for insert to authenticated
  with check (has_role('admin'));

create policy packing_read on packing_records for select to authenticated using (true);
create policy packing_insert on packing_records for insert to authenticated
  with check (has_role('packer', 'admin'));

create policy batches_read on batches for select to authenticated using (true);
create policy batches_write on batches for all to authenticated
  using (is_ops()) with check (is_ops());

-- ===========================================================================
-- API DATABASE ROLE
-- The FastAPI backend logs in as this role and then assumes `authenticated`
-- per request. It is deliberately NOT a superuser and NOT the table owner, so
-- `force row level security` above applies to it.
-- ===========================================================================

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'api_user') then
    -- Password is set out-of-band; see backend/.env.example.
    create role api_user login noinherit;
  end if;
end;
$$;

grant usage on schema public to api_user;
grant authenticated to api_user;
