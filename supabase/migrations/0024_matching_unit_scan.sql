-- 0024_matching_unit_scan.sql
--
-- Adds a genuine product-in-hand check at invoice matching, at the user's
-- explicit request. PRD §5.4/§7 describes the matcher physically placing the
-- product on the invoice before scanning her badge, but nothing before this
-- migration verified that in software — only the invoice number and the
-- matcher's badge were scanned. A later, equivalent check already exists at
-- packing (0019, CG3: the packer scans each product box into the carton).
-- This is deliberately additive, not a replacement: both checks now run, at
-- their own stages, against the same unit stickers.
--
-- Built by mirroring 0019/0021 exactly — same ledger shape (derived, never
-- written), same one-accept-per-sticker reuse, same ordering discipline for
-- the hard-stop trigger. That mechanism has already survived the offline
-- queue, double scans and two devices racing on one sticker; there is no
-- reason for the matching-stage version to be structured any differently.

-- ===========================================================================
-- ONE INVOICE TAKES MANY match_unit SCANS TOO
-- (same reasoning as 0019's pack_unit change to this index)
-- ===========================================================================

drop index if exists scan_events_one_accept_per_invoice;

create unique index scan_events_one_accept_per_invoice
  on scan_events (invoice_id, scan_type)
  where accepted and invoice_id is not null
    and scan_type not in ('pack_unit', 'match_unit');

-- A unit sticker can be accepted once for match_unit and, independently,
-- once for pack_unit later — scan_events_one_accept_per_sticker is keyed
-- (sticker_id, scan_type), so match_unit inherits that guarantee for free,
-- the same way pack_unit did.
create index if not exists scan_events_match_unit_idx
  on scan_events (invoice_id) where accepted and scan_type = 'match_unit';

-- ===========================================================================
-- HOW MANY UNITS HAVE BEEN CONFIRMED AT MATCHING SO FAR
-- ===========================================================================

create or replace function invoice_matched_units(p_invoice_id uuid)
returns int
language sql
stable
as $$
  select count(*)::int
    from scan_events
   where invoice_id = p_invoice_id
     and scan_type = 'match_unit'
     and accepted;
$$;

grant execute on function invoice_matched_units(uuid) to authenticated;

create view v_invoice_matching
with (security_invoker = true)
as
select
  i.id                                      as invoice_id,
  i.invoice_number,
  i.sku,
  i.units                                   as required_units,
  invoice_matched_units(i.id)               as matched_units,
  greatest(i.units - invoice_matched_units(i.id), 0) as remaining_units,
  invoice_matched_units(i.id) >= i.units    as ready_to_verify,
  i.is_open,
  v.verified_by,
  vp.full_name                              as verified_by_name
from invoices i
left join invoice_verifications v on v.invoice_id = i.id
left join profiles vp             on vp.id = v.verified_by;

grant select on v_invoice_matching to authenticated;

comment on view v_invoice_matching is
  'Per-invoice matching state: how many unit stickers have been scanned to '
  'confirm product-in-hand, and whether the matcher may now scan her badge.';

-- ===========================================================================
-- THE RESOLVER LEARNS match_unit
--
-- Everything above and below the match_unit branch is unchanged from 0021.
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
    select * into v_sticker from stickers where code = new.raw_code;

    if found then
      if v_sticker.sticker_type <> 'carton' then
        new.accepted := false;
        new.reject_reason := 'wrong_sticker_type';
        return new;
      end if;

      if v_sticker.status = 'void' then
        new.accepted := false;
        new.reject_reason := 'sticker_void';
        return new;
      end if;

      new.sticker_id := v_sticker.id;
      select * into v_invoice from invoices where id = v_sticker.invoice_id;
    else
      new.sticker_id := null;
      select * into v_invoice from invoices
       where upper(invoice_number) = new.raw_code;
    end if;

    if v_invoice.id is null then
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

  -- ---- Sticker-based scans (box_verify / unit_verify / match_unit / pack_unit) ----
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
  or (new.scan_type = 'unit_verify' and v_sticker.sticker_type <> 'unit')
  or (new.scan_type = 'pack_unit'   and v_sticker.sticker_type <> 'unit')
  or (new.scan_type = 'match_unit'  and v_sticker.sticker_type <> 'unit') then
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

  elsif new.scan_type = 'match_unit' then
    -- The carton has to be named, same as pack_unit below — a product box
    -- scanned into "somewhere" counts towards nothing.
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

    -- This scan happens *before* the matcher's badge is scanned — it is what
    -- CONTROL POINT 5's first half is now gated on (fn_matching_units_complete
    -- below). An invoice already verified has moved past matching.
    select exists (
      select 1 from invoice_verifications iv where iv.invoice_id = v_invoice.id
    ) into v_verified;

    if v_verified then
      new.accepted := false;
      new.reject_reason := 'wrong_invoice';
      return new;
    end if;

    -- The product must actually have arrived and been counted at offloading —
    -- the same requirement pack_unit makes later, checked here first so a
    -- matcher cannot confirm a product that was never received.
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

    select pol.sku into v_sku
      from purchase_order_lines pol
     where pol.id = v_sticker.purchase_order_line_id;

    if v_sku is not null and v_sku <> v_invoice.sku then
      new.accepted := false;
      new.reject_reason := 'wrong_invoice';
      return new;
    end if;

    if invoice_matched_units(new.invoice_id) >= v_invoice.units then
      new.accepted := false;
      new.reject_reason := 'invoice_already_matched';
      return new;
    end if;

  elsif new.scan_type = 'pack_unit' then
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

    select exists (
      select 1 from invoice_verifications iv where iv.invoice_id = v_invoice.id
    ) into v_verified;

    if not v_verified then
      new.accepted := false;
      new.reject_reason := 'wrong_invoice';
      return new;
    end if;

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

    select pol.sku into v_sku
      from purchase_order_lines pol
     where pol.id = v_sticker.purchase_order_line_id;

    if v_sku is not null and v_sku <> v_invoice.sku then
      new.accepted := false;
      new.reject_reason := 'wrong_invoice';
      return new;
    end if;

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
-- THE CONTROL POINT — badge scan refused until every unit is confirmed
--
-- Mirrors fn_packing_units_complete (0019) exactly. Named to sort after
-- trg_invoice_verifications_badge (0009), so a badge/role problem is reported
-- before a unit-count problem — the specific-before-generic ordering from
-- DECISIONS.md Part D.
-- ===========================================================================

create or replace function fn_matching_units_complete()
returns trigger
language plpgsql
as $$
declare
  v_units   int;
  v_number  text;
  v_matched int;
begin
  select i.units, i.invoice_number into v_units, v_number
    from invoices i where i.id = new.invoice_id;

  v_matched := invoice_matched_units(new.invoice_id);

  if v_matched <> v_units then
    raise exception
      'Invoice %: % of % units scanned at matching.',
      v_number, v_matched, v_units
      using errcode = 'check_violation',
            hint = 'Scan every unit sticker for this invoice before confirming the match.';
  end if;

  return new;
end;
$$;

create trigger trg_verify_zz_units_complete
  before insert on invoice_verifications
  for each row execute function fn_matching_units_complete();
