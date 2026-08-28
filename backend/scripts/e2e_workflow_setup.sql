-- Inbound setup for scripts/e2e_workflow.py. Not the thing under test.
--
-- The seed ships purchase orders and invoices but no received goods, and the
-- Phase 5 steps need product boxes that genuinely arrived — the whole point of
-- the reconciliation is that a box cannot be packed unless it was counted in.
-- Driving all of Phase 1 over HTTP first would triple the length of that script
-- to re-prove what test_control_points.py already proves, so the receiving
-- history is built here and the Phase 5 flow is what gets exercised through the
-- API.
do $$
declare
  v_line   uuid;
  v_sku    text;
  v_vendor uuid;
  v_po     uuid;
  v_guard  uuid;
  v_ops    uuid;
  v_off    uuid;
  v_entry  uuid;
  v_sheet  uuid;
  v_stick  uuid;
  v_box    uuid;
  v_code   text;
  v_units  int := 2;
  i        int;
begin
  select pol.id, pol.sku, po.vendor_id, po.id
    into v_line, v_sku, v_vendor, v_po
    from purchase_order_lines pol
    join purchase_orders po on po.id = pol.purchase_order_id
   where po.po_number = 'PO-2026-0001'
   order by pol.line_no limit 1;

  select id into v_guard from profiles where employee_code = 'EMP-G01';
  select id into v_ops   from profiles where employee_code = 'EMP-O01';
  select id into v_off   from profiles where employee_code = 'EMP-F01';

  -- Must be exactly 2 letters, 2 digits, 2 letters, 4 digits
  -- (gate_entries_vehicle_number_check, 0027) — a hex suffix can contain
  -- a-f, which a strict numeric suffix can't.
  insert into gate_entries
    (status, vehicle_number, vendor_id, purchase_order_id, requested_by, requested_at)
  values ('pending_approval', 'KA01WF' || lpad(floor(random() * 10000)::int::text, 4, '0'),
          v_vendor, v_po, v_guard, now())
  returning id into v_entry;

  update gate_entries set status = 'approved', decided_by = v_ops, decided_at = now()
   where id = v_entry;
  update gate_entries set status = 'inside' where id = v_entry;
  update gate_entries
     set declared_box_count = 1, declared_by = v_guard, declared_at = now(),
         status = 'counting'
   where id = v_entry;

  insert into sticker_sheets (gate_entry_id, sticker_type, quantity, generated_by)
  values (v_entry, 'box', 1, v_ops) returning id into v_sheet;

  v_code := 'BOX-' || upper(substr(md5(random()::text), 1, 8));
  insert into stickers
    (code, sticker_type, sheet_id, gate_entry_id, purchase_order_line_id,
     expected_units, sequence_no, status)
  values (v_code, 'box', v_sheet, v_entry, v_line, v_units, 1, 'applied')
  returning id into v_stick;

  insert into boxes
    (gate_entry_id, sticker_id, box_number, purchase_order_line_id, expected_units)
  values (v_entry, v_stick, 1, v_line, v_units) returning id into v_box;

  update stickers set box_id = v_box where id = v_stick;
  update gate_entries set issued_box_sticker_count = 1 where id = v_entry;

  insert into scan_events
    (client_event_id, scan_type, raw_code, accepted, scanned_by, scanned_at)
  values (gen_random_uuid(), 'box_verify', v_code, false, v_guard, now());

  update gate_entries set status = 'box_verified' where id = v_entry;
  update gate_entries set status = 'offloading'   where id = v_entry;

  insert into sticker_sheets (gate_entry_id, sticker_type, quantity, generated_by)
  values (v_entry, 'unit', v_units, v_ops) returning id into v_sheet;

  for i in 1..v_units loop
    v_code := 'UNT-' || upper(substr(md5(random()::text || i::text), 1, 8));
    insert into stickers
      (code, sticker_type, sheet_id, gate_entry_id, box_id,
       purchase_order_line_id, sequence_no, status)
    values (v_code, 'unit', v_sheet, v_entry, v_box, v_line, i, 'applied');

    insert into scan_events
      (client_event_id, scan_type, raw_code, accepted, scanned_by, scanned_at)
    values (gen_random_uuid(), 'unit_verify', v_code, false, v_off, now());
  end loop;

  insert into invoices (invoice_number, purchase_order_line_id, sku, units, customer_name)
  values ('INV-WF-' || upper(substr(md5(random()::text), 1, 6)), v_line, v_sku,
          v_units, 'Workflow Test Customer');

  -- A collecting driver already on the visitor register, with a photo on file.
  -- Gate entry refuses a first-time visitor without one (DECISIONS.md §2), and
  -- uploading an image is not what the Phase 5 walkthrough is testing.
  insert into visitors (mobile, full_name, id_photo_path, id_photo_captured_at)
  values ('9812345678', 'Exit Test Driver', '9812345678/e2e.jpg', now())
  on conflict (mobile) do update
     set id_photo_path = excluded.id_photo_path,
         id_photo_captured_at = excluded.id_photo_captured_at,
         id_photo_purged_at = null,
         id_photo_purge_reason = null;
end;
$$;

select i.invoice_number
  from invoices i
 where i.invoice_number like 'INV-WF-%' and i.is_open
 order by i.created_at desc limit 1;
