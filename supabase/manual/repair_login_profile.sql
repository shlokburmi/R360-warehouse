-- repair_login_profile.sql — "Cannot load your profile" after a successful
-- sign-in, on an account that used to work.
--
-- WHAT THAT SCREEN ACTUALLY MEANS
--
-- The password was accepted (GoTrue authenticated it), and then `GET /me`
-- refused. There are only three reasons it can:
--
--   * no_profile       — the account exists in `auth.users` but has no row in
--                        `public.profiles`. Nothing can happen without one: the
--                        role, the name and the pages all come from it.
--   * account_disabled — the profile exists with `is_active = false`.
--   * a 5xx            — the API could not be reached at all.
--
-- Since it is the same screen for all three, section 1 below is the fastest way
-- to tell which. (The app itself now names the reason as well.)
--
-- HOW AN ACCOUNT ENDS UP WITH NO PROFILE
--
-- `fn_handle_new_auth_user` (0003) creates the profile from a trigger on
-- `auth.users`, so an account made *through the app* or through the Supabase
-- dashboard *after* the migrations were pushed always has one. An account
-- created before that trigger existed does not, and neither does one whose
-- profile was hard-deleted (0032_admin_hard_delete_staff.sql removes the
-- profile row and leaves the login).
--
-- Run this in the Supabase Dashboard → SQL Editor for your PRODUCTION project.
-- Section 1 only reads. Section 2 is the repair and is commented out — read
-- section 1 first, then uncomment it and set the email.

-- ---------------------------------------------------------------------------
-- 1. WHICH ACCOUNTS CAN ACTUALLY SIGN IN
-- ---------------------------------------------------------------------------
-- `sign_in_state` is the answer for each login that exists:
--   ok                -> gets past /me
--   NO PROFILE        -> "Cannot load your profile" (fix: section 2)
--   PROFILE INACTIVE  -> "This account has been deactivated" (fix: section 2)

select u.email,
       u.created_at,
       p.id is not null            as has_profile,
       p.role::text                as role,
       p.is_active,
       case
         when p.id is null    then 'NO PROFILE'
         when not p.is_active then 'PROFILE INACTIVE'
         else 'ok'
       end                         as sign_in_state
  from auth.users u
  left join public.profiles p on p.id = u.id
 order by sign_in_state, u.email;

-- ---------------------------------------------------------------------------
-- 2. THE REPAIR
-- ---------------------------------------------------------------------------
-- Uncomment the block, set the email to the account from section 1, and set
-- the role you actually want it to hold. Safe to re-run: it creates the profile
-- if it is missing and reactivates it if it exists, and changes nothing else.
--
-- Roles: security_guard | ops_manager | offloading | warehouse_staff |
--        invoice_matcher | packer | admin
--
-- Note on `admin`: this is the only way to create the *first* Admin, because
-- the Staff screen needs an Admin to already be signed in, and
-- production_staff_accounts.sql deliberately creates no admin password in git.
-- Every Admin after this one should be made from the Staff screen so it lands
-- in the audit trail with a name against it.

-- The two `admin` logins this deployment has are the work accounts named in
-- production_staff_accounts.sql (shlok.b@ and dhruv.g@reward360.co, employee
-- codes EMP-A01 and EMP-O01) — if it is one of those that stopped working,
-- that is the email to put below.

-- do $$
-- declare
--   v_email text := 'CHANGE-ME@example.com';   -- the account from section 1
--   v_role  user_role := 'admin';              -- the role it should hold
--   v_name  text := 'Warehouse Admin';         -- shown in the app header
--   v_code  text := 'EMP-A01';                 -- employee code, must be unique
--   v_id    uuid;
-- begin
--   select id into v_id from auth.users where lower(email) = lower(v_email);
--   if v_id is null then
--     raise exception 'No login exists for %. Create the account first.', v_email;
--   end if;
--
--   insert into public.profiles (id, full_name, role, employee_code, is_active)
--   values (v_id, v_name, v_role, v_code, true)
--   on conflict (id) do update
--       set is_active = true,
--           role = excluded.role,
--           full_name = coalesce(nullif(profiles.full_name, ''), excluded.full_name);
--
--   -- Packers, matchers and admins carry an attribution badge; the others
--   -- never do (fn_badge_holder_guard). Issued only if there isn't one.
--   update public.profiles
--      set badge_code = generate_badge_code()
--    where id = v_id
--      and role in ('packer', 'invoice_matcher', 'admin')
--      and badge_code is null;
--
--   raise notice 'Profile ready for % as %', v_email, v_role;
-- end $$;

-- ---------------------------------------------------------------------------
-- 3. CONFIRM
-- ---------------------------------------------------------------------------
-- Re-run section 1. The account should read 'ok'. Then sign in again — no need
-- to clear anything on the phone; the profile is fetched fresh on every load.
