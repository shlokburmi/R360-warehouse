-- 0019_pack_unit_reconciliation.sql
--
-- "The number of QRs generated earlier must match the number after packing."
--
-- Until now nothing connected the product stickers issued at the gate to the
-- cartons that left. CONTROL POINT 3 checks units-per-box against the PO at
-- offloading, and CONTROL POINT 6 checks cartons-assigned against
-- cartons-scanned at out-scan. Between those two there was a gap wide enough to
-- lose a product box in: it could be counted into the warehouse and simply never
-- appear in any carton, and every existing control point would still pass.
--
-- This closes it by making packing a scanning step. The packer scans each
-- product box into the carton, and the carton cannot be recorded as packed until
-- the number scanned equals the number the invoice says it holds.
--
-- The shape is copied from CONTROL POINT 3 on purpose — same ledger, same
-- one-accept-per-sticker index, same derived-never-written counting — because
-- that mechanism has already survived the offline queue, double scans and two
-- devices racing on the same sticker.

-- ===========================================================================
-- ONE INVOICE TAKES MANY PRODUCT SCANS
--
-- `scan_events_one_accept_per_invoice` was written for out_scan, where an
-- invoice *is* a carton and one accepted scan per invoice is exactly right. A
-- pack_unit scan is the opposite: an invoice of 10 product boxes needs 10 of
-- them. Left as it was, the second product box on every carton would collide.
-- ===========================================================================

drop index if exists scan_events_one_accept_per_invoice;

create unique index scan_events_one_accept_per_invoice
  on scan_events (invoice_id, scan_type)
  where accepted and invoice_id is not null and scan_type <> 'pack_unit';

-- A given product box goes into exactly one carton. That is already guaranteed
-- by scan_events_one_accept_per_sticker, which is keyed on
-- (sticker_id, scan_type) — pack_unit inherits it for free. This index is only
-- for reading the count back cheaply.
create index if not exists scan_events_pack_unit_idx
  on scan_events (invoice_id) where accepted and scan_type = 'pack_unit';

-- ===========================================================================
-- HOW MANY PRODUCT BOXES ARE IN THIS CARTON SO FAR
--
-- Derived, never written — the same rule as boxes.scanned_units. If application
-- code could set this, "packed equals promised" would prove nothing.
-- ===========================================================================

create or replace function invoice_packed_units(p_invoice_id uuid)
returns int
language sql
stable
as $$
  select count(*)::int
    from scan_events
   where invoice_id = p_invoice_id
     and scan_type = 'pack_unit'
     and accepted;
$$;

grant execute on function invoice_packed_units(uuid) to authenticated;

create view v_invoice_packing
with (security_invoker = true)
as
select
  i.id                                   as invoice_id,
  i.invoice_number,
  i.sku,
  i.units                                as required_units,
  invoice_packed_units(i.id)             as packed_units,
  greatest(i.units - invoice_packed_units(i.id), 0) as remaining_units,
  invoice_packed_units(i.id) >= i.units  as ready_to_close,
  i.is_open,
  v.verified_by,
  vp.full_name                           as verified_by_name,
  a.assigned_to,
  ap.full_name                           as assigned_to_name,
  pr.packed_by,
  pp.full_name                           as packed_by_name,
  pr.packed_at
from invoices i
left join invoice_verifications v on v.invoice_id = i.id
left join profiles vp            on vp.id = v.verified_by
left join packing_assignments a  on a.invoice_id = i.id and a.is_current
left join profiles ap            on ap.id = a.assigned_to
left join packing_records pr     on pr.invoice_id = i.id
left join profiles pp            on pp.id = pr.packed_by;

grant select on v_invoice_packing to authenticated;

comment on view v_invoice_packing is
  'Per-invoice packing state: who verified it, who it is assigned to, how many '
  'product boxes have been scanned in, and whether it can be closed.';

-- ===========================================================================
-- THE RESOLVER LEARNS pack_unit
--
-- This extends fn_scan_resolve rather than adding a second trigger, and that is
-- not a stylistic choice. Triggers on the same event fire in name order, so a
-- separate `trg_pack_unit_guard` would have run *before* trg_scan_events_resolve
-- and seen a null sticker_id — the resolver is what turns a raw code into a
-- sticker.
--
-- It also keeps the rule from 0004: a refused scan is *recorded*, not raised. A
-- rejected row with a reason is evidence, and "the scanner didn't work" has to
-- stay a claim we can check rather than argue about. Only the control point on
-- packing_records below raises, because that one has nothing to record.
--
-- Everything above the pack_unit branch is unchanged from 0012.
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
  v_sku     text;
  v_verified boolean;
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

  -- ---- Sticker-based scans (box_verify / unit_verify / pack_unit) -----------
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

  -- A box sticker scanned in a unit step (or vice versa) is a real mistake that
  -- would otherwise silently inflate a count. At the packing bench it is the
  -- likeliest one: the big box the product came out of is sitting right there.
  if (new.scan_type = 'box_verify'  and v_sticker.sticker_type <> 'box')
  or (new.scan_type = 'unit_verify' and v_sticker.sticker_type <> 'unit')
  or (new.scan_type = 'pack_unit'   and v_sticker.sticker_type <> 'unit') then
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

  elsif new.scan_type = 'pack_unit' then
    -- The carton has to be named. A product box scanned into "somewhere" is
    -- worse than not scanned, because it counts towards nothing while looking
    -- like progress.
    if new.invoice_id is null then
      new.accepted := false;
      new.reject_reason := 'wrong_invoice';
      return new;
    end if;

    select * into v_invoice from invoices where id = new.invoice_id;

    if not found or not v_invoice.is_open then
      new.accepted := false;
      new.reject_reason := 'wrong_invoice';
      return new;
    end if;

    -- Packing presupposes matching: the product and its invoice only reach a
    -- bench because a matcher put them together (CONTROL POINT 5).
    select exists (
      select 1 from invoice_verifications iv where iv.invoice_id = v_invoice.id
    ) into v_verified;

    if not v_verified then
      new.accepted := false;
      new.reject_reason := 'wrong_invoice';
      return new;
    end if;

    -- The product must actually have arrived and been counted. Without this,
    -- packing would be a second, unaudited route to creating inventory — the
    -- same hole DECISIONS.md §C4 closes for putaway.
    if not exists (
      select 1 from scan_events se
       where se.sticker_id = v_sticker.id
         and se.scan_type = 'unit_verify'
         and se.accepted
    ) then
      new.accepted := false;
      new.reject_reason := 'unit_not_in_stock';
      return new;
    end if;

    -- Right product, right invoice. Packing a powerbank against a headphone
    -- invoice is the error that surfaces weeks later as a customer complaint,
    -- and the sticker knows which PO line it came from.
    select pol.sku into v_sku
      from purchase_order_lines pol
     where pol.id = v_sticker.purchase_order_line_id;

    if v_sku is not null and v_sku <> v_invoice.sku then
      new.accepted := false;
      new.reject_reason := 'wrong_invoice';
      return new;
    end if;

    -- Over-packing is refused rather than reconciled later. An eleventh item in
    -- a ten-item carton would make the carton "complete" and wrong.
    if invoice_packed_units(new.invoice_id) >= v_invoice.units then
      new.accepted := false;
      new.reject_reason := 'invoice_already_full';
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

-- ===========================================================================
-- THE CONTROL POINT
--
-- A carton cannot be recorded as packed until every product box the invoice
-- promises has been scanned into it. This is the check the process asks for:
-- issued QRs in, packed QRs out, and no proceeding on a mismatch.
--
-- This one raises rather than records, because unlike a scan there is nothing to
-- keep — the carton simply does not close.
--
-- There is deliberately no exemption for goods with no product stickers issued.
-- An earlier draft skipped the check in that case so that existing tests could
-- keep packing invoices out of thin air, and the result was a rule that switched
-- itself off depending on unrelated data elsewhere in the database — which is
-- worse than not having it. If a carton is to be packed, its contents get
-- scanned.
-- ===========================================================================

create or replace function fn_packing_units_complete()
returns trigger
language plpgsql
as $$
declare
  v_units  int;
  v_number text;
  v_packed int;
begin
  select i.units, i.invoice_number into v_units, v_number
    from invoices i where i.id = new.invoice_id;

  v_packed := invoice_packed_units(new.invoice_id);

  if v_packed <> v_units then
    raise exception
      'Invoice %: % of % product boxes scanned into the carton.',
      v_number, v_packed, v_units
      using errcode = 'check_violation',
            hint = 'Scan every product box into the carton before closing it.';
  end if;

  return new;
end;
$$;

-- Named to sort after trg_packing_assignment_* and the CONTROL POINT 5 guard, so
-- an operator hears about the missing person before the missing boxes — the
-- specific-before-generic ordering from DECISIONS.md Part D.
create trigger trg_packing_zz_units_complete
  before insert on packing_records
  for each row execute function fn_packing_units_complete();

-- ===========================================================================
-- WHO MAY WRITE A pack_unit SCAN
--
-- `scans_insert` enumerates scan types explicitly, so a new type is refused by
-- default — the insert fails with "your role is not allowed to perform this
-- action" and no amount of reading fn_scan_resolve explains why.
--
-- DECISIONS.md Part D records the mirror of this lesson: adding a role to a step
-- means revisiting the policy for what that step writes. Adding a *step* means
-- revisiting who may write it.
--
-- Packers and Ops covering the bench. Deliberately not the offloading team: they
-- scan goods *in* (unit_verify), and letting the same role do both halves would
-- put receiving and dispatch in one pair of hands.
-- ===========================================================================

drop policy if exists scans_insert on scan_events;

create policy scans_insert on scan_events
  for insert to authenticated
  with check (
    scanned_by = auth.uid()
    and (
      (scan_type = 'box_verify'
        and has_role('security_guard', 'ops_manager', 'admin'))
   or (scan_type = 'unit_verify'
        and has_role('offloading', 'ops_manager', 'admin'))
   or (scan_type = 'pack_unit'
        and has_role('packer', 'ops_manager', 'admin'))
   or (scan_type = 'out_scan'
        and has_role('ops_manager', 'admin'))
   or (scan_type = 'gate_exit'
        and has_role('security_guard', 'ops_manager', 'admin'))
    )
  );

-- ===========================================================================
-- RECONCILIATION ACROSS THE WHOLE ENTRY
--
-- The per-invoice check above is the hard stop. This view answers the wider
-- question an auditor asks: of the product stickers issued for this truck, how
-- many were received and how many left in a carton — and where are the rest?
-- ===========================================================================

create view v_sticker_reconciliation
with (security_invoker = true)
as
select
  ge.id                             as gate_entry_id,
  ge.entry_code,
  ve.name                           as vendor_name,
  po.po_number,
  count(distinct s.id)::int         as unit_stickers_issued,
  count(distinct uv.sticker_id)::int as received_at_offloading,
  count(distinct pu.sticker_id)::int as packed_into_cartons,
  (count(distinct s.id) - count(distinct uv.sticker_id))::int as never_received,
  (count(distinct uv.sticker_id) - count(distinct pu.sticker_id))::int as received_not_packed
from gate_entries ge
join vendors ve on ve.id = ge.vendor_id
left join purchase_orders po on po.id = ge.purchase_order_id
left join stickers s
  on s.gate_entry_id = ge.id and s.sticker_type = 'unit' and s.status <> 'void'
left join scan_events uv
  on uv.sticker_id = s.id and uv.scan_type = 'unit_verify' and uv.accepted
left join scan_events pu
  on pu.sticker_id = s.id and pu.scan_type = 'pack_unit' and pu.accepted
group by ge.id, ge.entry_code, ve.name, po.po_number;

grant select on v_sticker_reconciliation to authenticated;

comment on view v_sticker_reconciliation is
  'Product stickers issued vs received vs packed, per gate entry. '
  '`received_not_packed` is stock that came in and has not left in a carton — '
  'the gap CONTROL POINTS 3 and 6 both miss.';
