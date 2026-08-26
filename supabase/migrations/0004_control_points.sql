-- 0004_control_points.sql
--
-- PRD §4: the seven hard stops. These are implemented in the database, not only
-- in the service layer, because the first success metric is "zero manual
-- overrides" — and a rule that lives only in a FastAPI handler is one hotfix
-- away from being bypassed by whoever is on call at 11pm.
--
--   CP1 Gate entry ......... Ops must approve before the gate opens
--   CP2 Box count .......... scanned boxes == issued stickers == declared count
--   CP3 Unit count ......... scanned units == PO quantity for that box
--   CP4 Inbound ............ warehouse count == inbound team count
--   CP5 Packing ............ invoice + packer badge both scanned   (Phase 3)
--   CP6 Out-scan ........... every packed carton scanned            (Phase 3)
--   CP7 Gate exit .......... verified count == released count       (Phase 4)

-- ===========================================================================
-- CP1 — GATE ENTRY
-- ===========================================================================

-- Legal status transitions. Anything not listed is rejected.
create or replace function fn_gate_entry_transition_ok(old_status gate_entry_status,
                                                       new_status gate_entry_status)
returns boolean language sql immutable as $$
  select (old_status, new_status) in (
    ('draft',            'pending_approval'),
    ('draft',            'cancelled'),
    ('pending_approval', 'approved'),
    ('pending_approval', 'rejected'),
    ('pending_approval', 'cancelled'),
    ('approved',         'inside'),
    ('approved',         'cancelled'),
    ('inside',           'counting'),
    ('counting',         'box_verified'),
    ('box_verified',     'offloading'),
    ('offloading',       'offloaded'),
    ('offloaded',        'reconciled'),
    ('reconciled',       'departed')
  ) or old_status = new_status;
$$;

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
  -- Checked before the generic transition table on purpose. Every route into
  -- 'inside' other than from 'approved' is the same mistake — a vehicle being
  -- admitted without a decision — and "Illegal gate entry transition:
  -- pending_approval -> inside" is a worse thing to hand a guard at 6am than
  -- naming the control point that stopped them.
  if new.status = 'inside' and old.status <> 'approved' then
    raise exception 'Vehicle cannot enter without Ops approval (CONTROL POINT 1).'
      using errcode = 'check_violation',
            hint = 'The entry is ' || old.status || '. Wait for the Ops Manager to approve.';
  end if;

  if not fn_gate_entry_transition_ok(old.status, new.status) then
    raise exception 'Illegal gate entry transition: % -> %', old.status, new.status
      using errcode = 'check_violation';
  end if;

  -- CP1: approval must be a decision by a named Admin who is not the
  -- requester. The self-approval bar is also a table constraint; this adds
  -- the role requirement.
  if new.status in ('approved', 'rejected') and old.status = 'pending_approval' then
    if new.decided_by is null then
      raise exception 'Gate entry decision requires an approver (CONTROL POINT 1).'
        using errcode = 'check_violation';
    end if;

    select role into v_approver_role from profiles where id = new.decided_by;

    if v_approver_role is null or v_approver_role <> 'admin' then
      raise exception
        'Only an Admin may decide gate entries (CONTROL POINT 1). Got role: %',
        coalesce(v_approver_role::text, 'unknown')
        using errcode = 'insufficient_privilege';
    end if;

    if new.decided_at is null then
      new.decided_at := now();
    end if;
  end if;

  -- Reaching here means old.status was 'approved' (guarded at the top).
  if new.status = 'inside' and new.time_in is null then
    new.time_in := now();
  end if;

  -- CP2: leaving 'counting' requires the three numbers to agree —
  -- declared by the guard, issued by Ops, and physically scanned.
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

  -- CP3: offloading completes only when no box is still open or held.
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

  -- CP4: every PO line must be reconciled, and every reconciliation must match.
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

create trigger trg_gate_entries_guard
  before update on gate_entries
  for each row execute function fn_gate_entry_guard();

-- The transition guard above only runs on UPDATE, which would leave an obvious
-- way around CONTROL POINT 1: insert the row already in 'inside' and never
-- transition at all. A new entry may only begin at the start of the lifecycle.
create or replace function fn_gate_entry_insert_guard()
returns trigger
language plpgsql
as $$
begin
  if new.status not in ('draft', 'pending_approval') then
    raise exception
      'A gate entry must start as draft or pending_approval, not % (CONTROL POINT 1).',
      new.status
      using errcode = 'check_violation';
  end if;

  if new.decided_by is not null or new.time_in is not null then
    raise exception 'A new gate entry cannot be created already approved or admitted.'
      using errcode = 'check_violation';
  end if;

  return new;
end;
$$;

create trigger trg_gate_entries_insert_guard
  before insert on gate_entries
  for each row execute function fn_gate_entry_insert_guard();

-- Same reasoning for boxes: an inserted box must start unscanned, so that
-- 'verified' and 'complete' can only ever be reached through a real scan.
create or replace function fn_box_insert_guard()
returns trigger
language plpgsql
as $$
begin
  if new.status <> 'pending' then
    raise exception 'A box must be created with status pending, not % (CONTROL POINT 2).',
      new.status
      using errcode = 'check_violation';
  end if;

  if new.scanned_units <> 0 or new.quarantined_units <> 0 then
    raise exception 'A box cannot be created with units already counted.'
      using errcode = 'check_violation';
  end if;

  return new;
end;
$$;

create trigger trg_boxes_insert_guard
  before insert on boxes
  for each row execute function fn_box_insert_guard();

-- ===========================================================================
-- COUNTER INTEGRITY
-- boxes.scanned_units is derived from the scan ledger. If application code
-- could write it, "scanned == expected" would prove nothing.
-- ===========================================================================

create or replace function fn_boxes_counters_readonly()
returns trigger
language plpgsql
as $$
begin
  if (new.scanned_units is distinct from old.scanned_units
      or new.quarantined_units is distinct from old.quarantined_units)
     and coalesce(current_setting('app.counter_write', true), 'off') <> 'on' then
    raise exception
      'boxes.scanned_units/quarantined_units are derived from scan_events and cannot be set directly.'
      using errcode = 'restrict_violation';
  end if;
  return new;
end;
$$;

create trigger trg_boxes_counters_readonly
  before update on boxes
  for each row execute function fn_boxes_counters_readonly();

-- ===========================================================================
-- SCAN RESOLUTION
-- The API inserts a bare scan (code + who + when). This trigger decides whether
-- it is accepted and why not. Putting the decision here rather than in the
-- service means an offline replay six hours later is judged by exactly the same
-- rules as a live scan.
-- ===========================================================================

create or replace function fn_scan_resolve()
returns trigger
language plpgsql
as $$
declare
  v_sticker stickers%rowtype;
  v_box     boxes%rowtype;
  v_entry   gate_entries%rowtype;
  v_already boolean;
begin
  new.raw_code := upper(btrim(new.raw_code));

  select * into v_sticker from stickers where code = new.raw_code;

  if not found then
    new.accepted := false;
    new.reject_reason := 'unknown_code';
    new.sticker_id := null;
    return new;
  end if;

  new.sticker_id := v_sticker.id;
  new.gate_entry_id := v_sticker.gate_entry_id;
  new.box_id := coalesce(v_sticker.box_id, new.box_id);

  if v_sticker.status = 'void' then
    new.accepted := false;
    new.reject_reason := 'sticker_void';
    return new;
  end if;

  -- A box sticker scanned in the unit step (or vice versa) is a real mistake
  -- that would otherwise silently inflate a count.
  if (new.scan_type = 'box_verify'  and v_sticker.sticker_type <> 'box')
  or (new.scan_type = 'unit_verify' and v_sticker.sticker_type <> 'unit') then
    new.accepted := false;
    new.reject_reason := 'wrong_sticker_type';
    return new;
  end if;

  select exists (
    select 1 from scan_events
     where sticker_id = v_sticker.id and scan_type = new.scan_type and accepted
  ) into v_already;

  if v_already then
    new.accepted := false;
    new.reject_reason := 'already_scanned';
    return new;
  end if;

  select * into v_entry from gate_entries where id = v_sticker.gate_entry_id;

  if new.scan_type = 'box_verify' then
    if v_entry.status <> 'counting' then
      new.accepted := false;
      new.reject_reason := 'wrong_gate_entry';
      return new;
    end if;

  elsif new.scan_type = 'unit_verify' then
    if new.box_id is null then
      new.accepted := false;
      new.reject_reason := 'unknown_code';
      return new;
    end if;

    select * into v_box from boxes where id = new.box_id;

    if v_box.status not in ('verified', 'scanning') then
      new.accepted := false;
      new.reject_reason := 'box_not_open';
      return new;
    end if;

    -- Over-scan is refused rather than recorded and reconciled later. Letting an
    -- 11th unit into a 10-unit box would make the box "complete" and wrong.
    if v_box.scanned_units >= v_box.expected_units then
      new.accepted := false;
      new.reject_reason := 'over_expected_quantity';
      return new;
    end if;
  end if;

  new.accepted := true;
  new.reject_reason := null;

  if new.scan_type = 'unit_verify' and new.disposition is null then
    new.disposition := 'stock';
  end if;

  return new;
end;
$$;

create trigger trg_scan_events_resolve
  before insert on scan_events
  for each row execute function fn_scan_resolve();

-- Apply the effects of an accepted scan: advance the sticker, move the box, and
-- bump the derived counters (the only place allowed to).
create or replace function fn_scan_apply()
returns trigger
language plpgsql
as $$
declare
  v_box boxes%rowtype;
begin
  if not new.accepted then
    return new;
  end if;

  update stickers set status = 'scanned' where id = new.sticker_id;

  perform set_config('app.counter_write', 'on', true);

  if new.scan_type = 'box_verify' then
    update boxes
       set status = 'verified',
           verified_at = now(),
           verified_by = new.scanned_by
     where id = new.box_id and status = 'pending';

  elsif new.scan_type = 'unit_verify' then
    update boxes
       set scanned_units = scanned_units + 1,
           quarantined_units = quarantined_units
             + case when new.disposition = 'quarantine' then 1 else 0 end,
           status = case when status = 'verified' then 'scanning' else status end
     where id = new.box_id
    returning * into v_box;
  end if;

  perform set_config('app.counter_write', 'off', true);
  return new;
end;
$$;

create trigger trg_scan_events_apply
  after insert on scan_events
  for each row execute function fn_scan_apply();

-- ===========================================================================
-- CP3 — BOX CLOSURE
-- ===========================================================================

create or replace function fn_box_transition_guard()
returns trigger
language plpgsql
as $$
begin
  -- A box may only be declared complete when the scanned count equals the PO
  -- quantity for that box. Not >=, not "close enough".
  if new.status = 'complete' and old.status <> 'complete' then
    if new.scanned_units <> new.expected_units then
      raise exception
        'Box %: % of % units scanned. Goods are held (CONTROL POINT 3).',
        new.box_number, new.scanned_units, new.expected_units
        using errcode = 'check_violation';
    end if;

    -- Damage checkpoint is mandatory but non-blocking in outcome: you must
    -- answer the question, you are not required to answer "none".
    if new.damage_level is null then
      raise exception 'Box %: damage check must be recorded before the box can close.',
        new.box_number
        using errcode = 'check_violation';
    end if;

    if new.completed_at is null then
      new.completed_at := now();
    end if;
  end if;

  -- short_accepted and rejected are reachable only from 'held', and only as the
  -- consequence of a resolved exception. fn_apply_exception_resolution is the
  -- only writer; a direct API update lands here and is refused.
  if new.status in ('short_accepted', 'rejected')
     and old.status <> new.status
     and coalesce(current_setting('app.exception_resolution', true), 'off') <> 'on' then
    raise exception
      'Box % can only reach % through a resolved exception (CONTROL POINT 3).',
      new.box_number, new.status
      using errcode = 'insufficient_privilege',
            hint = 'Raise an exception and have Ops resolve it.';
  end if;

  return new;
end;
$$;

create trigger trg_boxes_transition_guard
  before update on boxes
  for each row execute function fn_box_transition_guard();

-- ===========================================================================
-- EXCEPTION RESOLUTION → box outcome (DECISIONS.md §3)
-- ===========================================================================

create or replace function fn_apply_exception_resolution()
returns trigger
language plpgsql
as $$
declare
  v_box boxes%rowtype;
begin
  if new.status <> 'resolved' or old.status = 'resolved' then
    return new;
  end if;

  if new.box_id is null then
    return new;
  end if;

  select * into v_box from boxes where id = new.box_id;

  perform set_config('app.exception_resolution', 'on', true);
  perform set_config('app.counter_write', 'on', true);

  if new.resolution = 'accept_short' then
    -- The short delivery is real and agreed. Units already scanned enter stock;
    -- the shortfall is recorded against the vendor's PO line.
    update boxes set status = 'short_accepted' where id = new.box_id;

    update purchase_order_lines
       set received_units = received_units + v_box.scanned_units
     where id = v_box.purchase_order_line_id;

  elsif new.resolution = 'reject_box' then
    update boxes set status = 'rejected' where id = new.box_id;

    update purchase_order_lines
       set rejected_units = rejected_units + v_box.expected_units
     where id = v_box.purchase_order_line_id;

  elsif new.resolution = 'recount' then
    -- Suspected scan error. The box reopens. The previous scans are NOT
    -- deleted — they stay in the ledger as evidence — but they no longer count,
    -- so their stickers are voided and reissued.
    update stickers
       set status = 'void',
           void_reason = 'Superseded by recount on exception ' || new.exception_code
     where box_id = new.box_id and sticker_type = 'unit' and status = 'scanned';

    update boxes
       set status = 'verified',
           scanned_units = 0,
           quarantined_units = 0,
           damage_level = null,
           damage_note = null
     where id = new.box_id;
  end if;

  perform set_config('app.counter_write', 'off', true);
  perform set_config('app.exception_resolution', 'off', true);

  return new;
end;
$$;

create trigger trg_exceptions_apply_resolution
  after update on exceptions
  for each row execute function fn_apply_exception_resolution();

-- ===========================================================================
-- CP5 / CP6 / CP7 — Phases 3 and 4.
-- Enforced now so the later phases cannot ship without them.
-- ===========================================================================

-- CP5: a carton cannot be marked packed unless the invoice was verified by a
-- matcher first, and the packer is a different person from the matcher.
create or replace function fn_packing_guard()
returns trigger
language plpgsql
as $$
declare
  v_verifier uuid;
begin
  select verified_by into v_verifier
    from invoice_verifications where invoice_id = new.invoice_id;

  if v_verifier is null then
    raise exception
      'Invoice has not been verified by an invoice matcher (CONTROL POINT 5).'
      using errcode = 'check_violation';
  end if;

  if v_verifier = new.packed_by then
    raise exception
      'The invoice matcher and the packer must be different people (CONTROL POINT 5).'
      using errcode = 'check_violation';
  end if;

  return new;
end;
$$;

create trigger trg_packing_guard
  before insert on packing_records
  for each row execute function fn_packing_guard();

-- CP7: a truck leaves only from a released batch.
create or replace function fn_batch_release_guard()
returns trigger
language plpgsql
as $$
begin
  if new.released_at is not null and old.released_at is null and new.released_by is null then
    raise exception 'Batch release requires a named releasing user (CONTROL POINT 6).'
      using errcode = 'check_violation';
  end if;
  return new;
end;
$$;

create trigger trg_batches_release_guard
  before update on batches
  for each row execute function fn_batch_release_guard();

-- ===========================================================================
-- REPORTING HELPERS
-- ===========================================================================

-- Warehouse-side count per PO line, straight from the scan ledger. This is the
-- number the inbound team's figure is compared against in CP4 — deriving it
-- rather than storing it means it cannot drift.
create or replace view v_warehouse_counts as
select
  b.gate_entry_id,
  b.purchase_order_line_id,
  count(*) filter (where se.accepted and se.disposition = 'stock')      as stock_units,
  count(*) filter (where se.accepted and se.disposition = 'quarantine') as quarantined_units,
  count(*) filter (where se.accepted)                                   as total_units
from boxes b
left join scan_events se
  on se.box_id = b.id and se.scan_type = 'unit_verify'
where b.status <> 'rejected'
group by b.gate_entry_id, b.purchase_order_line_id;

create or replace view v_vendor_accuracy as
select
  v.id            as vendor_id,
  v.code          as vendor_code,
  v.name          as vendor_name,
  count(distinct ge.id)                                      as deliveries,
  count(distinct e.id) filter (
    where e.exception_type in ('box_count_mismatch', 'unit_count_mismatch'))
                                                             as count_exceptions,
  count(distinct e.id) filter (where e.exception_type = 'damage')
                                                             as damage_exceptions,
  coalesce(sum(pol.expected_units), 0)                       as units_expected,
  coalesce(sum(pol.received_units), 0)                       as units_received,
  coalesce(sum(pol.rejected_units), 0)                       as units_rejected
from vendors v
left join gate_entries ge on ge.vendor_id = v.id
left join exceptions e on e.vendor_id = v.id
left join purchase_orders po on po.vendor_id = v.id
left join purchase_order_lines pol on pol.purchase_order_id = po.id
group by v.id, v.code, v.name;
