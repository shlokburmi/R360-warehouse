-- 0023_role_split.sql
-- Gives the three roles added in 0022 real permissions, reversing part of the
-- consolidation documented in docs/DECISIONS.md §CE1/§C5 at the user's
-- explicit request (that section already marks the trade-off overrulable).
--
-- Scope, decided with the PRD role table (§2) and §5.8/§5.9/§8:
--   ops_manager      — gate/exit decisions, sticker sheets, out-scan, batch
--                       release, packer-productivity report, ID-photo view.
--   invoice_matcher  — invoice lookup/order-no/verify (CP5 first half).
--   warehouse_staff  — putaway, carved out of offloading.
-- Exception resolution, the audit log, provisioning and badge issuance stay
-- admin-only — the PRD assigns those to Admin specifically, and CE1's
-- reasoning for keeping provisioning off Ops is unaffected by this split.
-- Admin keeps covering every role (require_roles() unions with admin in
-- Python; is_ops_manager()/has_role() below do the same in SQL) — the same
-- "Admin covers the bench" pattern §CC3 already accepts for packer.

-- ===========================================================================
-- ROLE HELPER
-- ===========================================================================

create or replace function is_ops_manager()
returns boolean language sql stable as $$
  select auth_role() in ('admin', 'ops_manager');
$$;

-- ===========================================================================
-- CP1 — GATE ENTRY DECISION: Admin or Ops Manager
-- ===========================================================================

create or replace function fn_gate_entry_guard()
returns trigger
language plpgsql
as $$
declare
  v_approver_role user_role;
  v_verified_boxes int;
  v_total_boxes int;
  v_open_boxes int;
  v_unmatched int;
  v_lines int;
  v_reconciled int;
begin
  if new.status = 'inside' and old.status <> 'approved' then
    raise exception 'Vehicle cannot enter without Ops approval (CONTROL POINT 1).'
      using errcode = 'check_violation',
            hint = 'The entry is ' || old.status || '. Wait for the Ops Manager to approve.';
  end if;

  if not fn_gate_entry_transition_ok(old.status, new.status) then
    raise exception 'Illegal gate entry transition: % -> %', old.status, new.status
      using errcode = 'check_violation';
  end if;

  if new.status in ('approved', 'rejected') and old.status = 'pending_approval' then
    if new.decided_by is null then
      raise exception 'Gate entry decision requires an approver (CONTROL POINT 1).'
        using errcode = 'check_violation';
    end if;

    select role into v_approver_role from profiles where id = new.decided_by;

    if v_approver_role is null or v_approver_role not in ('admin', 'ops_manager') then
      raise exception
        'Only an Admin or Ops Manager may decide gate entries (CONTROL POINT 1). Got role: %',
        coalesce(v_approver_role::text, 'unknown')
        using errcode = 'insufficient_privilege';
    end if;

    if new.decided_at is null then
      new.decided_at := now();
    end if;
  end if;

  if new.status = 'inside' and new.time_in is null then
    new.time_in := now();
  end if;

  if new.status = 'box_verified' then
    select count(*) filter (where status <> 'pending'), count(*)
      into v_verified_boxes, v_total_boxes
      from boxes where gate_entry_id = new.id;

    if new.declared_box_count is null then
      raise exception 'Box count has not been declared (CONTROL POINT 2).'
        using errcode = 'check_violation';
    end if;

    if v_total_boxes <> new.declared_box_count
       or new.issued_box_sticker_count <> new.declared_box_count then
      raise exception
        'Count mismatch: declared %, stickers issued %, boxes created % (CONTROL POINT 2).',
        new.declared_box_count, new.issued_box_sticker_count, v_total_boxes
        using errcode = 'check_violation',
              hint = 'Contact the Ops team to reissue the sticker sheet.';
    end if;

    if v_verified_boxes <> v_total_boxes then
      raise exception
        'Count mismatch: % of % boxes scanned (CONTROL POINT 2).',
        v_verified_boxes, v_total_boxes
        using errcode = 'check_violation',
              hint = 'Contact the Ops team.';
    end if;
  end if;

  if new.status = 'offloaded' then
    select count(*) into v_open_boxes
      from boxes
     where gate_entry_id = new.id
       and status not in ('complete', 'short_accepted', 'rejected', 'emptied');

    if v_open_boxes > 0 then
      raise exception
        '% box(es) are still open or held. Resolve them before offloading completes (CONTROL POINT 3).',
        v_open_boxes
        using errcode = 'check_violation';
    end if;
  end if;

  if new.status = 'reconciled' then
    if new.purchase_order_id is null then
      raise exception 'Cannot reconcile a gate entry with no purchase order (CONTROL POINT 4).'
        using errcode = 'check_violation';
    end if;

    select count(*) into v_lines
      from purchase_order_lines where purchase_order_id = new.purchase_order_id;

    select count(*), count(*) filter (where not matched)
      into v_reconciled, v_unmatched
      from inbound_reconciliations where gate_entry_id = new.id;

    if v_reconciled < v_lines then
      raise exception
        'Inbound verification incomplete: % of % PO lines reconciled (CONTROL POINT 4).',
        v_reconciled, v_lines
        using errcode = 'check_violation';
    end if;

    if v_unmatched > 0 then
      raise exception
        'Inbound count does not match warehouse count on % line(s) (CONTROL POINT 4).',
        v_unmatched
        using errcode = 'check_violation';
    end if;
  end if;

  if new.status = 'departed' and new.time_out is null then
    new.time_out := now();
  end if;

  return new;
end;
$$;

-- ===========================================================================
-- CG4 — CARTON-COUNT (LOAD) APPROVAL: Admin or Ops Manager
-- ===========================================================================

create or replace function fn_load_decision_guard()
returns trigger
language plpgsql
as $$
declare
  v_approver_role user_role;
begin
  if new.status = old.status then
    return new;
  end if;

  if old.status <> 'pending' then
    raise exception 'This carton count has already been decided (%).', old.status
      using errcode = 'check_violation';
  end if;

  if new.decided_by is null then
    raise exception 'A decision requires a named approver.'
      using errcode = 'check_violation';
  end if;

  if new.decided_by = old.counted_by then
    raise exception
      'The person who counted the cartons cannot approve their own count.'
      using errcode = 'check_violation',
            hint = 'Ops must approve it.';
  end if;

  select role into v_approver_role from profiles where id = new.decided_by;

  if v_approver_role is null or v_approver_role not in ('admin', 'ops_manager') then
    raise exception
      'Only an Admin or Ops Manager may decide a carton count. Got role: %',
      coalesce(v_approver_role::text, 'unknown')
      using errcode = 'check_violation';
  end if;

  if new.decided_at is null then
    new.decided_at := now();
  end if;

  return new;
end;
$$;

-- ===========================================================================
-- CG4 — GATE EXIT APPROVAL: Admin or Ops Manager
-- ===========================================================================

create or replace function fn_pickup_guard()
returns trigger
language plpgsql
as $$
declare
  v_released int;
  v_scanned  int;
  v_batch    text;
  v_approver_role user_role;
begin
  if new.status = 'departed' and old.status = 'verifying' then
    raise exception
      'Vehicle cannot leave until every released carton is verified present '
      '(CONTROL POINT 7).'
      using errcode = 'check_violation',
            hint = 'Scan the remaining cartons onto the vehicle first.';
  end if;

  if new.status = 'departed' and old.status = 'verified' then
    raise exception
      'Vehicle % has not been approved to leave yet.', new.vehicle_number
      using errcode = 'check_violation',
            hint = 'Request exit approval, then Ops releases the gate.';
  end if;

  if not fn_pickup_transition_ok(old.status, new.status) then
    raise exception 'Illegal pickup transition: % -> %', old.status, new.status
      using errcode = 'check_violation';
  end if;

  if new.status = 'verified' and old.status = 'verifying' then
    select b.batch_code, count(pr.id), count(pr.exit_scanned_at)
      into v_batch, v_released, v_scanned
      from batches b
      left join packing_records pr on pr.batch_id = b.id
     where b.id = new.batch_id
     group by b.batch_code;

    if coalesce(v_released, 0) = 0 then
      raise exception 'Batch % has no cartons.', coalesce(v_batch, '?')
        using errcode = 'check_violation';
    end if;

    if v_scanned <> v_released then
      raise exception
        'Pickup %: % of % released cartons verified (CONTROL POINT 7).',
        new.pickup_code, v_scanned, v_released
        using errcode = 'check_violation',
              hint = 'The truck cannot leave until every carton is accounted for.';
    end if;

    if new.verified_by is null then
      raise exception 'Verification requires a named user (CONTROL POINT 7).'
        using errcode = 'check_violation';
    end if;

    if new.verified_at is null then
      new.verified_at := now();
    end if;
  end if;

  if new.status = 'verified' and old.status = 'exit_pending' then
    new.exit_approved_by := null;
    new.exit_approved_at := null;
    new.exit_requested_by := null;
    new.exit_requested_at := null;
  end if;

  if new.status = 'exit_pending' and old.status = 'verified' then
    new.exit_approved_by := null;
    new.exit_approved_at := null;

    if new.exit_requested_by is null then
      raise exception 'Requesting exit approval requires a named user.'
        using errcode = 'check_violation';
    end if;

    if new.exit_requested_at is null then
      new.exit_requested_at := now();
    end if;
  end if;

  if new.status = 'departed' then
    if new.exit_approved_by is null then
      raise exception 'The gate cannot open without a recorded Ops approval.'
        using errcode = 'check_violation',
              hint = 'Ops must approve the exit.';
    end if;

    if new.exit_approved_by = new.exit_requested_by then
      raise exception
        'The guard who requested exit cannot also approve it.'
        using errcode = 'check_violation',
              hint = 'Ops approves the exit.';
    end if;

    select role into v_approver_role from profiles where id = new.exit_approved_by;

    if v_approver_role is null or v_approver_role not in ('admin', 'ops_manager') then
      raise exception
        'Only an Admin or Ops Manager may approve a vehicle leaving. Got role: %',
        coalesce(v_approver_role::text, 'unknown')
        using errcode = 'check_violation';
    end if;

    if new.released_by is null then
      raise exception 'Releasing the vehicle requires a named user (CONTROL POINT 7).'
        using errcode = 'check_violation';
    end if;

    if new.time_out is null then
      new.time_out := now();
    end if;
  end if;

  return new;
end;
$$;

-- ===========================================================================
-- CP5 — WHO MAY HOLD THE MATCHER BADGE
-- ===========================================================================

create or replace function fn_badge_holder_guard()
returns trigger
language plpgsql
as $$
declare
  v_who       uuid;
  v_role      user_role;
  v_usable    boolean;
  v_allowed   user_role[];
  v_what      text;
begin
  if tg_table_name = 'invoice_verifications' then
    v_who := new.verified_by;
    v_allowed := array['invoice_matcher', 'admin']::user_role[];
    v_what := 'verify an invoice';
  else
    v_who := new.packed_by;
    v_allowed := array['packer', 'admin']::user_role[];
    v_what := 'pack a carton';
  end if;

  select role, (badge_active and is_active) into v_role, v_usable
    from profiles where id = v_who;

  if v_role is null then
    raise exception 'Unknown badge.' using errcode = 'foreign_key_violation';
  end if;

  if not v_usable then
    raise exception 'That badge has been deactivated.'
      using errcode = 'insufficient_privilege',
            hint = 'Ask an Admin to issue a replacement badge.';
  end if;

  if not (v_role = any(v_allowed)) then
    raise exception 'That badge is not permitted to % (CONTROL POINT 5).', v_what
      using errcode = 'insufficient_privilege';
  end if;

  return new;
end;
$$;

-- ===========================================================================
-- CP5 — WHO MAY HOLD A BADGE AT ALL
-- (admin_issue_badge's own eligibility check — BADGE_ROLES in
-- schemas/admin.py is only a copy of this, for the form to grey the button
-- out before hitting the rule.)
-- ===========================================================================

create or replace function admin_issue_badge(p_profile_id uuid)
returns text
language plpgsql
volatile
security definer
set search_path = public, extensions
as $$
declare
  v_role user_role;
  v_active boolean;
  v_code text;
begin
  if not is_admin() then
    raise exception 'Only an Admin can issue an attribution badge.'
      using errcode = 'insufficient_privilege',
            hint = 'Ask an Admin to issue or reissue this badge.';
  end if;

  select role, is_active into v_role, v_active
    from profiles where id = p_profile_id;

  if v_role is null then
    raise exception 'No staff member with that id.';
  end if;

  if not v_active then
    raise exception 'That account is deactivated, so it cannot hold a badge.'
      using hint = 'Reactivate the account first if this person is back on shift.';
  end if;

  if v_role not in ('packer', 'invoice_matcher', 'admin') then
    raise exception 'A % does not carry an attribution badge.', v_role
      using hint = 'Badges are for packers, invoice matchers, and the Admins '
                   'who cover either station.';
  end if;

  loop
    v_code := generate_badge_code();
    exit when not exists (select 1 from profiles where badge_code = v_code);
  end loop;

  update profiles
     set badge_code = v_code,
         badge_active = true
   where id = p_profile_id;

  return v_code;
end;
$$;

-- ===========================================================================
-- RLS — sticker issuance, box creation, gate/invoice/batch admin writes
-- now open to Ops Manager as well as Admin.
-- ===========================================================================

drop policy if exists sheets_write on sticker_sheets;
create policy sheets_write on sticker_sheets
  for all to authenticated using (is_ops_manager()) with check (is_ops_manager());

drop policy if exists stickers_insert on stickers;
create policy stickers_insert on stickers
  for insert to authenticated with check (is_ops_manager());

drop policy if exists boxes_insert on boxes;
create policy boxes_insert on boxes
  for insert to authenticated with check (is_ops_manager());

-- void_sheet (now ops_manager-guarded) sets stickers.status = 'void'. The
-- scanned-status advance stays with whoever scans (packer); voiding was
-- is_ops()-only (admin), the same silent-no-op risk as everything else in
-- this section.
drop policy if exists stickers_update on stickers;
create policy stickers_update on stickers
  for update to authenticated
  using (is_ops_manager() or has_role('packer'))
  with check (is_ops_manager() or has_role('packer'));

drop policy if exists gate_entries_update_ops on gate_entries;
create policy gate_entries_update_ops on gate_entries
  for update to authenticated
  using (is_ops_manager()) with check (is_ops_manager());

drop policy if exists invoices_write on invoices;
create policy invoices_write on invoices for all to authenticated
  using (is_ops_manager()) with check (is_ops_manager());

drop policy if exists batches_write on batches;
create policy batches_write on batches for all to authenticated
  using (is_ops_manager()) with check (is_ops_manager());

-- exceptions_resolve and audit_read move to Ops Manager as well as Admin —
-- PRD §8 Role-based Access is explicit: "Ops Manager can see everything,
-- approve exceptions, view reports" (§5.9's page being labelled "(Admin)" is
-- the same "Admin always passes" pattern as everywhere else in this app, not
-- a narrower grant). vendors_write, locations_write and po_write stay on
-- is_ops() (admin-only) — creating/editing vendors, racks and purchase
-- orders isn't in either role's PRD description.

drop policy if exists exceptions_resolve on exceptions;
create policy exceptions_resolve on exceptions
  for update to authenticated using (is_ops_manager()) with check (is_ops_manager());

-- po_lines_write is the one exception to "master data stays admin-only"
-- above: fn_apply_exception_resolution (0004) updates
-- purchase_order_lines.received_units/rejected_units as a side effect of
-- *resolving* an exception, under the resolving session's RLS — not of
-- editing a PO line directly. Since exception resolution just moved to Ops
-- Manager, this has to move with it or an ops_manager's accept-short/
-- reject-box resolution silently fails to update the PO's counters (no
-- error — the same silent-UPDATE-no-op DECISIONS.md Part D warns about).
drop policy if exists po_lines_write on purchase_order_lines;
create policy po_lines_write on purchase_order_lines
  for all to authenticated using (is_ops_manager()) with check (is_ops_manager());

drop policy if exists audit_read on audit_log;
create policy audit_read on audit_log
  for select to authenticated using (is_ops_manager());

-- Out-scan: Ops Manager as well as Admin. `scans_insert` was last redefined in
-- 0019_pack_unit_reconciliation.sql; this replaces that definition again.
drop policy if exists scans_insert on scan_events;
create policy scans_insert on scan_events
  for insert to authenticated
  with check (
    scanned_by = auth.uid()
    and (
      (scan_type = 'box_verify'
        and has_role('packer', 'admin'))
   or (scan_type = 'unit_verify'
        and has_role('packer', 'admin'))
   or (scan_type = 'pack_unit'
        and has_role('packer', 'admin'))
   or (scan_type = 'match_unit'
        and has_role('invoice_matcher', 'admin'))
   or (scan_type = 'out_scan'
        and has_role('admin', 'ops_manager'))
   or (scan_type = 'gate_exit'
        and has_role('security_guard', 'admin'))
    )
  );

-- ===========================================================================
-- CP5 — invoice_verifications insert policy
-- ===========================================================================

drop policy if exists inv_verif_insert on invoice_verifications;
create policy inv_verif_insert on invoice_verifications for insert to authenticated
  with check (has_role('invoice_matcher', 'admin'));

-- ===========================================================================
-- PICKUPS and BATCH_LOAD_APPROVALS UPDATE — Ops Manager decides both now
--
-- decide_exit() writes pickups.exit_approved_by/exit_approved_at; decide_count()
-- writes batch_load_approvals.decided_by/status. Both routes moved to
-- require_ops_manager; both underlying UPDATE policies were still
-- `has_role('security_guard', 'admin')` (0012, 0018) and would have silently
-- discarded an ops_manager's decision under RLS, exactly like the
-- packing_records case above.
-- ===========================================================================

drop policy if exists pickups_update on pickups;
create policy pickups_update on pickups
  for update to authenticated
  using (has_role('security_guard', 'ops_manager', 'admin'))
  with check (has_role('security_guard', 'ops_manager', 'admin'));

drop policy if exists load_approvals_update on batch_load_approvals;
create policy load_approvals_update on batch_load_approvals
  for update to authenticated
  using (has_role('security_guard', 'ops_manager', 'admin'))
  with check (has_role('security_guard', 'ops_manager', 'admin'));

-- ===========================================================================
-- PACKING_RECORDS UPDATE — Ops Manager now writes here too
--
-- create_batch() sets packing_records.batch_id, and out-scan's resolver
-- stamps out_scanned_at/out_scanned_by — both now performed by Ops Manager as
-- well as Admin, but the final packing_update policy (0012, superseding
-- 0009) was `has_role('security_guard', 'admin')`. Without this, both writes
-- would silently affect zero rows under RLS for an ops_manager session
-- (DECISIONS.md Part D's lesson again: adding a role to a step means
-- revisiting every table that step's writes touch, not just the one the
-- route's name suggests).
-- ===========================================================================

drop policy if exists packing_update on packing_records;
create policy packing_update on packing_records
  for update to authenticated
  using (has_role('security_guard', 'ops_manager', 'admin'))
  with check (has_role('security_guard', 'ops_manager', 'admin'));

-- ===========================================================================
-- ASSIGNMENT — the matcher hands a carton to a named packer
--
-- 0017's own comment already says "Matchers included: the matcher is usually
-- the person handing the box over" — but the check was `has_role('packer',
-- 'admin')`, which only worked because matching was folded into admin at the
-- time. Now that invoice_matcher is its own role, it has to be named here
-- explicitly, or the comment's claim is simply false.
-- ===========================================================================

drop policy if exists packing_assignments_insert on packing_assignments;
create policy packing_assignments_insert on packing_assignments
  for insert to authenticated
  with check (
    has_role('packer', 'invoice_matcher', 'admin')
    and assigned_by = auth.uid()
  );

drop policy if exists packing_assignments_update on packing_assignments;
create policy packing_assignments_update on packing_assignments
  for update to authenticated
  using (has_role('packer', 'invoice_matcher', 'admin'))
  with check (has_role('packer', 'invoice_matcher', 'admin'));

-- ===========================================================================
-- PUTAWAY moves to warehouse_staff, carved out of offloading. offloading keeps
-- inbound reconciliation (CP4) and receiving.
-- ===========================================================================

drop policy if exists putaways_insert on putaways;
create policy putaways_insert on putaways for insert to authenticated
  with check (has_role('warehouse_staff', 'admin') and moved_by = auth.uid());

-- boxes_update: warehouse_staff needs this so fn_putaway_close_box can still
-- mark a box 'emptied', and ops_manager needs it so fn_apply_exception_resolution
-- can still set a box 'short_accepted'/'rejected'/back-to-'verified' when an
-- ops_manager resolves the exception that triggers it (DECISIONS.md Part D —
-- a role gaining an action means every table that action's triggers touch
-- needs revisiting, or the update silently matches zero rows).
drop policy if exists boxes_update on boxes;
create policy boxes_update on boxes
  for update to authenticated
  using (has_role('packer', 'offloading', 'warehouse_staff', 'ops_manager', 'admin'))
  with check (has_role('packer', 'offloading', 'warehouse_staff', 'ops_manager', 'admin'));
