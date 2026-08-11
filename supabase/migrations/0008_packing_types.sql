-- 0008_packing_types.sql
-- PHASE 3 types, kept in their own migration on purpose.
--
-- Postgres forbids *using* a value added by ALTER TYPE ADD VALUE in the same
-- transaction that added it, and the Supabase CLI runs each migration file in
-- one transaction. Splitting the type changes out means 0009 can reference these
-- values freely.

create type batch_status as enum (
  'open',      -- cartons assigned, nothing out-scanned yet
  'scanning',  -- out-scan in progress
  'complete',  -- CONTROL POINT 6 passed: every assigned carton scanned
  'released',  -- handed over to the pickup area
  'cancelled'
);

-- Out-scan can fail for reasons a sticker scan never could.
alter type scan_reject_reason add value if not exists 'not_packed';
alter type scan_reject_reason add value if not exists 'not_in_batch';
alter type scan_reject_reason add value if not exists 'batch_closed';
