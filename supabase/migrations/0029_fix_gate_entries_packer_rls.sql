-- 0029_fix_gate_entries_packer_rls.sql
--
-- 0005_rls.sql was edited in place (commit a86db6c) to rename these three
-- gate_entries UPDATE policies — offload/inbound roles became packer/
-- offloading, matching the reintroduced 7-role split — but editing an
-- already-applied migration has no effect anywhere it already ran. Local dev
-- picked it up because `supabase db reset` replays every migration from
-- scratch; production only ever ran the original version months ago, so it
-- kept the old policies (has_role('offloading')/'inbound') forever. This
-- migration is the real, one-time fix production actually needs.
--
-- Scoped narrowly to these three policies — is_ops() also differs from the
-- current file in production (still recognizes ops_manager, not admin-only),
-- but several *other* policies still depend on that wider behavior and
-- haven't been individually audited yet. Changing it here would risk
-- breaking things that currently work by accident. Out of scope for this fix.

-- Each pair drops both the old (production) and new (already-correct-locally)
-- name before recreating, so this applies cleanly regardless of which state
-- it starts from.
drop policy if exists gate_entries_update_gate on gate_entries;
create policy gate_entries_update_gate on gate_entries
  for update to authenticated
  using (has_role('security_guard') and status in ('draft', 'approved', 'inside'))
  with check (has_role('security_guard') and decided_by is distinct from auth.uid());

drop policy if exists gate_entries_update_offload on gate_entries;
drop policy if exists gate_entries_update_packer on gate_entries;
create policy gate_entries_update_packer on gate_entries
  for update to authenticated
  using (has_role('packer') and status in ('counting', 'box_verified', 'offloading'))
  with check (has_role('packer'));

drop policy if exists gate_entries_update_inbound on gate_entries;
drop policy if exists gate_entries_update_reconcile on gate_entries;
create policy gate_entries_update_reconcile on gate_entries
  for update to authenticated
  using (has_role('offloading') and status in ('offloaded', 'reconciled'))
  with check (has_role('offloading'));
