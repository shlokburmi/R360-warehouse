-- 0022_role_and_scan_types.sql
-- New enum values only. A value just added by ALTER TYPE ... ADD VALUE cannot
-- be relied on within the same transaction it was added in, so this migration
-- is deliberately standalone — every migration below reads as its own
-- transaction, and nothing here references these values yet.
--
-- Reintroduces three of the roles PRD §2 originally called for, which had
-- been consolidated into `admin`/`offloading` (see docs/DECISIONS.md §CE1,
-- §C5). `match_unit` and `invoice_already_matched` support a new hard stop
-- at invoice matching, mirroring the existing `pack_unit` mechanism (§CG3).

alter type user_role add value 'ops_manager';
alter type user_role add value 'invoice_matcher';
alter type user_role add value 'warehouse_staff';

alter type scan_type add value 'match_unit';

alter type scan_reject_reason add value 'invoice_already_matched';
