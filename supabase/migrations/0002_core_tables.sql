-- 0002_core_tables.sql
-- Core schema. Phase 1 tables are fully wired; Phase 2-4 tables (locations,
-- invoices, packing, batches) are created now so that foreign keys, RLS and the
-- audit trigger are consistent from day one and later phases are additive only.

-- ===========================================================================
-- PEOPLE
-- ===========================================================================

-- App users. One row per auth.users row, created by trigger on signup.
create table profiles (
  id              uuid primary key references auth.users(id) on delete restrict,
  full_name       text        not null check (length(btrim(full_name)) > 0),
  employee_code   text        unique,
  role            user_role   not null,
  mobile          text        check (mobile ~ '^[6-9][0-9]{9}$'),

  -- Badge used for *attribution* (packer/invoice-matcher), never for login.
  -- Opaque random token; the QR encodes this and nothing else.
  badge_code      text        unique check (badge_code ~ '^BDG-[0-9a-f]{16}$'),
  badge_active    boolean     not null default true,

  -- An Ops Manager flagged as backup receives escalated gate approvals at T+15m.
  is_backup_approver boolean  not null default false,

  is_active       boolean     not null default true,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

comment on column profiles.badge_code is
  'Attribution token only. Proves who handled an item; grants no privileges.';

create index profiles_role_idx on profiles(role) where is_active;
create index profiles_badge_idx on profiles(badge_code) where badge_active;

-- ===========================================================================
-- MASTER DATA
-- ===========================================================================

create table vendors (
  id          uuid primary key default gen_random_uuid(),
  code        text not null unique check (code ~ '^[A-Z0-9-]{2,20}$'),
  name        text not null check (length(btrim(name)) > 0),
  contact_mobile text check (contact_mobile ~ '^[6-9][0-9]{9}$'),
  is_active   boolean not null default true,
  created_at  timestamptz not null default now()
);

create index vendors_name_trgm on vendors using gin (name gin_trgm_ops);

-- Storage locations. Phase 2, but seeded now so quarantine exists in Phase 1.
-- Format Z-AA-RR-LL-BB per DECISIONS.md §6.
create table locations (
  id          uuid primary key default gen_random_uuid(),
  code        text not null unique check (code ~ '^[A-Z]-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2}$'),
  zone        text generated always as (substring(code from 1 for 1)) stored,
  description text,
  is_active   boolean not null default true
);

create index locations_zone_idx on locations(zone) where is_active;

-- ===========================================================================
-- PURCHASE ORDERS — the source of truth for "how many units should arrive"
-- ===========================================================================

create table purchase_orders (
  id          uuid primary key default gen_random_uuid(),
  po_number   text not null unique check (length(btrim(po_number)) > 0),
  vendor_id   uuid not null references vendors(id) on delete restrict,
  status      po_status not null default 'open',
  expected_on date,
  created_at  timestamptz not null default now(),
  created_by  uuid references profiles(id)
);

create index purchase_orders_vendor_idx on purchase_orders(vendor_id, status);

create table purchase_order_lines (
  id              uuid primary key default gen_random_uuid(),
  purchase_order_id uuid not null references purchase_orders(id) on delete restrict,
  line_no         int  not null check (line_no > 0),
  sku             text not null check (length(btrim(sku)) > 0),
  description     text not null,

  expected_units  int  not null check (expected_units > 0),
  units_per_box   int  not null check (units_per_box > 0),

  -- Maintained by the reconciliation service; never decremented directly.
  received_units  int  not null default 0 check (received_units >= 0),
  rejected_units  int  not null default 0 check (rejected_units >= 0),

  unique (purchase_order_id, line_no),
  unique (purchase_order_id, sku)
);

-- Boxes expected on a line, rounded up: 25 units at 10/box = 3 boxes.
create or replace function po_line_expected_boxes(line purchase_order_lines)
returns int language sql immutable as $$
  select ceil(line.expected_units::numeric / line.units_per_box)::int;
$$;

-- ===========================================================================
-- VISITORS — people arriving at the gate. Deduplicated on mobile so that the
-- "photo on first visit only" rule (DECISIONS.md §2) has something to key on.
-- ===========================================================================

create table visitors (
  id            uuid primary key default gen_random_uuid(),
  mobile        text not null unique check (mobile ~ '^[6-9][0-9]{9}$'),
  full_name     text not null check (length(btrim(full_name)) > 0),

  -- Path inside the private `identity-photos` bucket. Never a public URL.
  id_photo_path text,
  id_photo_captured_at timestamptz,

  first_seen_at timestamptz not null default now(),
  last_seen_at  timestamptz not null default now(),
  is_blocked    boolean not null default false,
  blocked_reason text,

  created_at    timestamptz not null default now()
);

-- Photo path and capture time travel together or not at all.
alter table visitors add constraint visitors_photo_consistent
  check (num_nonnulls(id_photo_path, id_photo_captured_at) <> 1);

create index visitors_name_trgm on visitors using gin (full_name gin_trgm_ops);

-- ===========================================================================
-- GATE ENTRIES — CONTROL POINT 1
-- ===========================================================================

create table gate_entries (
  id              uuid primary key default gen_random_uuid(),
  entry_code      text not null unique,   -- human-readable, e.g. GE-20260810-0007
  status          gate_entry_status not null default 'draft',

  vehicle_number  text not null check (vehicle_number ~ '^[A-Z0-9-]{4,15}$'),
  vendor_id       uuid not null references vendors(id) on delete restrict,
  purchase_order_id uuid references purchase_orders(id) on delete restrict,
  transporter_name text,

  -- Request
  requested_by    uuid not null references profiles(id),
  requested_at    timestamptz,

  -- Approval (CONTROL POINT 1). decided_by can never equal requested_by — a
  -- guard approving their own request is the whole thing this prevents.
  decided_by      uuid references profiles(id),
  decided_at      timestamptz,
  decision_note   text,

  -- SLA tracking (DECISIONS.md §4). Escalation moves notifications, never the
  -- decision — there is no auto-approve path anywhere in this schema.
  escalated_at    timestamptz,
  sla_breached    boolean not null default false,

  time_in         timestamptz,
  time_out        timestamptz,

  -- CONTROL POINT 2 counters
  declared_box_count int check (declared_box_count > 0),
  declared_by     uuid references profiles(id),
  declared_at     timestamptz,
  issued_box_sticker_count int not null default 0 check (issued_box_sticker_count >= 0),

  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),

  constraint gate_entries_no_self_approval check (decided_by is distinct from requested_by),
  constraint gate_entries_decision_complete check (num_nonnulls(decided_by, decided_at) <> 1),
  constraint gate_entries_time_order check (time_out is null or time_in is null or time_out >= time_in)
);

create index gate_entries_status_idx on gate_entries(status, requested_at desc);
create index gate_entries_pending_idx on gate_entries(requested_at)
  where status = 'pending_approval';
create index gate_entries_vendor_idx on gate_entries(vendor_id, created_at desc);

comment on constraint gate_entries_no_self_approval on gate_entries is
  'PRD §8: a guard cannot approve their own entry request.';

-- People on the vehicle for this specific entry.
create table gate_entry_persons (
  id             uuid primary key default gen_random_uuid(),
  gate_entry_id  uuid not null references gate_entries(id) on delete restrict,
  visitor_id     uuid not null references visitors(id) on delete restrict,
  visitor_role   visitor_role not null,

  -- Snapshot of the photo used to admit this person on this visit, so that
  -- refreshing a visitor's photo later never rewrites who was let in when.
  id_photo_path  text,
  created_at     timestamptz not null default now(),

  unique (gate_entry_id, visitor_id)
);

create index gate_entry_persons_entry_idx on gate_entry_persons(gate_entry_id);
create index gate_entry_persons_visitor_idx on gate_entry_persons(visitor_id);

-- ===========================================================================
-- STICKERS — issued by Ops, applied and scanned by the floor
-- ===========================================================================

create table sticker_sheets (
  id            uuid primary key default gen_random_uuid(),
  gate_entry_id uuid not null references gate_entries(id) on delete restrict,
  sticker_type  sticker_type not null,
  quantity      int not null check (quantity > 0),
  generated_by  uuid not null references profiles(id),
  generated_at  timestamptz not null default now(),
  pdf_path      text,
  -- Reprints happen (printer jams). Each reprint is a new sheet referencing the
  -- original, so "how many sheets did we print" stays answerable.
  reprint_of_id uuid references sticker_sheets(id)
);

create index sticker_sheets_entry_idx on sticker_sheets(gate_entry_id, sticker_type);

create table stickers (
  id             uuid primary key default gen_random_uuid(),
  code           text not null unique,  -- e.g. BOX-7F3A91C4 / UNT-2B8E04D1
  sticker_type   sticker_type not null,
  status         sticker_status not null default 'issued',

  sheet_id       uuid not null references sticker_sheets(id) on delete restrict,
  gate_entry_id  uuid not null references gate_entries(id) on delete restrict,
  box_id         uuid,  -- FK added after boxes table below
  purchase_order_line_id uuid references purchase_order_lines(id),

  -- For box stickers: how many units this box should contain, printed on the
  -- sticker face so the offloader can check without opening the app.
  expected_units int check (expected_units > 0),

  sequence_no    int not null check (sequence_no > 0),
  void_reason    text,
  created_at     timestamptz not null default now(),

  unique (sheet_id, sequence_no),
  constraint stickers_void_has_reason
    check (status <> 'void' or length(btrim(coalesce(void_reason, ''))) > 0)
);

create index stickers_entry_type_idx on stickers(gate_entry_id, sticker_type, status);
create index stickers_box_idx on stickers(box_id) where box_id is not null;

-- ===========================================================================
-- BOXES — CONTROL POINTS 2 & 3
-- ===========================================================================

create table boxes (
  id             uuid primary key default gen_random_uuid(),
  gate_entry_id  uuid not null references gate_entries(id) on delete restrict,
  sticker_id     uuid not null unique references stickers(id) on delete restrict,
  box_number     int  not null check (box_number > 0),
  status         box_status not null default 'pending',

  purchase_order_line_id uuid references purchase_order_lines(id),
  expected_units int not null check (expected_units > 0),

  -- Maintained exclusively by the scan trigger in 0004. Never written directly
  -- by the API; that is what makes "scanned == expected" a guarantee rather
  -- than a hope.
  scanned_units  int not null default 0 check (scanned_units >= 0),
  quarantined_units int not null default 0 check (quarantined_units >= 0),

  -- CONTROL POINT 2
  verified_at    timestamptz,
  verified_by    uuid references profiles(id),

  -- Damage checkpoint (DECISIONS.md §5). Mandatory before a box can complete.
  damage_level   damage_level,
  damage_note    text,
  damage_checked_by uuid references profiles(id),
  damage_checked_at timestamptz,

  -- CONTROL POINT 3
  completed_at   timestamptz,
  completed_by   uuid references profiles(id),

  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),

  unique (gate_entry_id, box_number),
  constraint boxes_damage_note_required
    check (damage_level is null or damage_level = 'none'
           or length(btrim(coalesce(damage_note, ''))) > 0)
);

alter table stickers
  add constraint stickers_box_fk foreign key (box_id) references boxes(id) on delete restrict;

create index boxes_entry_status_idx on boxes(gate_entry_id, status);
create index boxes_held_idx on boxes(gate_entry_id) where status = 'held';

comment on column boxes.scanned_units is
  'Written only by trg_scan_events_apply. Direct updates are rejected.';

-- Photos backing a damage report.
create table damage_photos (
  id          uuid primary key default gen_random_uuid(),
  box_id      uuid not null references boxes(id) on delete restrict,
  path        text not null,
  uploaded_by uuid not null references profiles(id),
  uploaded_at timestamptz not null default now()
);

-- ===========================================================================
-- SCAN EVENTS — the append-only ledger every count is derived from
-- ===========================================================================

create table scan_events (
  id             uuid primary key default gen_random_uuid(),

  -- Minted on the device before the scan is sent. Unique, so replaying an
  -- offline queue any number of times is a no-op. This is what makes sync safe.
  client_event_id uuid not null unique,

  scan_type      scan_type not null,
  raw_code       text not null,
  sticker_id     uuid references stickers(id) on delete restrict,
  gate_entry_id  uuid references gate_entries(id) on delete restrict,
  box_id         uuid references boxes(id) on delete restrict,

  accepted       boolean not null,
  reject_reason  scan_reject_reason,
  disposition    unit_disposition,

  scanned_by     uuid not null references profiles(id),
  scanned_at     timestamptz not null,   -- device clock
  recorded_at    timestamptz not null default now(),  -- server clock
  was_offline    boolean not null default false,
  device_label   text,

  constraint scan_events_reject_reason_present
    check (accepted = (reject_reason is null)),
  -- An accepted scan must resolve to a sticker. A rejected one need not.
  constraint scan_events_accepted_has_sticker
    check (not accepted or sticker_id is not null)
);

create index scan_events_box_idx on scan_events(box_id) where accepted;
create index scan_events_entry_idx on scan_events(gate_entry_id, scan_type, recorded_at desc);
create index scan_events_sticker_idx on scan_events(sticker_id) where accepted;
create index scan_events_operator_idx on scan_events(scanned_by, recorded_at desc);

-- A sticker can only be accepted once per scan type. This is the database-level
-- guard against double-counting a box or a unit, and it holds even if two
-- devices scan the same sticker in the same millisecond (PRD §7 Isolation).
create unique index scan_events_one_accept_per_sticker
  on scan_events(sticker_id, scan_type) where accepted;

-- ===========================================================================
-- INBOUND RECONCILIATION — CONTROL POINT 4
-- ===========================================================================

create table inbound_reconciliations (
  id             uuid primary key default gen_random_uuid(),
  gate_entry_id  uuid not null references gate_entries(id) on delete restrict,
  purchase_order_line_id uuid not null references purchase_order_lines(id) on delete restrict,

  warehouse_count int not null check (warehouse_count >= 0),  -- from scan ledger
  inbound_count   int not null check (inbound_count >= 0),    -- typed by inbound team
  matched         boolean generated always as (warehouse_count = inbound_count) stored,

  verified_by    uuid not null references profiles(id),
  verified_at    timestamptz not null default now(),

  unique (gate_entry_id, purchase_order_line_id)
);

-- ===========================================================================
-- EXCEPTIONS
-- ===========================================================================

create table exceptions (
  id             uuid primary key default gen_random_uuid(),
  exception_code text not null unique,   -- EX-20260810-0003
  exception_type exception_type not null,
  status         exception_status not null default 'open',

  gate_entry_id  uuid references gate_entries(id) on delete restrict,
  box_id         uuid references boxes(id) on delete restrict,
  purchase_order_id uuid references purchase_orders(id) on delete restrict,
  vendor_id      uuid references vendors(id) on delete restrict,

  title          text not null,
  details        jsonb not null default '{}'::jsonb,

  reported_by    uuid not null references profiles(id),
  reported_at    timestamptz not null default now(),

  escalated_at   timestamptz,
  escalated_to   uuid references profiles(id),

  resolution     exception_resolution,
  resolution_note text,
  resolved_by    uuid references profiles(id),
  resolved_at    timestamptz,

  -- Reversing entry (no-deletion policy). Points at the exception this one
  -- corrects; the original stays visible and stays in reports.
  reverses_id    uuid references exceptions(id),
  reversal_reason text,

  constraint exceptions_resolution_complete
    check (status <> 'resolved' or num_nonnulls(resolution, resolved_by, resolved_at) = 3),
  constraint exceptions_resolution_note_required
    check (resolution is null or length(btrim(coalesce(resolution_note, ''))) > 0),
  constraint exceptions_reversal_has_reason
    check (reverses_id is null or length(btrim(coalesce(reversal_reason, ''))) > 0)
);

create index exceptions_status_idx on exceptions(status, reported_at desc);
create index exceptions_vendor_idx on exceptions(vendor_id, reported_at desc);
create index exceptions_open_idx on exceptions(reported_at) where status in ('open', 'escalated');

-- ===========================================================================
-- NOTIFICATIONS
-- ===========================================================================

create table notifications (
  id             uuid primary key default gen_random_uuid(),
  recipient_id   uuid references profiles(id) on delete restrict,
  recipient_role user_role,   -- role-fanout when no specific user
  channel        notification_channel not null default 'in_app',

  title          text not null,
  body           text not null,
  payload        jsonb not null default '{}'::jsonb,

  gate_entry_id  uuid references gate_entries(id) on delete restrict,
  exception_id   uuid references exceptions(id) on delete restrict,

  created_at     timestamptz not null default now(),
  read_at        timestamptz,
  sent_at        timestamptz,
  send_error     text,

  constraint notifications_has_target
    check (num_nonnulls(recipient_id, recipient_role) >= 1)
);

create index notifications_inbox_idx on notifications(recipient_id, created_at desc)
  where read_at is null;
create index notifications_role_inbox_idx on notifications(recipient_role, created_at desc)
  where read_at is null;
create index notifications_pending_email_idx on notifications(created_at)
  where channel = 'email' and sent_at is null;

-- ===========================================================================
-- PHASE 2-4 PLACEHOLDERS
-- Created now so RLS, audit and FKs stay consistent. No API surface yet.
-- ===========================================================================

create table putaways (
  id            uuid primary key default gen_random_uuid(),
  box_id        uuid not null references boxes(id) on delete restrict,
  location_id   uuid not null references locations(id) on delete restrict,
  purchase_order_line_id uuid not null references purchase_order_lines(id),
  units         int not null check (units > 0),
  disposition   unit_disposition not null default 'stock',
  moved_by      uuid not null references profiles(id),
  moved_at      timestamptz not null default now()
);

create table invoices (
  id            uuid primary key default gen_random_uuid(),
  invoice_number text not null unique,
  purchase_order_line_id uuid references purchase_order_lines(id),
  sku           text not null,
  units         int not null check (units > 0),
  is_open       boolean not null default true,
  created_at    timestamptz not null default now()
);

create table invoice_verifications (
  id            uuid primary key default gen_random_uuid(),
  invoice_id    uuid not null references invoices(id) on delete restrict,
  verified_by   uuid not null references profiles(id),   -- matcher badge scan
  verified_at   timestamptz not null default now(),
  unique (invoice_id)
);

create table packing_records (
  id            uuid primary key default gen_random_uuid(),
  invoice_id    uuid not null references invoices(id) on delete restrict,
  packed_by     uuid not null references profiles(id),   -- packer badge scan
  packed_at     timestamptz not null default now(),
  carton_code   text,
  unique (invoice_id)
);

create table batches (
  id            uuid primary key default gen_random_uuid(),
  batch_code    text not null unique,
  released_by   uuid references profiles(id),
  released_at   timestamptz,
  created_at    timestamptz not null default now()
);
