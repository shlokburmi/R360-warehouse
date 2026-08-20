-- 0016_workflow_types.sql
-- New enum members for the workflow corrections in 0017-0019.
--
-- Separate from the migrations that use them for the same reason 0011 is
-- separate from 0012: `alter type ... add value` commits the label, but the new
-- value cannot be referenced by any statement in the same transaction. A
-- migration that adds a value and then writes it fails, and the failure looks
-- like a typo rather than a transaction rule.

-- Packing now scans each small box (unit) into the carton, so the count issued
-- at the gate can be reconciled against the count actually packed.
alter type scan_type add value if not exists 'pack_unit';

-- A verified vehicle waits for an Ops decision before the gate opens.
alter type pickup_status add value if not exists 'exit_pending';

-- Why a pack-unit scan can be refused. Stored so that "the scanner didn't work"
-- stays a claim we can check rather than argue about.
alter type scan_reject_reason add value if not exists 'wrong_invoice';
alter type scan_reject_reason add value if not exists 'unit_not_in_stock';
alter type scan_reject_reason add value if not exists 'invoice_already_full';

-- Guard's carton count on a finished batch, and the Ops decision on it.
-- Mirrors the gate-entry approval at the other end of the process.
do $$
begin
  if not exists (select 1 from pg_type where typname = 'load_approval_status') then
    create type load_approval_status as enum ('pending', 'approved', 'rejected');
  end if;
end;
$$;
