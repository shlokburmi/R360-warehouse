-- 0038_ops_manager_badge_issue.sql
--
-- Widens badge issue/reissue/revoke from Admin-only to Admin-or-Ops-Manager,
-- at the user's explicit request, made with the CONTROL POINT 5 trade-off
-- stated plainly first (DECISIONS.md §CE1): an Ops Manager who can issue a
-- badge can mint one under a fake name and satisfy the "second person"
-- requirement with a badge that has no real second person behind it. The
-- user accepted that risk in exchange for Ops Manager being able to manage
-- badges without an Admin's help — the same shape 0023_role_split.sql already
-- used for gate/exit decisions, sticker sheets, out-scan and batch release.
--
-- Both functions keep every other check unchanged (role eligibility, active
-- account, code collision) — only the "who is allowed to call this at all"
-- guard moves from is_admin() to is_ops_manager() (admin or ops_manager,
-- 0023_role_split.sql).

create or replace function admin_issue_badge(p_profile_id uuid)
returns text
language plpgsql
volatile
security definer
set search_path = public, extensions
as $$
declare
  v_role user_role;
  v_active boolean;
  v_code text;
begin
  if not is_ops_manager() then
    raise exception 'Only an Admin or Ops Manager can issue an attribution badge.'
      using errcode = 'insufficient_privilege',
            hint = 'Ask an Admin or Ops Manager to issue or reissue this badge.';
  end if;

  select role, is_active into v_role, v_active
    from profiles where id = p_profile_id;

  if v_role is null then
    raise exception 'No staff member with that id.';
  end if;

  if not v_active then
    raise exception 'That account is deactivated, so it cannot hold a badge.'
      using hint = 'Reactivate the account first if this person is back on shift.';
  end if;

  if v_role not in ('packer', 'invoice_matcher', 'admin') then
    raise exception 'A % does not carry an attribution badge.', v_role
      using hint = 'Badges are for packers, invoice matchers, and the Admins '
                   'who cover either station.';
  end if;

  loop
    v_code := generate_badge_code();
    exit when not exists (select 1 from profiles where badge_code = v_code);
  end loop;

  update profiles
     set badge_code = v_code,
         badge_active = true
   where id = p_profile_id;

  return v_code;
end;
$$;

create or replace function admin_revoke_badge(p_profile_id uuid)
returns void
language plpgsql
volatile
security definer
set search_path = public
as $$
begin
  if not is_ops_manager() then
    raise exception 'Only an Admin or Ops Manager can revoke an attribution badge.'
      using errcode = 'insufficient_privilege';
  end if;

  -- badge_code is left in place rather than nulled. Past packing records point
  -- at the person, not the code, but keeping the row intact means the audit
  -- trail for "this badge was revoked" has a subject, and PRD §7 says nothing
  -- is erased — it is superseded.
  update profiles
     set badge_active = false
   where id = p_profile_id
     and badge_code is not null;

  if not found then
    raise exception 'That person has no badge to revoke.';
  end if;
end;
$$;
