-- 0035_packer_invoice_creation.sql
--
-- Invoices are no longer typed into the dashboard at all. A Packer creates
-- one by scanning the physical invoice with the camera — OCR reads the Order
-- Number already printed on it, she picks the PO/product line and types the
-- unit count, and that's the whole invoice. She then hands the carton to a
-- different packing lady by scanning that packer's badge (already-existing
-- flow, unchanged) to actually pack it.
--
-- This requires Packer to be able to do everything Invoice Matcher could
-- already do around invoice creation/matching. Widening the database side to
-- match the same widening on the API layer surfaced three places that were
-- still scoped to a narrower role set than the endpoints in front of them
-- already allow — each would have silently blocked a Packer (or, in the
-- order_no_scans case, likely already silently blocks a non-admin Invoice
-- Matcher today).

-- ===========================================================================
-- 1. Creating/updating an invoice
-- ===========================================================================

drop policy if exists invoices_write on invoices;
create policy invoices_write on invoices for all to authenticated
  using (has_role('packer', 'invoice_matcher', 'ops_manager', 'admin'))
  with check (has_role('packer', 'invoice_matcher', 'ops_manager', 'admin'));

-- ===========================================================================
-- 2. Recording an Order No OCR read
--
-- Was admin-only (0015_order_no_ocr.sql) even though record_order_no's own
-- API guard already allowed Invoice Matcher/Ops Manager — a non-admin
-- Invoice Matcher's OCR read was likely already failing at this insert
-- before this change existed. Packer needs the same access now too.
-- ===========================================================================

drop policy if exists order_no_scans_insert on order_no_scans;
create policy order_no_scans_insert on order_no_scans
  for insert to authenticated
  with check (
    has_role('packer', 'invoice_matcher', 'ops_manager', 'admin')
    and scanned_by = auth.uid()
  );

-- ===========================================================================
-- 3. Whose badge may verify an invoice (CONTROL POINT 5, first half)
--
-- fn_badge_holder_guard (0023_role_split.sql) only allowed invoice_matcher/
-- admin badges to be recorded as invoice_verifications.verified_by. A Packer
-- scanning her own badge at this step was refused outright.
-- ===========================================================================

create or replace function fn_badge_holder_guard()
returns trigger
language plpgsql
as $$
declare
  v_who       uuid;
  v_role      user_role;
  v_usable    boolean;
  v_allowed   user_role[];
  v_what      text;
begin
  if tg_table_name = 'invoice_verifications' then
    v_who := new.verified_by;
    v_allowed := array['invoice_matcher', 'packer', 'admin']::user_role[];
    v_what := 'verify an invoice';
  else
    v_who := new.packed_by;
    v_allowed := array['packer', 'admin']::user_role[];
    v_what := 'pack a carton';
  end if;

  select role, (badge_active and is_active) into v_role, v_usable
    from profiles where id = v_who;

  if v_role is null then
    raise exception 'Unknown badge.' using errcode = 'foreign_key_violation';
  end if;

  if not v_usable then
    raise exception 'That badge has been deactivated.'
      using errcode = 'insufficient_privilege',
            hint = 'Ask an Admin to issue a replacement badge.';
  end if;

  if not (v_role = any(v_allowed)) then
    raise exception 'That badge is not permitted to % (CONTROL POINT 5).', v_what
      using errcode = 'insufficient_privilege';
  end if;

  return new;
end;
$$;
