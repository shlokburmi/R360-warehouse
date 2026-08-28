-- production_staff_accounts.sql — ONE-TIME manual script that creates the
-- real staff accounts for this app's production deployment (Security Guard,
-- Ops Manager, Offloading, Warehouse Staff, Invoice Matcher, Packer, Admin).
--
-- NOT applied automatically by `supabase db push` or `supabase db reset` —
-- this file lives outside supabase/migrations/ and outside seed.sql
-- deliberately, because seed.sql refuses to run against production on
-- purpose (see the guard at the bottom of that file). This script is the
-- production equivalent, run by hand once.
--
-- Run this ONCE, by pasting it into the Supabase Dashboard → SQL Editor for
-- your PRODUCTION project. Safe to re-run — each seed_user() call is a no-op
-- if the email already exists, so adding a new hire later just means adding
-- one more seed_user(...) line and re-running the file.
--
-- SECURITY NOTE: each password below is also sitting in this chat's history.
-- Whenever it's convenient, consider rotating any of these passwords from
-- the Supabase Auth dashboard (Authentication → Users → pick a user →
-- "Send password recovery" or set a new one directly) — that does not
-- require touching this file or re-running it.

set search_path = public, extensions;

create or replace function seed_user(
  p_email text, p_name text, p_role user_role, p_code text, p_mobile text,
  p_password text
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
    p_email, crypt(p_password, gen_salt('bf')),
    now(), now(), now(), null,
    jsonb_build_object('provider', 'email', 'providers', array['email']),
    jsonb_build_object(
      'full_name', p_name, 'role', p_role::text,
      'employee_code', p_code, 'mobile', p_mobile
    ),
    '', '', '', ''
  );

  return v_id;
end;
$$;

select seed_user('guard@r360.local',    'Sanjeev Kumar',  'security_guard',  'EMP-G01', '9876500001', 'Guard@2026!');
select seed_user('boopathi@r360.local', 'Boopathi',       'ops_manager',     'EMP-O01', '9876500002', 'OpsMgr@2026!');
select seed_user('opsbackup@r360.local','Ramesh Iyer',    'admin',           'EMP-O02', '9876500003', 'B@ckupAdm!n2026#Xk');
select seed_user('offload@r360.local',  'Arun Prasad',    'offloading',      'EMP-F01', '9876500004', 'Offload@2026!');
select seed_user('inbound@r360.local',  'Divya Nair',     'offloading',      'EMP-I01', '9876500005', 'Inbound@2026!');
select seed_user('store@r360.local',    'Mahesh Rao',     'warehouse_staff', 'EMP-W01', '9876500006', 'Store@2026!');
select seed_user('match1@r360.local',   'Lakshmi Devi',   'invoice_matcher', 'EMP-M01', '9876500007', 'Match1@2026!');
select seed_user('match2@r360.local',   'Priya Menon',    'invoice_matcher', 'EMP-M02', '9876500008', 'Match2@2026!');
select seed_user('pack1@r360.local',    'Kavitha S',      'packer',          'EMP-P01', '9876500009', 'Pack1@2026!');
select seed_user('pack2@r360.local',    'Anitha R',       'packer',          'EMP-P02', '9876500010', 'Pack2@2026!');
select seed_user('admin@r360.local',    'Warehouse Admin','admin',           'EMP-A01', '9876500011', 'Adm!n#2026$Xk9Qz');

update profiles set is_backup_approver = true
 where employee_code = 'EMP-O02';

update profiles set badge_code = generate_badge_code()
 where role in ('packer', 'invoice_matcher', 'admin') and badge_code is null;

drop function seed_user(text, text, user_role, text, text, text);
