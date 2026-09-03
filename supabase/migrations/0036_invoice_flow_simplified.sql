-- 0036_invoice_flow_simplified.sql
--
-- The invoice flow no longer tracks product/SKU/quantity at all — that is
-- Admin's separate ERP's concern. What this app records is: a Packer scans
-- the physical invoice (OCR reads the Order No, which becomes the invoice's
-- identity — 0035), hands it to a different packing lady by scanning her
-- badge, and that lady scans her own badge to confirm she packed it.
--
-- CONTROL POINT 5's two-person guarantee stays. It moves from being anchored
-- on a "verify" record (matcher scans her own badge after confirming
-- product-in-hand via unit-sticker scans) to being anchored on the
-- assignment record, which already names exactly the two people involved:
-- whoever did the assigning (`packing_assignments.assigned_by`) and whoever
-- it was assigned to (`assigned_to`). Assigning now stands in for verifying.

-- ===========================================================================
-- 1. invoices: sku/units are no longer collected, so they can no longer be
--    required. Left in place (harmless, always null going forward) rather
--    than dropped, to avoid a bigger migration for no operational benefit.
-- ===========================================================================

alter table invoices alter column sku drop not null;
alter table invoices alter column units drop not null;
alter table invoices drop constraint if exists invoices_units_check;

-- ===========================================================================
-- 2. The unit-scan-count gates go away entirely — there is nothing left to
--    count units against.
-- ===========================================================================

drop trigger if exists trg_verify_zz_units_complete on invoice_verifications;
drop trigger if exists trg_packing_zz_units_complete on packing_records;
-- All three views dropped up front: v_invoice_matching/v_invoice_packing
-- depend on the functions below, and v_invoice_status is rewritten later in
-- this file anyway (see step 5) — dropping it here too keeps every view drop
-- in one place instead of split across the file.
drop view if exists v_invoice_matching;
drop view if exists v_invoice_packing;
drop view if exists v_invoice_status;
drop function if exists fn_matching_units_complete();
drop function if exists fn_packing_units_complete();
drop function if exists invoice_matched_units(uuid);
drop function if exists invoice_packed_units(uuid);

-- ===========================================================================
-- 3. Assigning a carton no longer requires a prior "verify" — assigning IS
--    the confirming act now. Self-assignment check no longer needs to look
--    anything up: assigned_by and assigned_to are both right there on the
--    row being inserted.
-- ===========================================================================

create or replace function fn_packing_assignment_guard()
returns trigger
language plpgsql
as $$
declare
  v_open      boolean;
  v_number    text;
  v_role      user_role;
  v_active    boolean;
  v_badge     boolean;
  v_name      text;
begin
  select i.is_open, i.invoice_number into v_open, v_number
    from invoices i where i.id = new.invoice_id;

  if v_open is null then
    raise exception 'That invoice does not exist.' using errcode = 'check_violation';
  end if;

  if not v_open then
    raise exception 'Invoice % is closed and cannot be assigned.', v_number
      using errcode = 'check_violation',
            hint = 'Its batch has already been released.';
  end if;

  if exists (select 1 from packing_records pr where pr.invoice_id = new.invoice_id) then
    raise exception 'Invoice % has already been packed.', v_number
      using errcode = 'check_violation',
            hint = 'Reversing a pack is an Ops correction, not a reassignment.';
  end if;

  select p.role, p.is_active, p.badge_active, p.full_name
    into v_role, v_active, v_badge, v_name
    from profiles p where p.id = new.assigned_to;

  if v_role is null then
    raise exception 'That badge does not belong to anyone.' using errcode = 'check_violation';
  end if;

  if not v_active then
    raise exception '%''s account is deactivated.', v_name
      using errcode = 'check_violation';
  end if;

  if not v_badge then
    raise exception '%''s badge has been withdrawn.', v_name
      using errcode = 'check_violation',
            hint = 'Ask an Admin to reissue it.';
  end if;

  if v_role not in ('packer', 'admin') then
    raise exception '% is not a packer.', v_name
      using errcode = 'check_violation',
            hint = 'Packing is done by the packing team, or by an Admin covering the bench.';
  end if;

  -- CONTROL POINT 5, now checked here instead of at packing: the person
  -- doing the assigning and the person being assigned to must be different.
  if new.assigned_by = new.assigned_to then
    raise exception
      'You cannot assign invoice % to yourself (CONTROL POINT 5).', v_number
      using errcode = 'check_violation',
            hint = 'Packing must be a second person. Assign it to someone else.';
  end if;

  return new;
end;
$$;

-- ===========================================================================
-- 4. Packing now requires a current assignment to exist (there is no other
--    way left to establish the "second person"), and refuses if the packer
--    is the same person who assigned it.
-- ===========================================================================

create or replace function fn_packing_guard()
returns trigger
language plpgsql
as $$
declare
  v_assigner      uuid;
  v_assigner_name text;
  v_open          boolean;
  v_number        text;
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

  select a.assigned_by, p.full_name into v_assigner, v_assigner_name
    from packing_assignments a
    left join profiles p on p.id = a.assigned_by
   where a.invoice_id = new.invoice_id and a.is_current;

  if v_assigner is null then
    raise exception
      'Invoice % has not been assigned to anyone yet (CONTROL POINT 5).', v_number
      using errcode = 'check_violation',
            hint = 'Scan the invoice and assign it to a packer first.';
  end if;

  if v_assigner = new.packed_by then
    raise exception
      'The person who assigned this invoice and the packer must be different people '
      '(CONTROL POINT 5). % assigned it.', coalesce(v_assigner_name, 'That person')
      using errcode = 'check_violation';
  end if;

  return new;
end;
$$;

-- ===========================================================================
-- 5. v_invoice_status / v_invoice_packing: recreated (both already dropped in
--    step 2 above) rather than `create or replace`, because that only allows
--    appending columns, not removing the sku/units/verified_* ones this
--    rewrite drops.
-- ===========================================================================

create view v_invoice_status
with (security_invoker = true)
as
select
  i.id                        as invoice_id,
  i.invoice_number,
  i.order_no,
  i.customer_name,
  i.is_open,
  a.assigned_to,
  ap.full_name                as assigned_to_name,
  a.assigned_by,
  ab.full_name                as assigned_by_name,
  a.assigned_at,
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
    when a.id is not null               then 'assigned'
    else 'open'
  end                         as stage
from invoices i
left join packing_assignments a on a.invoice_id = i.id and a.is_current
left join profiles ap on ap.id = a.assigned_to
left join profiles ab on ab.id = a.assigned_by
left join packing_records pr on pr.invoice_id = i.id
left join profiles pp on pp.id = pr.packed_by
left join batches b on b.id = pr.batch_id;

grant select on v_invoice_status to authenticated;

create view v_invoice_packing
with (security_invoker = true)
as
select
  i.id                        as invoice_id,
  i.invoice_number,
  i.is_open,
  a.assigned_to,
  ap.full_name                as assigned_to_name,
  pr.packed_by,
  pp.full_name                as packed_by_name,
  pr.packed_at
from invoices i
left join packing_assignments a on a.invoice_id = i.id and a.is_current
left join profiles ap on ap.id = a.assigned_to
left join packing_records pr on pr.invoice_id = i.id
left join profiles pp on pp.id = pr.packed_by;

grant select on v_invoice_packing to authenticated;

comment on view v_invoice_packing is
  'Per-invoice packing state: who it is assigned to and whether it has been '
  'packed yet. No quantity/product tracking — that is Admin''s ERP''s concern.';

-- ===========================================================================
-- 6. Admin staff screen's "invoices_verified" count: same column name, new
--    source — how many invoices this person scanned and handed off, instead
--    of how many they verified (that step no longer exists).
-- ===========================================================================

create or replace view v_staff_directory
with (security_invoker = true)
as
select
  p.id,
  p.full_name,
  p.employee_code,
  p.role::text                                   as role,
  p.is_active,
  p.is_backup_approver,
  p.has_badge,
  p.badge_active,
  p.has_badge and p.badge_active                 as badge_usable,
  p.created_at,
  coalesce(v.assigned_count, 0)::int             as invoices_verified,
  coalesce(k.packed_count, 0)::int               as cartons_packed,
  greatest(v.last_at, k.last_at)                 as last_attributed_at
from profiles p
left join (
  select assigned_by as id, count(*) as assigned_count, max(assigned_at) as last_at
    from packing_assignments group by assigned_by
) v on v.id = p.id
left join (
  select packed_by as id, count(*) as packed_count, max(packed_at) as last_at
    from packing_records group by packed_by
) k on k.id = p.id;

grant select on v_staff_directory to authenticated;

comment on view v_staff_directory is
  'One row per staff account for the Admin screen. Never exposes badge_code — '
  'only whether a badge exists and whether it is active.';
