-- 0017_packing_assignment.sql
--
-- The floor step that was missing: who is going to pack this invoice.
--
-- A lead scans the assignee's badge card and the invoice becomes hers. Nothing
-- about CONTROL POINT 5 relaxes to allow that, which is worth being explicit
-- about because it looks like it should.
--
-- The invariant in DECISIONS.md §CC2 is that no operation *tells* anyone the
-- code of a badge sitting in someone's pocket. Reading a card that is physically
-- in front of you is the intended use — `resolve_badge_holder(code)` has always
-- taken a scanned code and returned its holder, for any signed-in user, because
-- that is exactly what a station tablet does. Physical custody of the badge is
-- the control, and it still is here.
--
-- What that leaves intact: the verifier and the packer must still be two
-- different people, and a single person can only defeat that by holding two
-- badges — which was equally true before this migration existed.

create table packing_assignments (
  id            uuid primary key default gen_random_uuid(),
  invoice_id    uuid not null references invoices(id) on delete restrict,

  assigned_to   uuid not null references profiles(id),
  assigned_by   uuid not null references profiles(id),
  assigned_at   timestamptz not null default now(),

  -- PRD §7: a reassignment is a new row that supersedes the old one, never an
  -- update in place. "This box moved from Kavitha to Anitha at 14:20, by
  -- Lakshmi" is a question the trail has to be able to answer.
  reassigns_id  uuid references packing_assignments(id),
  is_current    boolean not null default true,
  note          text,

  created_at    timestamptz not null default now()
);

-- One live assignment per invoice. A partial unique index rather than a plain
-- one, so superseded rows can accumulate while the current one stays singular.
create unique index packing_assignments_current_idx
  on packing_assignments (invoice_id) where is_current;

create index packing_assignments_assignee_idx
  on packing_assignments (assigned_to, assigned_at desc) where is_current;

-- ===========================================================================
-- GUARD
-- Everything here is refused at the database rather than in the service, for
-- the reason in DECISIONS.md §B3: a rule that lives only in a FastAPI handler
-- is one hotfix away from not being a rule.
-- ===========================================================================

create or replace function fn_packing_assignment_guard()
returns trigger
language plpgsql
as $$
declare
  v_open      boolean;
  v_number    text;
  v_verifier  uuid;
  v_role      user_role;
  v_active    boolean;
  v_badge     boolean;
  v_name      text;
begin
  select i.is_open, i.invoice_number into v_open, v_number
    from invoices i where i.id = new.invoice_id;

  if v_open is null then
    raise exception 'That invoice does not exist.' using errcode = 'check_violation';
  end if;

  if not v_open then
    raise exception 'Invoice % is closed and cannot be assigned.', v_number
      using errcode = 'check_violation',
            hint = 'Its batch has already been released.';
  end if;

  -- `is_open` does not cover this: an invoice stays open until its batch is
  -- released, so a carton that has already been packed is still open. Without
  -- this check the assignment and the pack record could be made to name
  -- different people, which is the one thing this table exists to prevent.
  if exists (select 1 from packing_records pr where pr.invoice_id = new.invoice_id) then
    raise exception 'Invoice % has already been packed.', v_number
      using errcode = 'check_violation',
            hint = 'Reversing a pack is an Ops correction, not a reassignment.';
  end if;

  -- Assignment presupposes matching. The small box and its invoice only reach a
  -- packing bench because Admin put them together, so an invoice with no
  -- verification has not physically been matched to goods yet.
  select v.verified_by into v_verifier
    from invoice_verifications v where v.invoice_id = new.invoice_id;

  if v_verifier is null then
    raise exception
      'Invoice % has not been matched yet.', v_number
      using errcode = 'check_violation',
            hint = 'Admin must scan the invoice and their badge first.';
  end if;

  select p.role, p.is_active, p.badge_active, p.full_name
    into v_role, v_active, v_badge, v_name
    from profiles p where p.id = new.assigned_to;

  if v_role is null then
    raise exception 'That badge does not belong to anyone.' using errcode = 'check_violation';
  end if;

  if not v_active then
    raise exception '%''s account is deactivated.', v_name
      using errcode = 'check_violation';
  end if;

  if not v_badge then
    raise exception '%''s badge has been withdrawn.', v_name
      using errcode = 'check_violation',
            hint = 'Ask an Admin to reissue it.';
  end if;

  -- Only the roles that actually pack. Assigning to a guard would produce a
  -- record nobody can act on and an attribution for work she does not do.
  if v_role not in ('packer', 'admin') then
    raise exception '% is not a packer.', v_name
      using errcode = 'check_violation',
            hint = 'Packing is done by the packing team, or by an Admin covering the bench.';
  end if;

  -- CONTROL POINT 5, checked at assignment instead of only at packing.
  -- The rule is unchanged; catching it here means the refusal lands while the
  -- lead is still holding the badge, rather than after the box is packed.
  if v_verifier = new.assigned_to then
    raise exception
      '% matched invoice % and cannot also pack it (CONTROL POINT 5).', v_name, v_number
      using errcode = 'check_violation',
            hint = 'Packing must be a second person. Assign it to someone else.';
  end if;

  return new;
end;
$$;

create trigger trg_packing_assignment_guard
  before insert on packing_assignments
  for each row execute function fn_packing_assignment_guard();

-- Inserting a new assignment supersedes the previous one automatically, so the
-- caller cannot leave two live rows behind by forgetting a step.
create or replace function fn_packing_assignment_supersede()
returns trigger
language plpgsql
as $$
begin
  update packing_assignments
     set is_current = false
   where invoice_id = new.invoice_id
     and id <> new.id
     and is_current;
  return new;
end;
$$;

-- BEFORE insert, not AFTER. A partial unique index is enforced as the row goes
-- in, so an AFTER trigger runs too late to have cleared the previous current
-- row — the insert collides with an index the trigger was about to make room in.
create trigger trg_packing_assignment_supersede
  before insert on packing_assignments
  for each row execute function fn_packing_assignment_supersede();

-- ===========================================================================
-- THE PACK MUST GO TO THE PERSON IT WAS ASSIGNED TO
--
-- Without this, assignment would be advisory: a lead could scan Kavitha's badge
-- and the carton could still be recorded against Anitha. Then "assigned to" and
-- "packed by" could disagree, and the assignment record would be worse than
-- useless — it would look like evidence while not being any.
-- ===========================================================================

create or replace function fn_packing_matches_assignment()
returns trigger
language plpgsql
as $$
declare
  v_assignee uuid;
  v_name     text;
  v_packer   text;
begin
  select a.assigned_to into v_assignee
    from packing_assignments a
   where a.invoice_id = new.invoice_id and a.is_current;

  -- No assignment is still allowed: a packer who picks up a verified invoice
  -- and scans her own badge is the original flow and remains valid. The check
  -- only bites once someone has been assigned.
  if v_assignee is null or v_assignee = new.packed_by then
    return new;
  end if;

  select full_name into v_name from profiles where id = v_assignee;
  select full_name into v_packer from profiles where id = new.packed_by;

  raise exception 'This invoice is assigned to %, not %.', v_name, v_packer
    using errcode = 'check_violation',
          hint = 'Reassign it first if someone else is packing it.';
end;
$$;

create trigger trg_packing_matches_assignment
  before insert on packing_records
  for each row execute function fn_packing_matches_assignment();

-- ===========================================================================
-- GRANTS AND RLS
-- ===========================================================================

alter table packing_assignments enable row level security;
alter table packing_assignments force row level security;
revoke all on packing_assignments from anon, authenticated;
grant select, insert, update on packing_assignments to authenticated;
revoke delete on packing_assignments from authenticated;

-- Readable by all staff: the packing bench, the matchers and Ops all need to
-- see who is packing what, and it is the same information that is written on a
-- whiteboard above the bench today.
create policy packing_assignments_read on packing_assignments
  for select to authenticated using (true);

-- Written by the people at the bench and by Ops covering it. Matchers included:
-- the matcher is usually the person handing the box over.
create policy packing_assignments_insert on packing_assignments
  for insert to authenticated
  with check (
    has_role('packer', 'admin')
    and assigned_by = auth.uid()
  );

-- Only the supersede trigger updates rows, and it runs as the caller, so the
-- policy has to permit it. Narrowed to is_current so a superseded row is
-- immutable thereafter.
create policy packing_assignments_update on packing_assignments
  for update to authenticated
  using (has_role('packer', 'admin'))
  with check (has_role('packer', 'admin'));

-- Audit + no-delete, same as every other business table.
create trigger trg_packing_assignments_audit
  after insert or update on packing_assignments
  for each row execute function fn_audit();

create trigger trg_packing_assignments_no_delete
  before delete on packing_assignments
  for each row execute function fn_no_delete();

comment on table packing_assignments is
  'Who is packing which invoice. Created by scanning the assignee''s badge card; '
  'physical custody of the badge is the control (see DECISIONS.md §CC2).';
