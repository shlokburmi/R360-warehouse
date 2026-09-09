-- 0042_retire_putaway.sql
-- Putaway is removed from the app, at the user's request.
--
-- What went: shelving units onto racks, the rack list, the stock-by-location
-- view built on top of them, and the Warehouse Staff role, whose only job was
-- that step. Goods are now received (CONTROL POINT 3), counted against the
-- inbound team's own figure (CONTROL POINT 4) and that is the end of the
-- inbound process — where the cartons physically sit afterwards is no longer
-- something this app claims to know.
--
-- WHAT THIS DOES NOT DO: drop `putaways` or `locations`. PRD §7's "nothing is
-- ever deleted" applies to a feature being retired as much as to a row being
-- corrected — anything already shelved stays on the record, readable, with its
-- audit trail intact. Both tables keep their SELECT policies and their audit
-- triggers; what goes is every path that writes to them, so the history is
-- frozen rather than erased. Reversing this migration restores the feature; a
-- `drop table` would not.
--
-- The `emptied` box status stays in the enum (Postgres cannot drop an enum
-- value that rows may reference) and stays refused by fn_box_transition_guard.
-- It was only ever reachable through a completed putaway, so from here it is
-- simply a status nothing can set — which is the correct end state, not a gap.

-- ---------------------------------------------------------------------------
-- 1. The writes
-- ---------------------------------------------------------------------------

drop trigger if exists trg_putaways_close_box on putaways;
drop trigger if exists trg_putaways_guard on putaways;

drop function if exists fn_putaway_close_box();
drop function if exists fn_putaway_guard();

-- No policy at all means no INSERT passes, for anyone. The revoke is belt and
-- braces: a future policy added by mistake still finds no privilege behind it.
drop policy if exists putaways_insert on putaways;
revoke insert, update, delete on putaways from authenticated;

-- Racks were master data for a step that no longer exists.
drop policy if exists locations_write on locations;
revoke insert, update, delete on locations from authenticated;

-- ---------------------------------------------------------------------------
-- 2. The reads
-- ---------------------------------------------------------------------------
-- v_putaway_queue and v_box_putaway_status existed only to drive the Putaway
-- screen. v_stock_by_location read `putaways` and nothing else, so with the
-- feature gone it could only ever return what was shelved before today — a
-- stock report frozen at a date, which is worse than no stock report.

drop view if exists v_putaway_queue;
drop view if exists v_box_putaway_status;
drop view if exists v_stock_by_location;

-- `putaways_read` and `locations_read` stay: the rows are history now, and
-- history that cannot be read is just a slower delete.

-- ---------------------------------------------------------------------------
-- 3. The role
-- ---------------------------------------------------------------------------
-- warehouse_staff was carved out of offloading in 0023 for putaway alone, so
-- retiring the step retires the role with it. The enum value stays (same reason
-- as `emptied`), and any account still holding it keeps its login and its
-- read-only view of the exception list — an Admin reassigns or deactivates it
-- from the Staff screen. It is no longer offered when creating an account
-- (ASSIGNABLE_ROLES, backend/app/schemas/admin.py) and no longer grants
-- anything here.

drop policy if exists boxes_update on boxes;
create policy boxes_update on boxes
  for update to authenticated
  using (has_role('packer', 'offloading', 'ops_manager', 'admin'))
  with check (has_role('packer', 'offloading', 'ops_manager', 'admin'));

comment on table putaways is
  'RETIRED 0042. Historical rack placements from when the app ran a putaway '
  'step. Read-only: no policy grants INSERT, and the triggers that maintained '
  'it are gone.';

comment on table locations is
  'RETIRED 0042. The rack list the putaway step used. Kept so the historical '
  'rows in `putaways` still resolve to a readable code.';
