-- 0020_carton_sticker_type.sql
--
-- Outbound cartons get their own printed, Admin-issued QR sticker — a third
-- sticker family alongside box and unit. DECISIONS.md §CC1 originally chose
-- the invoice number alone as the carton's label, for lack of a printer at
-- the packing/matching station. A printer is now available on the floor, so
-- this adds the sticker without removing the invoice-number fallback CC1
-- relied on — see 0021 for the resolver change that keeps both working.
--
-- Separate from 0021 for the same reason 0016 is separate from 0017-0019:
-- `alter type ... add value` commits the label, but the new value cannot be
-- referenced by any statement in the same transaction.

alter type sticker_type add value if not exists 'carton';

-- A carton sticker belongs to an invoice, not a gate entry or a sheet — it is
-- minted outbound, long after the truck that brought the goods in has
-- departed, and one at a time rather than in a printed batch.
alter table stickers alter column gate_entry_id drop not null;
alter table stickers alter column sheet_id drop not null;

alter table stickers
  add column if not exists invoice_id uuid references invoices(id) on delete restrict;

create index stickers_invoice_idx on stickers (invoice_id) where invoice_id is not null;

-- The family-shape CHECK constraint and the one-live-carton-per-invoice index
-- both have to name the 'carton' literal, so both live in 0021 — a statement
-- in this same transaction cannot reference the value `alter type` just added.
--
-- No RLS changes needed: `stickers_insert` already gates every sticker insert
-- to is_ops() (0005), and `stickers_read` is already open to all authenticated
-- staff. A carton sticker is issued and read through exactly the same policies
-- as a box or unit sticker.
