-- 0007_putaway.sql
-- PHASE 2 — Segregation and putaway (PRD Step 6).
--
-- The rule this phase has to hold is narrower than the earlier control points
-- but the same in spirit: units that were counted into the warehouse must be
-- placed somewhere, exactly once, and the record of where must be true.
--
-- Three ways that goes wrong without enforcement:
--   * the same box gets put away twice, inventing stock
--   * more units are placed than ever arrived
--   * damaged units are shelved as good stock, and sold
--
-- All three are refused below.

-- ---------------------------------------------------------------------------
-- Locations gain a capacity notion and a quarantine flag.
-- ---------------------------------------------------------------------------

alter table locations
  add column if not exists is_quarantine boolean
    generated always as (substring(code from 1 for 1) = 'Q') stored;

comment on column locations.is_quarantine is
  'Derived from the zone letter, so a quarantine rack cannot be mislabelled '
  'into being a stock rack.';

-- ---------------------------------------------------------------------------
-- Putaway needs to know how much of a box is still unplaced.
-- ---------------------------------------------------------------------------

-- Units that are eligible to be shelved, split by disposition. Derived from the
-- scan ledger, like every other count in this system.
create or replace view v_box_putaway_status as
select
  b.id                                   as box_id,
  b.gate_entry_id,
  b.box_number,
  b.purchase_order_line_id,
  b.status::text                         as box_status,
  b.scanned_units,
  b.quarantined_units,
  (b.scanned_units - b.quarantined_units) as stock_units,
  coalesce(p.stock_placed, 0)            as stock_placed,
  coalesce(p.quarantine_placed, 0)       as quarantine_placed,
  (b.scanned_units - b.quarantined_units) - coalesce(p.stock_placed, 0)
                                         as stock_remaining,
  b.quarantined_units - coalesce(p.quarantine_placed, 0)
                                         as quarantine_remaining
from boxes b
left join (
  select box_id,
         sum(units) filter (where disposition = 'stock')::int      as stock_placed,
         sum(units) filter (where disposition = 'quarantine')::int as quarantine_placed
    from putaways
   group by box_id
) p on p.box_id = b.id;

-- What the warehouse staff page lists: boxes cleared by CP4 with units left to
-- place. A box whose gate entry has not been reconciled does not appear at all.
create or replace view v_putaway_queue as
select
  s.box_id,
  s.gate_entry_id,
  s.box_number,
  s.box_status,
  s.stock_remaining,
  s.quarantine_remaining,
  ge.entry_code,
  ge.vehicle_number,
  v.name  as vendor_name,
  po.po_number,
  pol.sku,
  pol.description
from v_box_putaway_status s
join gate_entries ge on ge.id = s.gate_entry_id
join vendors v on v.id = ge.vendor_id
left join purchase_orders po on po.id = ge.purchase_order_id
left join purchase_order_lines pol on pol.id = s.purchase_order_line_id
where s.box_status in ('complete', 'short_accepted')
  and ge.status in ('reconciled', 'departed')
  and (s.stock_remaining > 0 or s.quarantine_remaining > 0);

grant select on v_box_putaway_status, v_putaway_queue to authenticated;

-- ---------------------------------------------------------------------------
-- CONTROL POINT: putaway
-- ---------------------------------------------------------------------------

create or replace function fn_putaway_guard()
returns trigger
language plpgsql
as $$
declare
  v_box       boxes%rowtype;
  v_entry     gate_entries%rowtype;
  v_status    record;
  v_location  locations%rowtype;
  v_remaining int;
begin
  select * into v_box from boxes where id = new.box_id;
  if not found then
    raise exception 'Box not found.' using errcode = 'foreign_key_violation';
  end if;

  select * into v_entry from gate_entries where id = v_box.gate_entry_id;

  -- Nothing is shelved before the inbound team has agreed the count. This is
  -- what makes CONTROL POINT 4 more than advisory: failing it does not just
  -- show a warning, it stops the goods physically moving to a rack.
  if v_entry.status not in ('reconciled', 'departed') then
    raise exception
      'Inbound verification is not complete for %; putaway is blocked (CONTROL POINT 4).',
      v_entry.entry_code
      using errcode = 'check_violation',
            hint = 'The inbound team must reconcile the counts first.';
  end if;

  if v_box.status not in ('complete', 'short_accepted') then
    raise exception 'Box % is % and cannot be put away.', v_box.box_number, v_box.status
      using errcode = 'check_violation',
            hint = 'Only a closed box can be shelved.';
  end if;

  select * into v_location from locations where id = new.location_id;
  if not found or not v_location.is_active then
    raise exception 'That storage location does not exist or is inactive.'
      using errcode = 'foreign_key_violation';
  end if;

  -- Damaged units go to a quarantine rack and good units do not. Getting this
  -- wrong is how damaged stock reaches a customer, so it is a hard stop rather
  -- than a warning on a screen.
  if new.disposition = 'quarantine' and not v_location.is_quarantine then
    raise exception
      'Damaged units must be placed in a quarantine location (zone Q), not %.',
      v_location.code
      using errcode = 'check_violation';
  end if;

  if new.disposition = 'stock' and v_location.is_quarantine then
    raise exception
      'Good stock cannot be placed in quarantine location %.', v_location.code
      using errcode = 'check_violation';
  end if;

  -- Never place more than arrived. Without this, putaway becomes a second,
  -- unaudited way to create inventory.
  select * into v_status from v_box_putaway_status where box_id = new.box_id;

  v_remaining := case
    when new.disposition = 'quarantine' then v_status.quarantine_remaining
    else v_status.stock_remaining
  end;

  if new.units > v_remaining then
    raise exception
      'Box %: only % % unit(s) left to place, tried to place %.',
      v_box.box_number, v_remaining, new.disposition, new.units
      using errcode = 'check_violation',
            hint = 'Counts come from the scan ledger and cannot be exceeded.';
  end if;

  return new;
end;
$$;

create trigger trg_putaways_guard
  before insert on putaways
  for each row execute function fn_putaway_guard();

-- Once every unit of a box has a home, the carton itself is empty and goes to
-- the outside rack (PRD Step 4).
create or replace function fn_putaway_close_box()
returns trigger
language plpgsql
as $$
declare
  v_status record;
begin
  select * into v_status from v_box_putaway_status where box_id = new.box_id;

  if v_status.stock_remaining <= 0 and v_status.quarantine_remaining <= 0 then
    perform set_config('app.exception_resolution', 'on', true);
    update boxes set status = 'emptied' where id = new.box_id
      and status in ('complete', 'short_accepted');
    perform set_config('app.exception_resolution', 'off', true);
  end if;

  return new;
end;
$$;

create trigger trg_putaways_close_box
  after insert on putaways
  for each row execute function fn_putaway_close_box();

-- 'emptied' is a legal destination for a closed box, reached only by the
-- trigger above. Extend the box transition guard to allow it.
create or replace function fn_box_transition_guard()
returns trigger
language plpgsql
as $$
begin
  if new.status = 'complete' and old.status <> 'complete' then
    if new.scanned_units <> new.expected_units then
      raise exception
        'Box %: % of % units scanned. Goods are held (CONTROL POINT 3).',
        new.box_number, new.scanned_units, new.expected_units
        using errcode = 'check_violation';
    end if;

    if new.damage_level is null then
      raise exception 'Box %: damage check must be recorded before the box can close.',
        new.box_number
        using errcode = 'check_violation';
    end if;

    if new.completed_at is null then
      new.completed_at := now();
    end if;
  end if;

  -- short_accepted, rejected and emptied are all reachable only through a
  -- specific server-side path — a resolved exception, or a completed putaway.
  -- A direct API update lands here and is refused.
  if new.status in ('short_accepted', 'rejected', 'emptied')
     and old.status <> new.status
     and coalesce(current_setting('app.exception_resolution', true), 'off') <> 'on' then
    raise exception
      'Box % cannot be set to % directly.',
      new.box_number, new.status
      using errcode = 'insufficient_privilege',
            hint = 'This happens automatically when the goods are resolved or shelved.';
  end if;

  return new;
end;
$$;

-- ---------------------------------------------------------------------------
-- Where is this SKU? The question Phase 3 picking will ask.
-- ---------------------------------------------------------------------------

create or replace view v_stock_by_location as
select
  l.id            as location_id,
  l.code          as location_code,
  l.zone,
  l.is_quarantine,
  pol.sku,
  pol.description,
  sum(p.units)::int as units,
  max(p.moved_at)   as last_movement
from putaways p
join locations l on l.id = p.location_id
join purchase_order_lines pol on pol.id = p.purchase_order_line_id
group by l.id, l.code, l.zone, l.is_quarantine, pol.sku, pol.description;

grant select on v_stock_by_location to authenticated;

-- ---------------------------------------------------------------------------
-- RLS: putaway is append-only and belongs to warehouse staff.
-- ---------------------------------------------------------------------------

-- Replaces the Phase-1 placeholder policy to also allow the offloading team,
-- who in practice move goods to racks on a busy shift.
drop policy if exists putaways_insert on putaways;

create policy putaways_insert on putaways
  for insert to authenticated
  with check (
    has_role('warehouse_staff', 'offloading', 'ops_manager', 'admin')
    and moved_by = auth.uid()
  );

-- Warehouse staff must be able to UPDATE boxes, not just insert putaways.
--
-- fn_putaway_close_box marks the carton 'emptied' once every unit is shelved,
-- and that trigger runs with the caller's privileges. The Phase-1 policy did not
-- include warehouse_staff, so the update matched zero rows and the box silently
-- stayed 'complete' — no error anywhere, just a carton that looks unshelved
-- forever. The narrow status change is still guarded by the transition trigger.
drop policy if exists boxes_update on boxes;

create policy boxes_update on boxes
  for update to authenticated
  using (has_role('security_guard', 'offloading', 'warehouse_staff', 'ops_manager', 'admin'))
  with check (has_role('security_guard', 'offloading', 'warehouse_staff', 'ops_manager', 'admin'));
