-- 0025_vendor_proposal_and_po_note.sql
--
-- Two gaps surfaced by actually using the Gate Entry screen as a guard:
--
-- 1. A guard has no way to note the real PO number when it exists on the
--    vendor's paperwork but hasn't been entered into this system yet.
--    `purchase_order_id` on gate_entries was already optional (a truck can
--    proceed with none at all) — this adds a free-text companion for "here
--    is the number, attach the real PO once it exists."
--
-- 2. A guard has no way to register a vendor that isn't in the system yet at
--    all. There is no vendor-management UI anywhere in this app — vendors
--    are is_ops()-only (0005_rls.sql:108), seeded, not created through the
--    API. Rather than widen that policy generally, this follows the same
--    narrow-door pattern admin_issue_badge()/resolve_badge_holder() already
--    use (DECISIONS.md §CE2, §CC2): a SECURITY DEFINER function that lets a
--    guard do exactly one thing — insert a vendor row that starts
--    unconfirmed (is_active = false) — and nothing more. Ops/Admin confirms
--    it as part of deciding the very gate entry that named it (see the
--    Python change to decide_entry), not through a separate screen.

alter table gate_entries add column po_reference_note text;

comment on column gate_entries.po_reference_note is
  'Free-text PO number from the delivery challan, for when the real purchase '
  'order has not been entered into the system yet. Not a substitute for '
  'purchase_order_id — Ops still attaches the real PO once it exists.';

create or replace function guard_propose_vendor(p_name text, p_mobile text default null)
returns table (id uuid, name text)
language plpgsql
volatile
security definer
set search_path = public, extensions
as $$
declare
  v_role user_role;
  v_code text;
  v_id uuid;
begin
  -- Qualified explicitly: the RETURNS TABLE column `id` above is otherwise
  -- ambiguous against profiles.id inside this function's own body.
  select role into v_role from profiles where profiles.id = auth.uid();

  if v_role is null or v_role not in ('security_guard', 'admin') then
    raise exception 'Only a Security Guard or Admin may propose a new vendor.'
      using errcode = 'insufficient_privilege';
  end if;

  if length(btrim(coalesce(p_name, ''))) = 0 then
    raise exception 'A vendor name is required.' using errcode = 'check_violation';
  end if;

  if p_mobile is not null and p_mobile !~ '^[6-9][0-9]{9}$' then
    raise exception 'Mobile must be 10 digits starting 6-9.'
      using errcode = 'check_violation';
  end if;

  -- vendors_code_check requires ^[A-Z0-9-]{2,20}$. Collision odds with 8 hex
  -- characters are the same non-issue as generate_badge_code()'s loop.
  loop
    v_code := 'V-' || upper(substr(md5(gen_random_uuid()::text), 1, 8));
    exit when not exists (select 1 from vendors where code = v_code);
  end loop;

  insert into vendors (code, name, contact_mobile, is_active)
  values (v_code, btrim(p_name), p_mobile, false)
  returning vendors.id into v_id;

  return query select v_id, btrim(p_name);
end;
$$;

comment on function guard_propose_vendor(text, text) is
  'The one door a guard has into vendor master data: inserts an unconfirmed '
  '(is_active = false) vendor row. Ops/Admin confirms it by approving the '
  'gate entry that references it (see decide_entry in services/gate.py).';

grant execute on function guard_propose_vendor(text, text) to authenticated;

-- vendors_write was is_ops() (admin-only, 0005_rls.sql:108). decide_entry
-- (services/gate.py) now confirms a pending vendor (is_active = true) as a
-- side effect of approving the gate entry that names it — and gate-entry
-- approval is Ops Manager's job too (0023_role_split.sql), not Admin's
-- alone. Same silent-no-op risk DECISIONS.md Part D warns about, and the
-- same fix already applied to po_lines_write for exactly this reason.
drop policy if exists vendors_write on vendors;
create policy vendors_write on vendors
  for all to authenticated using (is_ops_manager()) with check (is_ops_manager());
