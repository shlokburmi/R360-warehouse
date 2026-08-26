-- 0013_admin_provisioning.sql
--
-- The Admin screen (PRD §2 "Admin", §8 approval rules): provisioning staff,
-- changing a role, deactivating someone who has left, and issuing or revoking
-- attribution badges. All four were SQL jobs run by hand until now, which meant
-- the one operation with a real security invariant attached — issuing a badge —
-- was performed by whoever had a psql prompt, with no audit actor recorded.
--
-- Writing it surfaced a hole in the invariant from DECISIONS.md §CC2, fixed
-- first below because everything after it depends on the invariant holding.

-- ===========================================================================
-- 1. THE AUDIT LOG WAS LEAKING BADGE CODES
--
-- §CC2 revokes `select (badge_code)` from `authenticated`, because reading
-- someone's badge code is equivalent to holding their badge. But fn_audit
-- stores `to_jsonb(new)` for every profiles write, and `audit_read` lets
-- anyone passing is_ops() read audit_log. So an Ops Manager could read every
-- packer's badge code out of the audit trail.
--
-- That defeats CONTROL POINT 5 exactly as §CC2 describes: an Ops Manager
-- carries a badge, so they could verify an invoice with their own badge and
-- then pack it with a packer's code lifted from the trail — the two-person
-- rule satisfied by one person. The column grant was the visible door; this
-- was the open window beside it.
--
-- The fix is to redact, not to stop auditing. "Someone's badge changed, and
-- here is who changed it and when" is exactly what the trail is for; the value
-- itself is what must not survive in a readable table.
-- ===========================================================================

create or replace function fn_audit_redact(p_table text, p_row jsonb)
returns jsonb
language plpgsql
immutable
as $$
declare
  k text;
begin
  if p_row is null or p_table <> 'profiles' then
    return p_row;
  end if;

  -- Null is preserved as null rather than redacted, so the trail still answers
  -- "did this person have a badge at all?" — which is a legitimate audit
  -- question — without answering "what was it?".
  foreach k in array array['badge_code', 'mobile'] loop
    if p_row ? k and p_row -> k <> 'null'::jsonb then
      p_row := jsonb_set(p_row, array[k], '"[redacted]"'::jsonb);
    end if;
  end loop;

  return p_row;
end;
$$;

comment on function fn_audit_redact(text, jsonb) is
  'Strips values that must not be readable from an audit row. See DECISIONS.md §CC2.';

-- Unchanged from 0003 except for the two redaction calls in the INSERT.
-- `changed_keys` is still computed from the raw rows above, so a badge reissue
-- is still visible *as* a badge reissue.
create or replace function fn_audit()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
  v_before jsonb;
  v_after  jsonb;
  v_keys   text[];
  v_id     uuid;
begin
  if tg_op = 'UPDATE' then
    v_before := to_jsonb(old);
    v_after  := to_jsonb(new);
    select array_agg(key order by key)
      into v_keys
      from jsonb_each(v_after)
     where value is distinct from (v_before -> key);

    if v_keys is null then
      return new;
    end if;
  else
    v_after := to_jsonb(new);
  end if;

  begin
    v_id := (v_after ->> 'id')::uuid;
  exception when others then
    v_id := null;
  end;

  insert into audit_log (
    table_name, record_id, action, actor_id, actor_role, actor_source,
    before_data, after_data, changed_keys
  ) values (
    tg_table_name, v_id, tg_op, current_actor_id(), current_actor_role()::text,
    coalesce(nullif(current_setting('app.actor_source', true), ''), 'api'),
    fn_audit_redact(tg_table_name, v_before),
    fn_audit_redact(tg_table_name, v_after),
    v_keys
  );

  return new;
end;
$$;

-- Rows written before this migration still hold the plaintext, and audit_log is
-- append-only — fn_no_update() refuses to let even this migration rewrite it.
-- That refusal is correct and worth keeping: a trail that can be edited to
-- remove an inconvenient row is not a trail, and "we edited it for a good
-- reason this time" is not a property anyone can verify later.
--
-- So the leak is remediated the way a leaked credential always is: rotate,
-- don't try to un-publish. Every badge code that was exposed is replaced here,
-- which makes the copies sitting in the historical trail worthless. The trail
-- keeps saying exactly what happened, and what it says is now harmless.
--
-- The operational cost is real and deliberate: **every existing badge must be
-- reprinted after this migration runs.** Issue the replacements from the Admin
-- screen, which is now the only place that can.
update profiles
   set badge_code = generate_badge_code()
 where badge_code is not null;

-- ===========================================================================
-- 2. ISSUING A BADGE
--
-- SECURITY DEFINER for the same reason resolve_badge_holder() is: the caller
-- must be able to mint a code without being able to read the column. This is
-- the *only* operation in the system that returns a badge code, and it returns
-- one it has just created, to the Admin who asked for it, once.
--
-- There is deliberately still no way to read an existing code. "Reissue" means
-- mint a new one, which invalidates the old — so a lost badge is replaced, not
-- looked up. That keeps §CC2 intact: at no point can anyone learn the code of
-- a badge that is currently in someone else's pocket.
-- ===========================================================================

-- `extensions` is on the search_path deliberately. pgcrypto lives there on
-- Supabase (the `create extension` in 0001 is a no-op against a project that
-- already has it), so generate_badge_code() — plain `language sql` with no
-- search_path of its own — resolves gen_random_bytes() from whatever the caller
-- had. A definer function pinned to `public` alone therefore fails at the point
-- of minting a code, which is a long way from where the mistake looks like it
-- is.
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
  -- The privilege check is the whole security boundary here, because the
  -- definer context has already bypassed RLS by the time this line runs.
  if not is_admin() then
    raise exception 'Only an Admin can issue an attribution badge.'
      using errcode = 'insufficient_privilege',
            hint = 'Ask an Admin to issue or reissue this badge.';
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

  -- Badges only mean something at the stations that scan one. A badge on a
  -- guard would be attribution for work the guard never does.
  if v_role not in ('packer', 'admin') then
    raise exception 'A % does not carry an attribution badge.', v_role
      using hint = 'Badges are for packers, and the Admins who match '
                   'invoices or cover the packing bench.';
  end if;

  -- generate_badge_code() is 64 bits of randomness, so this loop effectively
  -- never runs twice. It exists because `badge_code` is unique and a collision
  -- surfacing as a constraint error on an Admin's screen would be a mystery.
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

comment on function admin_issue_badge(uuid) is
  'Mints a fresh attribution badge and returns it once. The only function that '
  'ever returns a badge code; there is no way to read an existing one.';

create or replace function admin_revoke_badge(p_profile_id uuid)
returns void
language plpgsql
volatile
security definer
set search_path = public
as $$
begin
  if not is_admin() then
    raise exception 'Only an Admin can revoke an attribution badge.'
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

revoke all on function admin_issue_badge(uuid) from public, anon;
revoke all on function admin_revoke_badge(uuid) from public, anon;
grant execute on function admin_issue_badge(uuid) to authenticated;
grant execute on function admin_revoke_badge(uuid) to authenticated;

-- ===========================================================================
-- 3. "DOES THIS PERSON HAVE A BADGE?" WITHOUT READING IT
--
-- The Admin screen has to show whether a badge is outstanding, which is one bit
-- of a column nobody may read. A view computing `badge_code is not null` does
-- not solve it: with security_invoker the caller needs SELECT on badge_code to
-- evaluate the expression, and without security_invoker the view bypasses RLS
-- on profiles entirely — which is the bug 0010 was written to fix.
--
-- A generated column derives the bit once, in the table, where it can be
-- granted separately from the value it came from. Same pattern as
-- locations.is_quarantine (DECISIONS.md §C3): the derivation is the database's
-- job, so it cannot drift from the thing it describes.
-- ===========================================================================

alter table profiles
  add column has_badge boolean
  generated always as (badge_code is not null) stored;

grant select (has_badge) on profiles to authenticated;

comment on column profiles.has_badge is
  'Whether a badge has ever been issued. Readable; badge_code is not.';

-- ===========================================================================
-- 4. THE STAFF DIRECTORY
--
-- One row per staff member with enough context to act: the role, whether the
-- account is live, whether a badge is outstanding, and whether the person has
-- actually been used for attribution. The last of those is what makes
-- "deactivate" a considered decision rather than a guess.
--
-- security_invoker, like every other view since 0010: each column below is
-- granted to `authenticated`, and badge_code is deliberately not among them.
-- ===========================================================================

create view v_staff_directory
with (security_invoker = true)
as
select
  p.id,
  p.full_name,
  p.employee_code,
  p.role::text                                   as role,
  p.is_active,
  p.is_backup_approver,
  p.has_badge,
  p.badge_active,
  p.has_badge and p.badge_active                 as badge_usable,
  p.created_at,
  coalesce(v.verified_count, 0)::int             as invoices_verified,
  coalesce(k.packed_count, 0)::int               as cartons_packed,
  greatest(v.last_at, k.last_at)                 as last_attributed_at
from profiles p
left join (
  select verified_by as id, count(*) as verified_count, max(verified_at) as last_at
    from invoice_verifications group by verified_by
) v on v.id = p.id
left join (
  select packed_by as id, count(*) as packed_count, max(packed_at) as last_at
    from packing_records group by packed_by
) k on k.id = p.id;

grant select on v_staff_directory to authenticated;

comment on view v_staff_directory is
  'One row per staff account for the Admin screen. Never exposes badge_code — '
  'only whether a badge exists and whether it is active.';
