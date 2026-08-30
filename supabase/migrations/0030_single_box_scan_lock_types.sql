-- 0030_single_box_scan_lock_types.sql
--
-- New enum value only. A value just added by ALTER TYPE ... ADD VALUE cannot
-- be relied on within the same transaction it was added in (0016/0022 do the
-- same split), so this is standalone — 0031 is what actually uses it.
--
-- Only one box may be mid-scan (status 'scanning') at a time per truck: once
-- unit stickers are being scanned into a box, no other box's units may be
-- scanned until that box is damage-checked and closed. Prevents an operator
-- scanning units into the wrong physical box while several are open on the
-- floor at once.

alter type scan_reject_reason add value if not exists 'other_box_open';
