-- 0027_strict_vehicle_number_format.sql
--
-- The user asked for one fixed, compulsory vehicle-number format across the
-- app: exactly 2 letters, 2 digits, 2 letters, 4 digits (e.g. KA01AB1234),
-- nothing more, nothing less. The Pydantic-level check
-- (GateEntryCreate/PickupCreate.vehicle_number, via
-- clean_and_validate_vehicle in schemas/gate.py) already enforces this, but
-- per DECISIONS.md §B3 — "a rule that lives only in application code is one
-- hotfix away from being bypassed" — the actual guarantee has to be the
-- database constraint, not the friendly error in front of it. Both tables
-- had the old, much looser `^[A-Z0-9-]{4,15}$` check.

alter table gate_entries drop constraint gate_entries_vehicle_number_check;
alter table gate_entries add constraint gate_entries_vehicle_number_check
  check (vehicle_number ~ '^[A-Z]{2}[0-9]{2}[A-Z]{2}[0-9]{4}$');

alter table pickups drop constraint pickups_vehicle_number_check;
alter table pickups add constraint pickups_vehicle_number_check
  check (vehicle_number ~ '^[A-Z]{2}[0-9]{2}[A-Z]{2}[0-9]{4}$');
