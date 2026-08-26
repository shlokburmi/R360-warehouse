-- 0021_carton_sticker_resolution.sql
--
-- Teaches fn_scan_resolve to resolve out_scan and gate_exit against a carton
-- sticker first, falling back to the raw invoice number if no sticker matches
-- — the same "QR first, human-readable text as fallback" shape every other
-- sticker in this system already has (DECISIONS.md §CE2), now extended to the
-- carton label CC1 originally left as invoice-number-only.
--
-- A code that matches a *box* or *unit* sticker is refused here with
-- `wrong_sticker_type` rather than falling through to `unknown_code` — the
-- same distinction fn_scan_resolve already makes lower down for box_verify
-- vs unit_verify, extended to the family boundary 0020 adds.
--
-- Everything below the out_scan/gate_exit block is unchanged from 0019.

-- ===========================================================================
-- FAMILY SHAPE — moved from 0020, which cannot reference the 'carton' label
-- it just added in the same transaction.
-- ===========================================================================

-- Box and unit stickers keep belonging to a gate entry and a sheet, never an
-- invoice; a carton sticker belongs to an invoice alone. Enumerated rather
-- than left implicit, so a future third shape has to update this constraint
-- instead of silently fitting through a gap in it.
alter table stickers add constraint stickers_family_shape check (
  (sticker_type in ('box', 'unit')
    and gate_entry_id is not null and sheet_id is not null and invoice_id is null)
  or
  (sticker_type = 'carton'
    and invoice_id is not null and gate_entry_id is null and sheet_id is null)
);

-- One live carton sticker per invoice. Re-issuing voids the old one first
-- (mirrors admin_issue_badge's "reissue means replace" in 0013), so this only
-- ever has to refuse a *second live* sticker, never a second sticker.
create unique index stickers_one_live_carton_per_invoice
  on stickers (invoice_id) where sticker_type = 'carton' and status <> 'void';

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
      -- Nothing leaves that Admin has not released. This is what stops a
      -- carton being loaded straight from the packing bench onto a truck.
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
    -- bench because Admin put them together (CONTROL POINT 5).
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
