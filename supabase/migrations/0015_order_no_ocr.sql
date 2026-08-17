-- 0015_order_no_ocr.sql
--
-- PRD §5.4 — capture the Order No printed on the delivery challan.
--
-- Why OCR at all, in a system built entirely around barcodes: the challan
-- carries two barcodes, the DC No (`SC0629159`) and the courier's tracking
-- number (`REWP0000906496`). The Order No — `CP002458380_0001` — is printed as
-- plain text in the header block and is encoded nowhere. So the only ways to
-- get it off the paper are a human typing it or a camera reading it, and a
-- matcher holding a box in one hand types badly.
--
-- ===========================================================================
-- WHY THE OCR READ IS RECORDED SEPARATELY FROM THE VALUE
-- ===========================================================================
--
-- `invoices.order_no` is the value the warehouse acts on. `order_no_scans` is
-- the record of how that value came to exist.
--
-- These have to be two different things because OCR is *probabilistic* and
-- every other input in this system is not. A barcode either decodes or it does
-- not; there is no such thing as a barcode that decodes to something slightly
-- wrong. OCR routinely confuses 0/O, 1/I, 5/S and 8/B, which on a string like
-- CP002458380_0001 means a read can be wrong while looking entirely plausible.
--
-- When someone asks six months from now why a shipment was booked against the
-- wrong order, "the OCR misread it" is only a usable answer if the raw text,
-- the engine's confidence, and whether a human corrected it were all kept.
-- Storing just the final string throws away the evidence and leaves the
-- question unanswerable — the same reasoning as 0014's purge record.

-- ===========================================================================
-- THE VALUE
-- ===========================================================================

alter table invoices add column order_no text;

-- The format is fixed: two letters, nine digits, underscore, four digits.
--
-- This constraint is load-bearing rather than decorative. It is the last line
-- of defence against a misread reaching the table, and it catches the most
-- common OCR failure directly: a letter substituted into a digit run
-- (CP0O2458380_0001) violates it, so the database refuses the write even if
-- every layer above was satisfied. A looser pattern would let exactly the
-- errors OCR makes pass through.
alter table invoices add constraint invoices_order_no_format
  check (order_no is null or order_no ~ '^CP[0-9]{9}_[0-9]{4}$');

-- Deliberately NOT unique. The `_0001` suffix indexes a shipment within order
-- CP002458380, so distinct invoices legitimately share the order prefix, and
-- whether the full string can repeat across re-dispatches is not something this
-- migration is in a position to assert. An index without a uniqueness claim
-- makes the lookup fast without inventing a business rule.
create index invoices_order_no_idx on invoices (order_no) where order_no is not null;

comment on column invoices.order_no is
  'Order No from the delivery challan header, e.g. CP002458380_0001. Populated '
  'by OCR with operator confirmation, or by manual entry. See order_no_scans '
  'for the provenance of any given value.';

-- ===========================================================================
-- THE PROVENANCE
-- ===========================================================================

create table order_no_scans (
  id            uuid primary key default gen_random_uuid(),
  invoice_id    uuid not null references invoices(id) on delete restrict,

  -- What the engine actually returned, before any parsing. Kept because the
  -- parsed value cannot explain its own mistakes: to tell a camera-focus
  -- problem from a bad crop region you need to see that the raw block said
  -- "Order No : CPOO2458380_00O1" rather than nothing at all.
  raw_text      text,

  -- Null when the read failed to yield a conforming string. A failed read is
  -- still worth a row — a station whose reads fail all morning is a dirty lens
  -- or a bad crop, and that pattern is only visible if misses are recorded.
  parsed_order_no text
    check (parsed_order_no is null or parsed_order_no ~ '^CP[0-9]{9}_[0-9]{4}$'),

  -- Engine confidence, 0-100. Meaningless for manual entry, hence nullable.
  confidence    numeric(5,2) check (confidence is null or confidence between 0 and 100),

  -- 'ocr'  — the camera proposed it and the operator accepted it as read.
  -- 'manual' — typed, either as fallback or because OCR was refused.
  -- A plain check rather than a new enum: this is a provenance flag internal to
  -- one table, not a domain concept other tables branch on.
  source        text not null check (source in ('ocr', 'manual')),

  -- True when OCR proposed a value and the operator changed it before
  -- accepting. This is the single most useful column here: its rate over time
  -- is the honest measure of whether the OCR is helping or being worked around.
  was_corrected boolean not null default false,

  scanned_by    uuid not null references profiles(id),
  scanned_at    timestamptz not null default now(),

  -- A manual entry has nothing to be corrected from, so the combination is
  -- incoherent rather than merely unusual.
  constraint order_no_scans_correction_needs_ocr
    check (not (source = 'manual' and was_corrected))
);

create index order_no_scans_invoice_idx on order_no_scans (invoice_id, scanned_at desc);

comment on table order_no_scans is
  'Append-only log of every attempt to read an Order No off a challan, '
  'successful or not. The evidence behind invoices.order_no.';

-- ===========================================================================
-- IMMUTABILITY, AUDIT AND RLS
--
-- Same three-trigger treatment 0003 gives every business table, plus the
-- append-only rule scan_events gets. A scan that happened cannot be edited
-- into a scan that didn't; a correction is the next row, and the pair of rows
-- is what tells you the first read was wrong.
-- ===========================================================================

create trigger trg_order_no_scans_audit after insert or update on order_no_scans
  for each row execute function fn_audit();

create trigger trg_order_no_scans_no_delete before delete on order_no_scans
  for each row execute function fn_no_delete();

create trigger trg_order_no_scans_no_update before update on order_no_scans
  for each row execute function fn_no_update();

alter table order_no_scans enable row level security;
alter table order_no_scans force row level security;
revoke all on order_no_scans from anon, authenticated;
grant select, insert on order_no_scans to authenticated;

create policy order_no_scans_read on order_no_scans
  for select to authenticated using (true);

-- `scanned_by = auth.uid()` mirrors the scan_events and invoice_verifications
-- policies: you may record that *you* read a challan, never that someone else
-- did. Attribution in this system is never a client-supplied field.
create policy order_no_scans_insert on order_no_scans
  for insert to authenticated
  with check (
    has_role('invoice_matcher', 'ops_manager', 'admin')
    and scanned_by = auth.uid()
  );

-- ===========================================================================
-- EXPOSE order_no THROUGH THE READ PATH
--
-- Every invoice read in the backend goes through v_invoice_status, not the
-- table, so a column the view does not select does not exist as far as the API
-- is concerned. Recreated verbatim from 0009 with one line added.
--
-- `order_no` is appended at the *end* of the select list, not slotted in next to
-- invoice_number where it reads better. `create or replace view` can only add
-- columns to the end — inserting one mid-list makes Postgres see it as renaming
-- every subsequent column and it refuses:
--
--     ERROR: cannot change name of view column "sku" to "order_no"
--
-- The alternative is drop-and-recreate, which would take the grants and the
-- security_invoker setting with it.
-- ===========================================================================

create or replace view v_invoice_status as
select
  i.id                        as invoice_id,
  i.invoice_number,
  i.sku,
  i.units,
  i.customer_name,
  i.is_open,
  i.purchase_order_line_id,
  pol.description,
  iv.verified_by,
  vp.full_name                as verified_by_name,
  iv.verified_at,
  pr.id                       as packing_record_id,
  pr.packed_by,
  pp.full_name                as packed_by_name,
  pr.packed_at,
  pr.batch_id,
  b.batch_code,
  b.status::text              as batch_status,
  pr.out_scanned_at,
  case
    when not i.is_open                  then 'closed'
    when pr.out_scanned_at is not null  then 'out_scanned'
    when pr.batch_id is not null        then 'batched'
    when pr.id is not null              then 'packed'
    when iv.id is not null              then 'verified'
    else 'open'
  end                         as stage,
  i.order_no
from invoices i
left join purchase_order_lines pol on pol.id = i.purchase_order_line_id
left join invoice_verifications iv on iv.invoice_id = i.id
left join profiles vp on vp.id = iv.verified_by
left join packing_records pr on pr.invoice_id = i.id
left join profiles pp on pp.id = pr.packed_by
left join batches b on b.id = pr.batch_id;

-- 0010 made this view honour the caller's RLS instead of the definer's. That
-- property is not carried by `create or replace`, and losing it would let any
-- authenticated request read every invoice regardless of policy — so it is
-- re-asserted here rather than assumed.
alter view v_invoice_status set (security_invoker = true);

grant select on v_invoice_status to authenticated;
