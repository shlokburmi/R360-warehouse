-- 0012_pickup.sql
-- PHASE 4 — Pickup verification and gate exit. PRD §5.7, Step 10, CONTROL POINT 7.
--
-- The last hard stop, and the one with the least room for argument: a vehicle
-- leaves with exactly the cartons that were released to it, or it does not
-- leave. Everything before this point can be corrected inside the building.
-- Once a truck is on the road, a missing carton is somebody else's problem and
-- nobody's record.
--
-- Outbound gets its own table rather than reusing `gate_entries`. An inbound
-- entry is about a vendor, a PO and a box count; an outbound one is about a
-- batch and a carton count. Overloading one table with a direction flag would
-- mean half the columns are always null and every query needs a filter it is
-- easy to forget.

-- ===========================================================================
-- PICKUPS
-- ===========================================================================

create table pickups (
  id                uuid primary key default gen_random_uuid(),
  pickup_code       text not null unique,          -- PU-20260810-0001
  status            pickup_status not null default 'registered',

  -- One pickup per batch. A batch is released as a unit for a single vehicle;
  -- allowing several would make "all cartons present" ambiguous about which
  -- cartons belong to which truck.
  batch_id          uuid not null unique references batches(id) on delete restrict,

  vehicle_number    text not null check (vehicle_number ~ '^[A-Z0-9-]{4,15}$'),
  courier_name      text,
  transporter_name  text,

  registered_by     uuid not null references profiles(id),
  registered_at     timestamptz not null default now(),

  verified_by       uuid references profiles(id),
  verified_at       timestamptz,

  released_by       uuid references profiles(id),
  time_in           timestamptz not null default now(),
  time_out          timestamptz,

  cancel_reason     text,

  constraint pickups_time_order check (time_out is null or time_out >= time_in),
  constraint pickups_verified_complete
    check (num_nonnulls(verified_by, verified_at) <> 1),
  constraint pickups_cancel_has_reason
    check (status <> 'cancelled' or length(btrim(coalesce(cancel_reason, ''))) > 0)
);

create index pickups_status_idx on pickups(status, registered_at desc);
create index pickups_batch_idx on pickups(batch_id);

create or replace function fn_set_pickup_code()
returns trigger language plpgsql as $$
begin
  if new.pickup_code is null or new.pickup_code = '' then
    new.pickup_code := next_code('PU');
  end if;
  return new;
end;
$$;

create trigger trg_pickups_code before insert on pickups
  for each row execute function fn_set_pickup_code();

-- People on the collecting vehicle. Same shape as gate_entry_persons, and the
-- same identity rules apply — a first-time courier is photographed once.
create table pickup_persons (
  id            uuid primary key default gen_random_uuid(),
  pickup_id     uuid not null references pickups(id) on delete restrict,
  visitor_id    uuid not null references visitors(id) on delete restrict,
  visitor_role  visitor_role not null,
  id_photo_path text,
  created_at    timestamptz not null default now(),

  unique (pickup_id, visitor_id)
);

create index pickup_persons_pickup_idx on pickup_persons(pickup_id);

-- Exit scanning stamps the carton, so "which cartons physically left" is a
-- recorded fact rather than an inference from the batch.
alter table packing_records
  add column if not exists exit_scanned_at timestamptz,
  add column if not exists exit_scanned_by uuid references profiles(id);

-- ===========================================================================
-- A pickup can only be registered against a released batch
-- ===========================================================================

create or replace function fn_pickup_insert_guard()
returns trigger
language plpgsql
as $$
declare
  v_batch batches%rowtype;
begin
  select * into v_batch from batches where id = new.batch_id;

  if not found then
    raise exception 'Batch not found.' using errcode = 'foreign_key_violation';
  end if;

  -- Registering a pickup for an unreleased batch would let a truck start
  -- loading goods that Ops has not finished out-scanning.
  if v_batch.status <> 'released' then
    raise exception
      'Batch % has not been released for pickup (it is %).',
      v_batch.batch_code, v_batch.status
      using errcode = 'check_violation',
            hint = 'Ops must complete the out-scan and release the batch first.';
  end if;

  if new.status <> 'registered' then
    raise exception 'A pickup must start as registered, not %.', new.status
      using errcode = 'check_violation';
  end if;

  if new.time_out is not null or new.verified_at is not null then
    raise exception 'A new pickup cannot be created already verified or departed.'
      using errcode = 'check_violation';
  end if;

  return new;
end;
$$;

create trigger trg_pickups_insert_guard
  before insert on pickups
  for each row execute function fn_pickup_insert_guard();

-- ===========================================================================
-- CONTROL POINT 7 — verified count must equal released count
-- ===========================================================================

create or replace function fn_pickup_transition_ok(old_status pickup_status,
                                                   new_status pickup_status)
returns boolean language sql immutable as $$
  select (old_status, new_status) in (
    ('registered', 'verifying'),
    ('registered', 'cancelled'),
    ('verifying',  'verified'),
    ('verifying',  'cancelled'),
    ('verified',   'departed'),
    ('verified',   'cancelled')
  ) or old_status = new_status;
$$;

create or replace function fn_pickup_guard()
returns trigger
language plpgsql
as $$
declare
  v_released int;
  v_scanned  int;
  v_batch    text;
begin
  -- Specific message before the generic transition table, for the same reason as
  -- the gate and batch guards: every other route to 'departed' is the same
  -- mistake, and naming the control point is what makes it actionable.
  if new.status = 'departed' and old.status <> 'verified' then
    raise exception
      'Vehicle cannot leave until every released carton is verified present '
      '(CONTROL POINT 7).'
      using errcode = 'check_violation',
            hint = 'Scan the remaining cartons onto the vehicle first.';
  end if;

  if not fn_pickup_transition_ok(old.status, new.status) then
    raise exception 'Illegal pickup transition: % -> %', old.status, new.status
      using errcode = 'check_violation';
  end if;

  if new.status = 'verified' and old.status <> 'verified' then
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

    -- The comparison that matters: released against physically present.
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

  if new.status = 'departed' then
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

create trigger trg_pickups_guard
  before update on pickups
  for each row execute function fn_pickup_guard();

-- ===========================================================================
-- GATE-EXIT SCANNING
--
-- Same carton label as out-scan (the invoice number), one step further along.
-- ===========================================================================

create or replace function fn_scan_resolve()
returns trigger
language plpgsql
as $$
declare
  v_sticker stickers%rowtype;
  v_box     boxes%rowtype;
  v_entry   gate_entries%rowtype;
  v_invoice invoices%rowtype;
  v_packing packing_records%rowtype;
  v_batch   batches%rowtype;
  v_pickup  pickups%rowtype;
  v_already boolean;
begin
  new.raw_code := upper(btrim(new.raw_code));

  -- ---- Carton scans (out_scan / gate_exit) resolve against invoices --------
  if new.scan_type in ('out_scan', 'gate_exit') then
    select * into v_invoice from invoices
     where upper(invoice_number) = new.raw_code;

    if not found then
      new.accepted := false;
      new.reject_reason := 'unknown_code';
      return new;
    end if;

    new.invoice_id := v_invoice.id;

    select * into v_packing from packing_records where invoice_id = v_invoice.id;

    if not found then
      new.accepted := false;
      new.reject_reason := 'not_packed';
      return new;
    end if;

    if v_packing.batch_id is null then
      new.accepted := false;
      new.reject_reason := 'not_in_batch';
      return new;
    end if;

    select * into v_batch from batches where id = v_packing.batch_id;

    if new.scan_type = 'out_scan' then
      if v_batch.status not in ('open', 'scanning') then
        new.accepted := false;
        new.reject_reason := 'batch_closed';
        return new;
      end if;

      if v_packing.out_scanned_at is not null then
        new.accepted := false;
        new.reject_reason := 'already_scanned';
        return new;
      end if;

    else  -- gate_exit
      -- Nothing leaves that Ops has not released. This is what stops a carton
      -- being loaded straight from the packing bench onto a truck.
      if v_batch.status <> 'released' then
        new.accepted := false;
        new.reject_reason := 'batch_not_released';
        return new;
      end if;

      select * into v_pickup from pickups where batch_id = v_batch.id;

      if not found then
        new.accepted := false;
        new.reject_reason := 'no_pickup_registered';
        return new;
      end if;

      if v_pickup.status not in ('registered', 'verifying') then
        new.accepted := false;
        new.reject_reason := 'wrong_pickup';
        return new;
      end if;

      if v_packing.exit_scanned_at is not null then
        new.accepted := false;
        new.reject_reason := 'already_scanned';
        return new;
      end if;
    end if;

    new.accepted := true;
    new.reject_reason := null;
    return new;
  end if;

  -- ---- Sticker-based scans (box_verify / unit_verify) ----------------------
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

-- Applying a gate-exit scan: stamp the carton and move the pickup into
-- 'verifying'. 'verified' is not set here — that is CONTROL POINT 7 and belongs
-- to an explicit act by the guard, not to a side effect of the last scan.
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

  if new.scan_type = 'out_scan' then
    update packing_records
       set out_scanned_at = now(), out_scanned_by = new.scanned_by
     where invoice_id = new.invoice_id;

    update batches b
       set status = 'scanning'
     where b.id = (select batch_id from packing_records where invoice_id = new.invoice_id)
       and b.status = 'open';

    return new;
  end if;

  if new.scan_type = 'gate_exit' then
    update packing_records
       set exit_scanned_at = now(), exit_scanned_by = new.scanned_by
     where invoice_id = new.invoice_id;

    update pickups p
       set status = 'verifying'
     where p.batch_id = (
             select batch_id from packing_records where invoice_id = new.invoice_id
           )
       and p.status = 'registered';

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

-- ===========================================================================
-- Reads
-- ===========================================================================

create or replace view v_pickup_status as
select
  p.id                                            as pickup_id,
  p.pickup_code,
  p.status::text                                  as status,
  p.vehicle_number,
  p.courier_name,
  p.transporter_name,
  p.batch_id,
  b.batch_code,
  count(pr.id)::int                               as released_cartons,
  count(pr.exit_scanned_at)::int                  as verified_cartons,
  (count(pr.id) - count(pr.exit_scanned_at))::int as remaining_cartons,
  p.registered_at,
  rb.full_name                                    as registered_by_name,
  p.verified_at,
  vb.full_name                                    as verified_by_name,
  p.time_in,
  p.time_out,
  lb.full_name                                    as released_by_name
from pickups p
join batches b on b.id = p.batch_id
left join packing_records pr on pr.batch_id = b.id
left join profiles rb on rb.id = p.registered_by
left join profiles vb on vb.id = p.verified_by
left join profiles lb on lb.id = p.released_by
group by p.id, p.pickup_code, p.status, p.vehicle_number, p.courier_name,
         p.transporter_name, p.batch_id, b.batch_code, p.registered_at,
         rb.full_name, p.verified_at, vb.full_name, p.time_in, p.time_out,
         lb.full_name;

alter view v_pickup_status set (security_invoker = true);
grant select on v_pickup_status to authenticated;

-- ===========================================================================
-- RLS
-- ===========================================================================

alter table pickups enable row level security;
alter table pickups force row level security;
alter table pickup_persons enable row level security;
alter table pickup_persons force row level security;

revoke all on pickups, pickup_persons from anon, authenticated;
grant select, insert, update on pickups to authenticated;
grant select, insert on pickup_persons to authenticated;

-- Everyone can see the outbound queue; only the gate and Ops touch it.
create policy pickups_read on pickups
  for select to authenticated using (true);

create policy pickups_insert on pickups
  for insert to authenticated
  with check (
    has_role('security_guard', 'ops_manager', 'admin')
    and registered_by = auth.uid()
  );

create policy pickups_update on pickups
  for update to authenticated
  using (has_role('security_guard', 'ops_manager', 'admin'))
  with check (has_role('security_guard', 'ops_manager', 'admin'));

create policy pickup_persons_read on pickup_persons
  for select to authenticated
  using (has_role('security_guard', 'ops_manager', 'admin'));

create policy pickup_persons_insert on pickup_persons
  for insert to authenticated
  with check (has_role('security_guard', 'ops_manager', 'admin'));

-- The gate-exit scan stamps packing_records, and gate_exit scans are already
-- restricted to guards and Ops by the scan_events insert policy — so the
-- existing Ops-only update policy is not enough.
drop policy if exists packing_update on packing_records;

create policy packing_update on packing_records
  for update to authenticated
  using (has_role('security_guard', 'ops_manager', 'admin'))
  with check (has_role('security_guard', 'ops_manager', 'admin'));

-- Audit and no-delete for the new tables.
do $$
declare t text;
begin
  foreach t in array array['pickups', 'pickup_persons'] loop
    execute format(
      'create trigger trg_%1$s_audit after insert or update on %1$I
         for each row execute function fn_audit()', t);
    execute format(
      'create trigger trg_%1$s_no_delete before delete on %1$I
         for each row execute function fn_no_delete()', t);
  end loop;
end;
$$;
