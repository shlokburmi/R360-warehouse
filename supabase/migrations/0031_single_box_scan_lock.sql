-- 0031_single_box_scan_lock.sql
--
-- Only one box may be mid-scan (status 'scanning') at a time per truck.
-- Without this, a unit sticker for Box 3 scanned while Box 1 is still open
-- would resolve and count correctly (a sticker always knows its own box),
-- which is data-safe but not what the floor process wants: Box 1 must be
-- damage-checked and closed before Box 3's units are scanned at all.
--
-- Full function body carried forward from 0024_matching_unit_scan.sql with
-- one addition in the unit_verify branch — everything else is unchanged.

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
  v_other_open boolean;
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

    -- Only one box may be mid-scan at a time. A box already 'scanning' that
    -- is not this one means the floor has a different box open right now.
    select exists (
      select 1 from boxes
       where gate_entry_id = v_box.gate_entry_id
         and status = 'scanning'
         and id <> v_box.id
    ) into v_other_open;

    if v_other_open then
      new.accepted := false;
      new.reject_reason := 'other_box_open';
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
