-- 0033_ops_manager_staff_admin.sql
-- Ops Manager gets the same staff add/edit/delete access as Admin.
--
-- DECISIONS.md §CE1/§CH1 deliberately kept provisioning off Ops Manager: an
-- Ops Manager who can create and badge accounts could manufacture the second
-- person CONTROL POINT 5 requires (verifier and packer both traced back to the
-- one person who provisioned them). The user has reviewed that trade-off again
-- and asked to grant Ops Manager full staff-management access anyway,
-- reopening it knowingly -- this migration is that grant.
--
-- Scoped to staff CRUD only. Reading name/role/active/badge_active is already
-- open to every authenticated user (`profiles_select_staff`, 0010) so nothing
-- changes there. `profiles_admin_all` -- the "for all" policy that gates
-- insert/update/delete on profiles -- moves from is_admin() to
-- is_ops_manager() (0023 -- already `auth_role() in ('admin', 'ops_manager')`,
-- so Admin keeps every right it had). Badge issue/revoke
-- (admin_issue_badge/admin_revoke_badge) and audit history (audit_read, still
-- is_ops()) are untouched -- this migration does not extend those.

drop policy if exists profiles_admin_all on profiles;
create policy profiles_admin_all on profiles
  for all to authenticated
  using (is_ops_manager()) with check (is_ops_manager());
