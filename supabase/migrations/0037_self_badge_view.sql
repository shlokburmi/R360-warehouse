-- 0037_self_badge_view.sql
--
-- Lets a badge holder see her OWN current badge as a QR, so it can be
-- scanned straight off her dashboard instead of a printed card — e.g. an
-- Invoice Matcher scanning a Packer's badge from her screen at
-- /invoices/assign, the same call that already accepts a scan off a
-- physical card (0017_packing_assignment.sql).
--
-- This is a narrower version of the rule in 0013/§CC2 ("no operation reads
-- an existing code back"), not a repeal of it: my_badge_code() only ever
-- returns the *caller's own* code, resolved from auth.uid() inside the
-- function body — there is still no function, and there will still never
-- be one, that lets a person read anyone else's. CONTROL POINT 5 depends on
-- one packer being unable to read another's badge, and that stays true.

create or replace function my_badge_code()
returns text
language sql
stable
security definer
set search_path = public
as $$
  select badge_code
    from profiles
   where id = auth.uid()
     and badge_active
     and badge_code is not null;
$$;

comment on function my_badge_code() is
  'Returns the CALLING user''s own current badge code, or null if they have '
  'none active. Self-only by construction (auth.uid()) — there is still no '
  'way to read anyone else''s, see resolve_badge_holder() and §CC2.';

revoke all on function my_badge_code() from public, anon;
grant execute on function my_badge_code() to authenticated;
