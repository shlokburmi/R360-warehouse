-- 0034_fix_inbound_reconciliation_role_name.sql
--
-- Production's `inbound_write`/`inbound_update` policies on
-- inbound_reconciliations were still checking has_role('inbound', ...) — the
-- role name from before it was renamed to 'offloading' (0022/0023's role
-- split). 'inbound' is not a value of the user_role enum any more, so no
-- account can ever match it: every Offloading Team submission of CONTROL
-- POINT 4 counts was refused by RLS with "new row violates row-level
-- security policy", surfaced to the operator as "You are not allowed to do
-- this."
--
-- This brings production's two write policies in line with what
-- 0005_rls.sql has defined all along — the gap was never in the tracked
-- migration history, only in production never having received the
-- migration(s) that updated these two policies when the role was renamed.

drop policy if exists inbound_write on inbound_reconciliations;
create policy inbound_write on inbound_reconciliations
  for insert to authenticated
  with check (has_role('offloading', 'admin') and verified_by = auth.uid());

drop policy if exists inbound_update on inbound_reconciliations;
create policy inbound_update on inbound_reconciliations
  for update to authenticated
  using (has_role('offloading', 'admin'))
  with check (has_role('offloading', 'admin') and verified_by = auth.uid());
