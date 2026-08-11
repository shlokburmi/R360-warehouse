-- 0011_pickup_types.sql
-- PHASE 4 types. Separate migration because ALTER TYPE ADD VALUE cannot be used
-- in the transaction that added it, and the CLI runs one transaction per file.

create type pickup_status as enum (
  'registered',  -- vehicle and people logged at the gate
  'verifying',   -- cartons being scanned onto the vehicle
  'verified',    -- CONTROL POINT 7 passed: every released carton present
  'departed',    -- time_out stamped; terminal
  'cancelled'
);

-- Why a gate-exit scan can be refused.
alter type scan_reject_reason add value if not exists 'batch_not_released';
alter type scan_reject_reason add value if not exists 'no_pickup_registered';
alter type scan_reject_reason add value if not exists 'wrong_pickup';
