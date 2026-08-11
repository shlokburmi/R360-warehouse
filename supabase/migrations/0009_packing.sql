-- 0009_packing.sql
-- PHASE 3 — Invoice matching, packing attribution, out-scan, batch release.
-- PRD Steps 7-9, §5.4-5.6, CONTROL POINTS 5 and 6.
--
-- The purpose of this phase is attribution. By the end of it every carton that
-- leaves the building can answer three questions without anyone having to
-- remember anything: which invoice is inside, who verified it against the goods,
-- and who packed it. That turns an error from "the packing team, sometime
-- Tuesday" into a named person and a timestamp.
--
-- Badges capture that attribution. A badge proves *who handled this*; it grants
-- no privileges and cannot be used to log in (docs/DECISIONS.md §1).

-- ===========================================================================
-- INVOICES
-- ===========================================================================

alter table invoices
  add column if not exists customer_name text,
  add column if not exists closed_at timestamptz,
  add column if not exists closed_reason text;

-- ===========================================================================
-- BATCHES
-- ===========================================================================

alter table batches
  add column if not exists status batch_status not null default 'open',
  add column if not exists planned_carton_count int not null default 0
    check (planned_carton_count >= 0),
  add column if not exists created_by uuid references profiles(id),
  add column if not exists notes text;

create index if not exists batches_status_idx on batches(status, created_at desc);

create or replace function fn_set_batch_code()
returns trigger language plpgsql as $$
begin
  if new.batch_code is null or new.batch_code = '' then
    new.batch_code := next_code('BT');
  end if;
  return new;
end;
$$;

create trigger trg_batches_code before insert on batches
  for each row execute function fn_set_batch_code();

-- ===========================================================================
-- PACKING RECORDS gain their batch and out-scan state
-- ===========================================================================

alter table packing_records
  add column if not exists batch_id uuid references batches(id) on delete restrict,
  add column if not exists out_scanned_at timestamptz,
  add column if not exists out_scanned_by uuid references profiles(id);

create index if not exists packing_records_batch_idx on packing_records(batch_id);
create index if not exists packing_records_unbatched_idx on packing_records(packed_at)
  where batch_id is null;

-- ===========================================================================
-- Derived invoice lifecycle.
--
-- Derived rather than a stored status column: a stored one would need updating
-- from four different places and would eventually disagree with the records it
-- is supposed to summarise.
-- ===========================================================================

create or replace view v_invoice_status as
select
  i.id                        as invoice_id,
  i.invoice_number,
  i.sku,
  i.units,
  i.customer_name,
  i.is_open,
  i.purchase_order_line_id,
  pol.description,
  iv.verified_by,
  vp.full_name                as verified_by_name,
  iv.verified_at,
  pr.id                       as packing_record_id,
  pr.packed_by,
  pp.full_name                as packed_by_name,
  pr.packed_at,
  pr.batch_id,
  b.batch_code,
  b.status::text              as batch_status,
  pr.out_scanned_at,
  case
    when not i.is_open                  then 'closed'
    when pr.out_scanned_at is not null  then 'out_scanned'
    when pr.batch_id is not null        then 'batched'
    when pr.id is not null              then 'packed'
    when iv.id is not null              then 'verified'
    else 'open'
  end                         as stage
from invoices i
left join purchase_order_lines pol on pol.id = i.purchase_order_line_id
left join invoice_verifications iv on iv.invoice_id = i.id
left join profiles vp on vp.id = iv.verified_by
left join packing_records pr on pr.invoice_id = i.id
left join profiles pp on pp.id = pr.packed_by
left join batches b on b.id = pr.batch_id;

create or replace view v_batch_status as
select
  b.id                                              as batch_id,
  b.batch_code,
  b.status::text                                    as status,
  b.planned_carton_count,
  count(pr.id)::int                                 as assigned_cartons,
  count(pr.out_scanned_at)::int                     as scanned_cartons,
  (count(pr.id) - count(pr.out_scanned_at))::int    as remaining_cartons,
  b.created_at,
  cb.full_name                                      as created_by_name,
  b.released_at,
  rb.full_name                                      as released_by_name,
  b.notes
from batches b
left join packing_records pr on pr.batch_id = b.id
left join profiles cb on cb.id = b.created_by
left join profiles rb on rb.id = b.released_by
group by b.id, b.batch_code, b.status, b.planned_carton_count,
         b.created_at, cb.full_name, b.released_at, rb.full_name, b.notes;

grant select on v_invoice_status, v_batch_status to authenticated;

-- ===========================================================================
-- CONTROL POINT 5 — packing needs a verified invoice and a second person
-- ===========================================================================

-- Replaces the Phase-1 version: adds the open-invoice check and a message that
-- names the verifier. The two-person rule is unchanged and is the point.
create or replace function fn_packing_guard()
returns trigger
language plpgsql
as $$
declare
  v_verifier uuid;
  v_verifier_name text;
  v_open boolean;
  v_number text;
begin
  select i.is_open, i.invoice_number into v_open, v_number
    from invoices i where i.id = new.invoice_id;

  if v_open is null then
    raise exception 'Invoice not found.' using errcode = 'foreign_key_violation';
  end if;

  if not v_open then
    raise exception 'Invoice % is closed and cannot be packed.', v_number
      using errcode = 'check_violation';
  end if;

  select iv.verified_by, p.full_name into v_verifier, v_verifier_name
    from invoice_verifications iv
    left join profiles p on p.id = iv.verified_by
   where iv.invoice_id = new.invoice_id;

  if v_verifier is null then
    raise exception
      'Invoice % has not been verified by an invoice matcher (CONTROL POINT 5).', v_number
      using errcode = 'check_violation',
            hint = 'The matcher must scan the invoice and their badge first.';
  end if;

  -- Two pairs of eyes. One person doing both halves makes the match a
  -- formality, and the value of matching is precisely that it is independent
  -- of packing.
  if v_verifier = new.packed_by then
    raise exception
      'The invoice matcher and the packer must be different people (CONTROL POINT 5). '
      '% verified this invoice.', coalesce(v_verifier_name, 'That person')
      using errcode = 'check_violation';
  end if;

  return new;
end;
$$;

-- A badge must belong to an active holder of the right role. Badges are
-- revocable independently of the account, so a lost badge stops working without
-- disabling the person.
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
    v_allowed := array['invoice_matcher', 'ops_manager', 'admin']::user_role[];
    v_what := 'verify an invoice';
  else
    v_who := new.packed_by;
    v_allowed := array['packer', 'ops_manager', 'admin']::user_role[];
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
            hint = 'Ask Ops to issue a replacement badge.';
  end if;

  if not (v_role = any(v_allowed)) then
    raise exception 'That badge is not permitted to % (CONTROL POINT 5).', v_what
      using errcode = 'insufficient_privilege';
  end if;

  return new;
end;
$$;

create trigger trg_invoice_verifications_badge
  before insert on invoice_verifications
  for each row execute function fn_badge_holder_guard();

create trigger trg_packing_records_badge
  before insert on packing_records
  for each row execute function fn_badge_holder_guard();

-- ===========================================================================
-- OUT-SCAN
--
-- The carton label is the invoice number (PRD §5.6), so an out-scan resolves
-- against invoices rather than stickers. scan_events now carries either kind of
-- reference and the resolver picks by scan type.
-- ===========================================================================

alter table scan_events
  add column if not exists invoice_id uuid references invoices(id) on delete restrict;

-- Previously an accepted scan had to resolve to a sticker. An out-scan resolves
-- to an invoice instead, so the requirement becomes "one of the two".
alter table scan_events drop constraint if exists scan_events_accepted_has_sticker;
alter table scan_events add constraint scan_events_accepted_has_target
  check (not accepted or sticker_id is not null or invoice_id is not null);

create index if not exists scan_events_invoice_idx on scan_events(invoice_id)
  where invoice_id is not null;

-- Same double-count protection stickers get: a carton counts once per scan type,
-- even if two devices scan it in the same millisecond.
create unique index if not exists scan_events_one_accept_per_invoice
  on scan_events(invoice_id, scan_type) where accepted and invoice_id is not null;

-- Extends fn_scan_resolve with the out_scan branch. Everything above the
-- out_scan block is unchanged from 0004.
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
  v_already boolean;
begin
  new.raw_code := upper(btrim(new.raw_code));

  -- ---- Out-scan resolves against invoices, not stickers -------------------
  if new.scan_type = 'out_scan' then
    select * into v_invoice from invoices
     where upper(invoice_number) = new.raw_code;

    if not found then
      new.accepted := false;
      new.reject_reason := 'unknown_code';
      return new;
    end if;

    new.invoice_id := v_invoice.id;

    select * into v_packing from packing_records where invoice_id = v_invoice.id;

    -- An unpacked carton cannot be out-scanned. Allowing it would let goods
    -- reach the pickup area without ever passing CONTROL POINT 5, which is the
    -- only thing recording who packed them.
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

    new.accepted := true;
    new.reject_reason := null;
    return new;
  end if;

  -- ---- Sticker-based scans (box_verify / unit_verify / gate_exit) ---------
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

-- Applying an accepted out-scan: stamp the carton and advance the batch.
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

    -- Move the batch into 'scanning' on the first carton. 'complete' is not set
    -- here — that is CONTROL POINT 6 and belongs to an explicit act by Ops, not
    -- to a side effect of a scan.
    update batches b
       set status = 'scanning'
     where b.id = (select batch_id from packing_records where invoice_id = new.invoice_id)
       and b.status = 'open';

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
-- CONTROL POINT 6 — batch release
-- ===========================================================================

create or replace function fn_batch_transition_ok(old_status batch_status,
                                                  new_status batch_status)
returns boolean language sql immutable as $$
  select (old_status, new_status) in (
    ('open',      'scanning'),
    ('open',      'cancelled'),
    ('scanning',  'complete'),
    ('scanning',  'cancelled'),
    ('complete',  'released'),
    ('complete',  'cancelled')
  ) or old_status = new_status;
$$;

create or replace function fn_batch_release_guard()
returns trigger
language plpgsql
as $$
declare
  v_assigned int;
  v_scanned  int;
begin
  -- Checked before the generic transition table, for the same reason as the gate
  -- guard in 0004: every route into 'released' other than from 'complete' is the
  -- same mistake — a batch going out with a carton unaccounted for — and naming
  -- the control point is far more useful than "Illegal batch transition:
  -- scanning -> released".
  if new.status = 'released' and old.status <> 'complete' then
    raise exception
      'Batch % cannot be released until every carton is out-scanned (CONTROL POINT 6).',
      new.batch_code
      using errcode = 'check_violation',
            hint = 'Complete the out-scan first.';
  end if;

  if not fn_batch_transition_ok(old.status, new.status) then
    raise exception 'Illegal batch transition: % -> %', old.status, new.status
      using errcode = 'check_violation';
  end if;

  if new.status = 'complete' and old.status <> 'complete' then
    select count(*), count(out_scanned_at) into v_assigned, v_scanned
      from packing_records where batch_id = new.id;

    if v_assigned = 0 then
      raise exception 'Batch % has no cartons assigned.', new.batch_code
        using errcode = 'check_violation';
    end if;

    -- Every packed carton in the batch has to be physically scanned before the
    -- batch can be called complete. This is the same shape as the box count at
    -- the gate: a planned number against a scanned number.
    if v_scanned <> v_assigned then
      raise exception
        'Batch %: % of % cartons out-scanned (CONTROL POINT 6).',
        new.batch_code, v_scanned, v_assigned
        using errcode = 'check_violation',
              hint = 'Scan the remaining cartons before releasing the batch.';
    end if;

    if v_assigned <> new.planned_carton_count then
      raise exception
        'Batch %: % cartons assigned but % were planned (CONTROL POINT 6).',
        new.batch_code, v_assigned, new.planned_carton_count
        using errcode = 'check_violation';
    end if;
  end if;

  if new.status = 'released' then
    -- old.status is necessarily 'complete' here; guarded at the top.
    if new.released_by is null then
      raise exception 'Batch release requires a named releasing user (CONTROL POINT 6).'
        using errcode = 'check_violation';
    end if;

    if new.released_at is null then
      new.released_at := now();
    end if;
  end if;

  return new;
end;
$$;

-- A carton cannot be added to, or removed from, a batch that has started
-- scanning. Otherwise "all cartons scanned" could be satisfied by quietly
-- dropping the one that is missing.
create or replace function fn_batch_assignment_guard()
returns trigger
language plpgsql
as $$
declare
  v_status batch_status;
begin
  if new.batch_id is distinct from old.batch_id then
    if old.batch_id is not null then
      select status into v_status from batches where id = old.batch_id;
      if v_status not in ('open', 'cancelled') then
        raise exception
          'Cartons cannot be moved out of batch once out-scanning has started.'
          using errcode = 'check_violation';
      end if;
    end if;

    if new.batch_id is not null then
      select status into v_status from batches where id = new.batch_id;
      if v_status <> 'open' then
        raise exception 'Batch is no longer open for new cartons.'
          using errcode = 'check_violation';
      end if;
    end if;
  end if;

  return new;
end;
$$;

create trigger trg_packing_records_batch_guard
  before update on packing_records
  for each row execute function fn_batch_assignment_guard();

-- ===========================================================================
-- RLS
-- ===========================================================================

-- Matching and packing pages need to update packing_records (batch assignment,
-- out-scan stamp), which the Phase-1 placeholder policies did not allow.
drop policy if exists packing_insert on packing_records;
drop policy if exists inv_verif_insert on invoice_verifications;

create policy inv_verif_insert on invoice_verifications
  for insert to authenticated
  with check (has_role('invoice_matcher', 'ops_manager', 'admin'));

create policy packing_insert on packing_records
  for insert to authenticated
  with check (has_role('packer', 'ops_manager', 'admin'));

-- Ops assigns cartons to batches, and the out-scan trigger stamps them. That
-- trigger runs with the caller's privileges, and out_scan scans are already
-- restricted to Ops by the scan_events insert policy, so this is consistent.
create policy packing_update on packing_records
  for update to authenticated
  using (is_ops()) with check (is_ops());

-- Closing an invoice needs no new policy: `invoices_write` from 0005 is already
-- FOR ALL to Ops.
