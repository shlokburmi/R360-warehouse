-- production_master_data.sql — ONE-TIME manual script that seeds starting
-- master data (vendors, storage locations, purchase orders, invoices) so
-- every role has something real to work with when walking the actual app
-- flow end to end.
--
-- Deliberately does NOT create gate entries, stickers, or scan events —
-- those go through the app's own control-point logic (RLS-guarded status
-- transitions, audit trail, badge attribution). Pre-inserting them directly
-- would produce records the app itself could never have created, and skip
-- the very flow this is meant to demonstrate. Start a real gate entry from
-- the Guard's "Gate Entry" screen using the vendor/PO this script creates.
--
-- NOT applied automatically — lives outside supabase/migrations/ and
-- seed.sql on purpose, same reasoning as production_staff_accounts.sql.
-- Safe to re-run: every insert below is `on conflict do nothing`.
--
-- Run once, pasted into the Supabase Dashboard → SQL Editor for your
-- PRODUCTION project.

-- ---------------------------------------------------------------------------
-- Vendors
-- ---------------------------------------------------------------------------

insert into vendors (code, name, contact_mobile) values
  ('ACME-EL',  'Acme Electronics Pvt Ltd',   '9812300001'),
  ('SUNRISE',  'Sunrise Distributors',       '9812300002'),
  ('KVR-LOG',  'KVR Logistics',              '9812300003'),
  ('BHARAT-G', 'Bharat General Supplies',    '9812300004')
on conflict (code) do nothing;

-- ---------------------------------------------------------------------------
-- Storage locations — Z-AA-RR-LL-BB (DECISIONS.md §6)
-- Zone A fast-moving, B bulk, C high-value cage, Q quarantine.
-- ---------------------------------------------------------------------------

insert into locations (code, description)
select
  format('%s-%s-%s-%s-01', z.zone, lpad(a::text, 2, '0'), lpad(r::text, 2, '0'), lpad(l::text, 2, '0')),
  z.label
from (values ('A', 'Fast moving'), ('B', 'Bulk storage'), ('C', 'High value cage')) as z(zone, label),
     generate_series(1, 3) a,
     generate_series(1, 4) r,
     generate_series(1, 3) l
on conflict (code) do nothing;

insert into locations (code, description) values
  ('Q-01-01-01-01', 'Quarantine — damaged / disputed goods'),
  ('Q-01-01-01-02', 'Quarantine — pending Ops decision')
on conflict (code) do nothing;

-- ---------------------------------------------------------------------------
-- Purchase orders
-- PO-2026-0001 is the happy path. PO-2026-0002 sets up the short-delivery
-- branch (scan fewer units than expected and watch the box go to `held`).
-- ---------------------------------------------------------------------------

with po as (
  insert into purchase_orders (po_number, vendor_id, status, expected_on)
  select 'PO-2026-0001', id, 'open', current_date
    from vendors where code = 'ACME-EL'
  on conflict (po_number) do nothing
  returning id
)
insert into purchase_order_lines (purchase_order_id, line_no, sku, description, expected_units, units_per_box)
select po.id, v.line_no, v.sku, v.descr, v.units, v.per_box
  from po,
       (values
         (1, 'PWB-10K',  'Powerbank 10000mAh',        30, 10),
         (2, 'BTS-BT01', 'Bluetooth Speaker Mini',    20, 10),
         (3, 'HDP-WR2',  'Wireless Headphones Pro',   10, 10)
       ) as v(line_no, sku, descr, units, per_box);

with po as (
  insert into purchase_orders (po_number, vendor_id, status, expected_on)
  select 'PO-2026-0002', id, 'open', current_date
    from vendors where code = 'SUNRISE'
  on conflict (po_number) do nothing
  returning id
)
insert into purchase_order_lines (purchase_order_id, line_no, sku, description, expected_units, units_per_box)
select po.id, v.line_no, v.sku, v.descr, v.units, v.per_box
  from po,
       (values
         (1, 'TRV-MUG', 'Travel Mug Steel 500ml', 20, 10),
         (2, 'BKP-15L', 'Backpack 15L',           10, 10)
       ) as v(line_no, sku, descr, units, per_box);

-- ---------------------------------------------------------------------------
-- Outbound invoices — one per carton, which is what makes the invoice
-- number usable as the carton label at out-scan.
-- ---------------------------------------------------------------------------

insert into invoices (invoice_number, purchase_order_line_id, sku, units, customer_name)
select
  format('INV-2026-%s', lpad(v.n::text, 4, '0')),
  pol.id,
  pol.sku,
  v.units,
  v.customer
from purchase_order_lines pol
join purchase_orders po on po.id = pol.purchase_order_id
join (values
        (1, 'PWB-10K',  2, 'HDFC Bank — Mumbai'),
        (2, 'PWB-10K',  3, 'ICICI Bank — Pune'),
        (3, 'PWB-10K',  2, 'Axis Bank — Delhi'),
        (4, 'BTS-BT01', 4, 'HDFC Bank — Chennai'),
        (5, 'BTS-BT01', 2, 'Kotak Bank — Bengaluru'),
        (6, 'HDP-WR2',  1, 'SBI Cards — Hyderabad')
     ) as v(n, sku, units, customer) on v.sku = pol.sku
where po.po_number = 'PO-2026-0001'
on conflict (invoice_number) do nothing;
