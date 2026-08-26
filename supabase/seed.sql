-- seed.sql — development data. Applied automatically by `supabase db reset`.
--
-- Creates one demo user per role so the whole Phase 1 flow can be walked
-- end to end without wiring up real accounts. Password for all: Warehouse@123
--
-- These accounts exist ONLY in local/dev. Never run this against production —
-- see the guard at the bottom of the file.

do $$
begin
  if current_setting('server_version_num')::int < 140000 then
    raise exception 'Postgres 14+ required';
  end if;
end;
$$;

-- pgcrypto lives in `extensions` on Supabase, so crypt() and gen_salt() below
-- are not reachable from `public` alone. This worked by accident for a long time
-- because the session `supabase db reset` uses happens to have a wider path; the
-- same assumption broke migration 0003 the first time it met a hosted project.
-- A missing schema in a search_path is skipped, so this is safe anywhere.
set search_path = public, extensions;

-- ---------------------------------------------------------------------------
-- Local dev password for the API's non-superuser role.
--
-- `supabase db reset` recreates the database, so a password set by hand does
-- not survive it. Putting it here means a reset always leaves a stack the
-- backend can actually connect to. Production sets this out of band and never
-- runs this file.
-- ---------------------------------------------------------------------------

alter role api_user with password 'api_password';

-- ---------------------------------------------------------------------------
-- Demo users
-- Inserted directly into auth.users; the trg_auth_user_created trigger creates
-- the matching profiles row from raw_user_meta_data.
-- ---------------------------------------------------------------------------

-- auth.users has no unique constraint on `email` by itself (GoTrue scopes
-- uniqueness by instance), so ON CONFLICT (email) is not valid here. Check
-- first instead, which also makes re-running the seed a no-op.
create or replace function seed_user(
  p_email text, p_name text, p_role user_role, p_code text, p_mobile text
) returns uuid
language plpgsql as $$
declare
  v_id uuid;
begin
  select id into v_id from auth.users where email = p_email;
  if v_id is not null then
    return v_id;
  end if;

  v_id := gen_random_uuid();

  insert into auth.users (
    id, instance_id, aud, role, email, encrypted_password,
    email_confirmed_at, created_at, updated_at, last_sign_in_at,
    raw_app_meta_data, raw_user_meta_data,
    confirmation_token, recovery_token, email_change_token_new, email_change
  ) values (
    v_id, '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
    p_email, crypt('Warehouse@123', gen_salt('bf')),
    now(), now(), now(), null,
    jsonb_build_object('provider', 'email', 'providers', array['email']),
    jsonb_build_object(
      'full_name', p_name, 'role', p_role::text,
      'employee_code', p_code, 'mobile', p_mobile
    ),
    -- GoTrue declares these NOT NULL with no default on some versions, and
    -- treats '' as "no token pending".
    '', '', '', ''
  );

  return v_id;
end;
$$;

-- Four roles: security_guard, offloading, packer, admin. Admin absorbs the
-- old ops_manager and invoice_matcher duties (approvals, sticker issuance,
-- invoice matching); offloading absorbs the old inbound and warehouse_staff
-- duties (reconciliation, putaway).
select seed_user('guard@r360.local',    'Sanjeev Kumar',  'security_guard', 'EMP-G01', '9876500001');
select seed_user('boopathi@r360.local', 'Boopathi',       'admin',          'EMP-O01', '9876500002');
select seed_user('opsbackup@r360.local','Ramesh Iyer',    'admin',          'EMP-O02', '9876500003');
select seed_user('offload@r360.local',  'Arun Prasad',    'offloading',     'EMP-F01', '9876500004');
select seed_user('inbound@r360.local',  'Divya Nair',     'offloading',     'EMP-I01', '9876500005');
select seed_user('store@r360.local',    'Mahesh Rao',     'offloading',     'EMP-W01', '9876500006');
select seed_user('match1@r360.local',   'Lakshmi Devi',   'admin',          'EMP-M01', '9876500007');
select seed_user('match2@r360.local',   'Priya Menon',    'admin',          'EMP-M02', '9876500008');
select seed_user('pack1@r360.local',    'Kavitha S',      'packer',         'EMP-P01', '9876500009');
select seed_user('pack2@r360.local',    'Anitha R',       'packer',         'EMP-P02', '9876500010');
select seed_user('admin@r360.local',    'Warehouse Admin','admin',          'EMP-A01', '9876500011');

-- Backup approver for the T+15m escalation path (DECISIONS.md §4).
update profiles set is_backup_approver = true
 where employee_code = 'EMP-O02';

-- Attribution badges for the roles that scan one (Phase 3).
--
-- Admins get one too, because they cover both the matching and packing
-- stations when someone is on a break. That is also the only way the
-- two-person rule in CONTROL POINT 5 can actually be exercised: an admin's
-- badge cannot pack the invoice they just matched, so the same-person case
-- only arises for someone permitted to do both.
update profiles set badge_code = generate_badge_code()
 where role in ('packer', 'admin') and badge_code is null;

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
-- PO-2026-0001 is the happy path. PO-2026-0002 is set up so a demo can walk the
-- short-delivery branch (scan 8 of 10 and watch the box go to `held`).
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
-- Outbound invoices (Phase 3).
--
-- One invoice per carton, which is what makes the invoice number usable as the
-- carton label at out-scan. Quantities are deliberately smaller than a full box
-- so a single received box can serve several customers.
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

-- ---------------------------------------------------------------------------
-- A couple of known visitors, so the "returning visitor, no photo needed"
-- branch is reachable on the very first demo.
-- ---------------------------------------------------------------------------

insert into visitors (mobile, full_name, id_photo_path, id_photo_captured_at)
values
  ('9900112233', 'Sanjeev Kumar (Driver)', 'seed/driver-9900112233.jpg', now() - interval '10 days'),
  ('9900112244', 'Ravi Shankar (Laborer)', null, null)
on conflict (mobile) do nothing;

drop function seed_user(text, text, user_role, text, text);

-- ---------------------------------------------------------------------------
-- Production guard: refuse to leave demo credentials in a non-local database.
-- ---------------------------------------------------------------------------

do $$
begin
  if coalesce(current_setting('app.environment', true), 'local') = 'production' then
    raise exception 'seed.sql must never be applied to production.';
  end if;
end;
$$;
