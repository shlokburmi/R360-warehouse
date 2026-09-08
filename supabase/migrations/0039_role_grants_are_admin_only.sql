-- 0039_role_grants_are_admin_only.sql
--
-- Ops Manager could make themselves Admin.
--
-- 0033 moved `profiles_admin_all` from is_admin() to is_ops_manager() so that
-- an Ops Manager could add and edit staff. That policy governs the whole row,
-- and `role` is a column on it — so "edit staff" silently included "grant
-- yourself every right Admin has". `update_staff` in the API had no check
-- either. Confirmed over HTTP: PATCH /admin/staff/{own id} {"role":"admin"}
-- answered 200, and /me came back as admin on the next call.
--
-- That hands the holder the two things 0033 said it was not extending — badge
-- issue/revoke and the audit history — plus password reset on every account,
-- which is account takeover of every other user including Admins. It also
-- empties out the reasoning in DECISIONS.md §CE1: the point of keeping
-- provisioning away from Ops Manager is that one person should not be able to
-- manufacture both halves of CONTROL POINT 5, and a self-promotion route makes
-- that separation decorative.
--
-- So: staff CRUD stays with Ops Manager, exactly as 0033 intended, but the
-- `role` column becomes Admin-only. Enforced here rather than only in Python,
-- for the reason in DECISIONS.md §B3 — a rule that lives in one handler is one
-- hotfix away from not being a rule.
--
-- Writes with no authenticated user (migrations, the seed, the worker) are
-- unaffected: auth_role() is null for those, and the guard only fires when a
-- real signed-in actor is doing the writing.

create or replace function fn_role_grant_guard()
returns trigger
language plpgsql
as $$
declare
  v_actor user_role := auth_role();
begin
  -- No signed-in actor: a migration, the seed, or a background worker. Those
  -- run as the owner and are outside this rule.
  if v_actor is null then
    return new;
  end if;

  if tg_op = 'INSERT' then
    if new.role = 'admin' and v_actor <> 'admin' then
      raise exception
        'Only an Admin can create an Admin account.'
        using errcode = 'insufficient_privilege',
              hint = 'Ask an Admin to create it, or create the account with a non-Admin role.';
    end if;
    return new;
  end if;

  if new.role is distinct from old.role and v_actor <> 'admin' then
    raise exception
      'Only an Admin can change what role an account holds (attempted % -> %).',
      old.role, new.role
      using errcode = 'insufficient_privilege',
            hint = 'Staff details, activation and badges stay with Ops Manager; the role does not.';
  end if;

  return new;
end;
$$;

create trigger trg_profiles_role_grant_guard
  before insert or update on profiles
  for each row execute function fn_role_grant_guard();

comment on function fn_role_grant_guard is
  'Role grants are Admin-only (0039). Ops Manager keeps staff CRUD from 0033; '
  'assigning a role — to themselves or anyone else — is not part of it.';
