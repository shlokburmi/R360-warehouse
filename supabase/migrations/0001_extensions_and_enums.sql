-- 0001_extensions_and_enums.sql
-- Reward360 Warehouse Management — base types.
--
-- Enums are used instead of free-text status columns so that an invalid state is
-- rejected by the database rather than discovered in a report three weeks later.

create extension if not exists "pgcrypto";      -- gen_random_uuid(), digest()
create extension if not exists "pg_trgm";       -- fuzzy vendor/driver name search

-- ---------------------------------------------------------------------------
-- People and access
-- ---------------------------------------------------------------------------

create type user_role as enum (
  'security_guard',
  'ops_manager',
  'offloading',
  'warehouse_staff',
  'invoice_matcher',
  'packer',
  'inbound',
  'admin'
);

-- Role of a person physically arriving on a vehicle (not an app user).
create type visitor_role as enum ('driver', 'laborer', 'supervisor');

-- ---------------------------------------------------------------------------
-- Gate / inbound lifecycle
-- ---------------------------------------------------------------------------

-- Deliberately linear. Legal transitions are enforced by trigger in 0004.
create type gate_entry_status as enum (
  'draft',             -- guard is still filling the form
  'pending_approval',  -- sent to Ops, gate locked
  'rejected',          -- Ops refused; terminal
  'cancelled',         -- guard/ops abandoned before entry; terminal
  'approved',          -- Ops approved, gate may open
  'inside',            -- vehicle admitted, time_in stamped
  'counting',          -- box count declared, stickers being issued/scanned
  'box_verified',      -- CONTROL POINT 2 passed
  'offloading',        -- unit stickers being applied and scanned
  'offloaded',         -- CONTROL POINT 3 passed for all boxes
  'reconciled',        -- CONTROL POINT 4 passed (inbound team)
  'departed'           -- time_out stamped; terminal
);

create type po_status as enum (
  'open', 'partially_received', 'received', 'closed', 'cancelled'
);

create type sticker_type as enum ('box', 'unit');

create type sticker_status as enum (
  'issued',   -- printed on a sheet, not yet on anything
  'applied',  -- physically stuck on a box/unit
  'scanned',  -- verified by a scan
  'void'      -- misprint / damaged / reissued; never reused
);

create type box_status as enum (
  'pending',        -- sticker issued, box not yet scanned at gate
  'verified',       -- CONTROL POINT 2: box sticker scanned and counted
  'scanning',       -- unit stickers being scanned into this box
  'complete',       -- CONTROL POINT 3: scanned units == expected units
  'held',           -- mismatch; awaiting Ops decision. NOTHING enters.
  'short_accepted', -- Ops accepted a short delivery
  'rejected',       -- Ops refused the box
  'emptied'         -- units putaway, carton moved to outside rack
);

create type scan_type as enum (
  'box_verify',   -- guard scanning box stickers at the gate
  'unit_verify',  -- offloader scanning unit stickers
  'out_scan',     -- ops scanning packed cartons (Phase 3)
  'gate_exit'     -- guard verifying released boxes (Phase 4)
);

-- Why a scan was refused. Stored so that "the scanner didn't work" is a
-- claim we can check rather than argue about.
create type scan_reject_reason as enum (
  'unknown_code',
  'wrong_sticker_type',
  'wrong_gate_entry',
  'already_scanned',
  'box_not_open',
  'over_expected_quantity',
  'sticker_void'
);

create type damage_level as enum ('none', 'packaging', 'product');

create type unit_disposition as enum ('stock', 'quarantine');

-- ---------------------------------------------------------------------------
-- Exceptions
-- ---------------------------------------------------------------------------

create type exception_type as enum (
  'box_count_mismatch',
  'unit_count_mismatch',
  'inbound_mismatch',
  'damage',
  'gate_sla_breach',
  'other'
);

create type exception_status as enum (
  'open',       -- raised, waiting on Ops
  'escalated',  -- SLA passed, pushed to admin/superadmin
  'resolved',   -- a named human decided
  'withdrawn'   -- raised in error; requires a reason, never deleted
);

-- The three outcomes from DECISIONS.md §3, plus generic accept/reject for
-- non-count exceptions.
create type exception_resolution as enum (
  'accept_short',
  'recount',
  'reject_box',
  'accept',
  'reject'
);

create type notification_channel as enum ('in_app', 'email');
