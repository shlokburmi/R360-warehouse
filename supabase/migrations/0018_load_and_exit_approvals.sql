-- 0018_load_and_exit_approvals.sql
--
-- Two approval gates on the outbound side, mirroring the one at the gate on the
-- way in. Both were described as part of the process and neither existed:
--
--   1. When packing is finished, the guard counts the cartons and Ops decides.
--      Nothing is released to the pickup area until that decision is recorded.
--   2. When the loaded vehicle has been verified, Ops decides again before the
--      gate opens.
--
-- The shape is deliberately the same as CONTROL POINT 1: the person who counts
-- is not the person who approves, the timer escalates the notification and never
-- the decision, and nothing auto-approves. A count that approves itself is not a
-- check, which is the whole reason the inbound gate works.

-- ===========================================================================
-- 1. THE GUARD'S CARTON COUNT, AND OPS'S DECISION ON IT
-- ===========================================================================

create table batch_load_approvals (
  id              uuid primary key default gen_random_uuid(),
  batch_id        uuid not null references batches(id) on delete restrict,

  -- What the guard physically counted on the floor, which is the number worth
  -- recording precisely because it may disagree with the system's.
  counted_cartons int not null check (counted_cartons >= 0),
  counted_by      uuid not null references profiles(id),
  counted_at      timestamptz not null default now(),

  -- What the batch says it should be, captured at count time. Stored rather
  -- than recomputed: the point of the record is what the two numbers were when
  -- the human looked, and a later carton movement would silently rewrite that.
  expected_cartons int not null check (expected_cartons >= 0),

  status          load_approval_status not null default 'pending',
  decided_by      uuid references profiles(id),
  decided_at      timestamptz,
  note            text,

  -- PRD §7: a re-count is a new row superseding the old one, never an edit.
  recounts_id     uuid references batch_load_approvals(id),
  is_current      boolean not null default true,

  created_at      timestamptz not null default now(),

  constraint load_approval_decided_together
    check (num_nonnulls(decided_by, decided_at) <> 1),

  -- A rejection has to say why. "Rejected" with no reason is an argument
  -- waiting to happen on a loading bay at 7pm.
  constraint load_approval_reject_has_note
    check (status <> 'rejected' or length(btrim(coalesce(note, ''))) > 0)
);

create unique index batch_load_approvals_current_idx
  on batch_load_approvals (batch_id) where is_current;

create index batch_load_approvals_pending_idx
  on batch_load_approvals (counted_at) where is_current and status = 'pending';

create or replace function fn_load_approval_guard()
returns trigger
language plpgsql
as $$
declare
  v_status batch_status;
  v_code   text;
  v_actual int;
begin
  select b.status, b.batch_code into v_status, v_code
    from batches b where b.id = new.batch_id;

  if v_status is null then
    raise exception 'That batch does not exist.' using errcode = 'check_violation';
  end if;

  -- Counting a batch that has not finished its out-scan would produce a number
  -- that disagrees with the system for a completely uninteresting reason, and
  -- train everyone to approve mismatches.
  if v_status <> 'complete' then
    raise exception
      'Batch % is not ready to count — it is %.', v_code, v_status
      using errcode = 'check_violation',
            hint = 'Finish the out-scan first (CONTROL POINT 6).';
  end if;

  select count(*)::int into v_actual
    from packing_records where batch_id = new.batch_id;

  if tg_op = 'INSERT' then
    new.expected_cartons := v_actual;

    if new.status <> 'pending' then
      raise exception 'A carton count starts as pending and is decided by Ops.'
        using errcode = 'check_violation';
    end if;
  end if;

  return new;
end;
$$;

create trigger trg_load_approval_guard
  before insert on batch_load_approvals
  for each row execute function fn_load_approval_guard();

-- The decision itself: who may make it, and against whom.
create or replace function fn_load_decision_guard()
returns trigger
language plpgsql
as $$
declare
  v_approver_role user_role;
begin
  if new.status = old.status then
    return new;
  end if;

  if old.status <> 'pending' then
    raise exception 'This carton count has already been decided (%).', old.status
      using errcode = 'check_violation';
  end if;

  if new.decided_by is null then
    raise exception 'A decision requires a named approver.'
      using errcode = 'check_violation';
  end if;

  -- The same separation as CONTROL POINT 1. A guard who could approve their own
  -- count would make the count a formality, which is exactly what it must not
  -- be — it is the only independent check that what was packed is what is
  -- physically on the bay.
  if new.decided_by = old.counted_by then
    raise exception
      'The person who counted the cartons cannot approve their own count.'
      using errcode = 'check_violation',
            hint = 'Ops must approve it.';
  end if;

  select role into v_approver_role from profiles where id = new.decided_by;

  if v_approver_role is null or v_approver_role not in ('ops_manager', 'admin') then
    raise exception
      'Only an Ops Manager or Admin may decide a carton count. Got role: %',
      coalesce(v_approver_role::text, 'unknown')
      using errcode = 'check_violation';
  end if;

  if new.decided_at is null then
    new.decided_at := now();
  end if;

  return new;
end;
$$;

create trigger trg_load_decision_guard
  before update on batch_load_approvals
  for each row execute function fn_load_decision_guard();

create or replace function fn_load_approval_supersede()
returns trigger
language plpgsql
as $$
begin
  update batch_load_approvals
     set is_current = false
   where batch_id = new.batch_id and id <> new.id and is_current;
  return new;
end;
$$;

-- BEFORE insert, not AFTER. A partial unique index is enforced as the row goes
-- in, so an AFTER trigger runs too late to have cleared the previous current
-- row — the insert collides with an index the trigger was about to make room in.
create trigger trg_load_approval_supersede
  before insert on batch_load_approvals
  for each row execute function fn_load_approval_supersede();

-- ===========================================================================
-- RELEASE IS BLOCKED UNTIL THAT DECISION EXISTS
--
-- Replaces the 0009 guard. The CONTROL POINT 6 message still runs first, then
-- the new check, then the generic transition table — the ordering lesson from
-- DECISIONS.md Part D: the check that knows *why* must fire before the one that
-- only knows *that*.
-- ===========================================================================

create or replace function fn_batch_release_guard()
returns trigger
language plpgsql
as $$
declare
  v_assigned int;
  v_scanned  int;
  v_approval load_approval_status;
  v_counted  int;
  v_expected int;
begin
  if new.status = 'released' and old.status <> 'complete' then
    raise exception
      'Batch % cannot be released until every carton is out-scanned (CONTROL POINT 6).',
      new.batch_code
      using errcode = 'check_violation',
            hint = 'Complete the out-scan first.';
  end if;

  -- The new gate. Deliberately before the transition table so the operator is
  -- told what is missing rather than that the move was illegal.
  if new.status = 'released' and old.status = 'complete' then
    select la.status, la.counted_cartons, la.expected_cartons
      into v_approval, v_counted, v_expected
      from batch_load_approvals la
     where la.batch_id = new.id and la.is_current;

    if v_approval is null then
      raise exception
        'Batch % has not been counted by a guard yet.', new.batch_code
        using errcode = 'check_violation',
              hint = 'The guard counts the cartons, then Ops approves the count.';
    end if;

    if v_approval = 'pending' then
      raise exception
        'Batch %: the guard counted % cartons and Ops has not decided yet.',
        new.batch_code, v_counted
        using errcode = 'check_violation',
              hint = 'Ops must approve the count before anything is loaded.';
    end if;

    if v_approval = 'rejected' then
      raise exception
        'Batch %: Ops rejected the carton count (% counted, % expected).',
        new.batch_code, v_counted, v_expected
        using errcode = 'check_violation',
              hint = 'Recount the cartons and submit again.';
    end if;
  end if;

  if not fn_batch_transition_ok(old.status, new.status) then
    raise exception 'Illegal batch transition: % -> %', old.status, new.status
      using errcode = 'check_violation';
  end if;

  if new.status = 'complete' and old.status <> 'complete' then
    select count(*), count(out_scanned_at) into v_assigned, v_scanned
      from packing_records where batch_id = new.id;

    if v_assigned = 0 then
      raise exception 'Batch % has no cartons assigned.', new.batch_code
        using errcode = 'check_violation';
    end if;

    if v_scanned <> v_assigned then
      raise exception
        'Batch %: % of % cartons out-scanned (CONTROL POINT 6).',
        new.batch_code, v_scanned, v_assigned
        using errcode = 'check_violation',
              hint = 'Scan the remaining cartons before completing the batch.';
    end if;

    if v_assigned <> new.planned_carton_count then
      raise exception
        'Batch %: % cartons assigned but % were planned (CONTROL POINT 6).',
        new.batch_code, v_assigned, new.planned_carton_count
        using errcode = 'check_violation';
    end if;
  end if;

  if new.status = 'released' then
    -- old.status is necessarily 'complete' here; guarded at the top.
    if new.released_by is null then
      raise exception 'Batch release requires a named releasing user (CONTROL POINT 6).'
        using errcode = 'check_violation';
    end if;

    if new.released_at is null then
      new.released_at := now();
    end if;
  end if;

  return new;
end;
$$;

-- ===========================================================================
-- 2. OPS APPROVES THE VEHICLE LEAVING
-- ===========================================================================

alter table pickups
  add column exit_requested_by  uuid references profiles(id),
  add column exit_requested_at  timestamptz,
  add column exit_approved_by   uuid references profiles(id),
  add column exit_approved_at   timestamptz,
  add column exit_rejected_note text;

alter table pickups add constraint pickups_exit_request_together
  check (num_nonnulls(exit_requested_by, exit_requested_at) <> 1);

alter table pickups add constraint pickups_exit_approval_together
  check (num_nonnulls(exit_approved_by, exit_approved_at) <> 1);

-- 'verified' no longer leads straight to 'departed'. The guard requests exit,
-- Ops approves, and only then does the gate open.
create or replace function fn_pickup_transition_ok(old_status pickup_status,
                                                   new_status pickup_status)
returns boolean language sql immutable as $$
  select (old_status, new_status) in (
    ('registered',   'verifying'),
    ('registered',   'cancelled'),
    ('verifying',    'verified'),
    ('verifying',    'cancelled'),
    ('verified',     'exit_pending'),
    ('verified',     'cancelled'),
    ('exit_pending', 'departed'),
    -- Back to 'verified' on a rejection, so the guard can re-request after
    -- whatever Ops asked for has been dealt with.
    ('exit_pending', 'verified'),
    ('exit_pending', 'cancelled')
  ) or old_status = new_status;
$$;

create or replace function fn_pickup_guard()
returns trigger
language plpgsql
as $$
declare
  v_released int;
  v_scanned  int;
  v_batch    text;
  v_approver_role user_role;
begin
  -- Specific messages before the generic transition table (DECISIONS.md Part D).
  -- Two distinct failures used to collapse into one unhelpful message here.
  if new.status = 'departed' and old.status = 'verifying' then
    raise exception
      'Vehicle cannot leave until every released carton is verified present '
      '(CONTROL POINT 7).'
      using errcode = 'check_violation',
            hint = 'Scan the remaining cartons onto the vehicle first.';
  end if;

  if new.status = 'departed' and old.status = 'verified' then
    raise exception
      'Vehicle % has not been approved to leave yet.', new.vehicle_number
      using errcode = 'check_violation',
            hint = 'Request exit approval, then Ops releases the gate.';
  end if;

  if not fn_pickup_transition_ok(old.status, new.status) then
    raise exception 'Illegal pickup transition: % -> %', old.status, new.status
      using errcode = 'check_violation';
  end if;

  if new.status = 'verified' and old.status = 'verifying' then
    select b.batch_code, count(pr.id), count(pr.exit_scanned_at)
      into v_batch, v_released, v_scanned
      from batches b
      left join packing_records pr on pr.batch_id = b.id
     where b.id = new.batch_id
     group by b.batch_code;

    if coalesce(v_released, 0) = 0 then
      raise exception 'Batch % has no cartons.', coalesce(v_batch, '?')
        using errcode = 'check_violation';
    end if;

    -- The comparison that matters: released against physically present.
    if v_scanned <> v_released then
      raise exception
        'Pickup %: % of % released cartons verified (CONTROL POINT 7).',
        new.pickup_code, v_scanned, v_released
        using errcode = 'check_violation',
              hint = 'The truck cannot leave until every carton is accounted for.';
    end if;

    if new.verified_by is null then
      raise exception 'Verification requires a named user (CONTROL POINT 7).'
        using errcode = 'check_violation';
    end if;

    if new.verified_at is null then
      new.verified_at := now();
    end if;
  end if;

  -- An approval belongs to one request. Going back to 'verified' — a rejection,
  -- or a re-request — drops any approval still attached, because otherwise a
  -- withdrawn consent could be spent on the next round.
  if new.status = 'verified' and old.status = 'exit_pending' then
    new.exit_approved_by := null;
    new.exit_approved_at := null;
    new.exit_requested_by := null;
    new.exit_requested_at := null;
  end if;

  if new.status = 'exit_pending' and old.status = 'verified' then
    new.exit_approved_by := null;
    new.exit_approved_at := null;

    if new.exit_requested_by is null then
      raise exception 'Requesting exit approval requires a named user.'
        using errcode = 'check_violation';
    end if;

    if new.exit_requested_at is null then
      new.exit_requested_at := now();
    end if;
  end if;

  if new.status = 'departed' then
    if new.exit_approved_by is null then
      raise exception 'The gate cannot open without a recorded Ops approval.'
        using errcode = 'check_violation',
              hint = 'Ops must approve the exit.';
    end if;

    -- The same two-person rule as the inbound gate. A guard who could approve
    -- their own exit request would make the second gate decorative.
    if new.exit_approved_by = new.exit_requested_by then
      raise exception
        'The guard who requested exit cannot also approve it.'
        using errcode = 'check_violation',
              hint = 'Ops approves the exit.';
    end if;

    -- And the approver has to be someone entitled to approve. Without this the
    -- rule above is only "name someone else" — a guard could nominate any
    -- colleague and open the gate, which is CONTROL POINT 1's mistake made
    -- twice. Mirrors fn_gate_guard in 0004.
    select role into v_approver_role from profiles where id = new.exit_approved_by;

    if v_approver_role is null or v_approver_role not in ('ops_manager', 'admin') then
      raise exception
        'Only an Ops Manager or Admin may approve a vehicle leaving. Got role: %',
        coalesce(v_approver_role::text, 'unknown')
        using errcode = 'check_violation';
    end if;

    if new.released_by is null then
      raise exception 'Releasing the vehicle requires a named user (CONTROL POINT 7).'
        using errcode = 'check_violation';
    end if;

    if new.time_out is null then
      new.time_out := now();
    end if;
  end if;

  return new;
end;
$$;

-- The pickup view has to surface the new state, or the guard's screen cannot
-- tell "verified, waiting on Ops" from "verified, go". Rebuilt rather than
-- patched because a view cannot have columns added in place.
--
-- security_invoker, like every view since 0010.
drop view if exists v_pickup_status;

create view v_pickup_status
with (security_invoker = true)
as
select
  p.id                                          as pickup_id,
  p.pickup_code,
  p.status::text                                as status,
  p.vehicle_number,
  p.courier_name,
  p.transporter_name,
  p.batch_id,
  b.batch_code,
  count(pr.id)::int                             as released_cartons,
  count(pr.exit_scanned_at)::int                as verified_cartons,
  (count(pr.id) - count(pr.exit_scanned_at))::int as remaining_cartons,
  p.registered_at,
  rb.full_name                                  as registered_by_name,
  p.verified_at,
  vb.full_name                                  as verified_by_name,
  p.time_in,
  p.time_out,
  lb.full_name                                  as released_by_name,
  p.exit_requested_at,
  xr.full_name                                  as exit_requested_by_name,
  p.exit_approved_at,
  xa.full_name                                  as exit_approved_by_name,
  p.exit_rejected_note,
  extract(epoch from (now() - p.exit_requested_at))::int as exit_waiting_seconds
from pickups p
join batches b                on b.id = p.batch_id
left join packing_records pr  on pr.batch_id = b.id
left join profiles rb         on rb.id = p.registered_by
left join profiles vb         on vb.id = p.verified_by
left join profiles lb         on lb.id = p.released_by
left join profiles xr         on xr.id = p.exit_requested_by
left join profiles xa         on xa.id = p.exit_approved_by
group by p.id, p.pickup_code, p.status, p.vehicle_number, p.courier_name,
         p.transporter_name, p.batch_id, b.batch_code, p.registered_at,
         rb.full_name, p.verified_at, vb.full_name, p.time_in, p.time_out,
         lb.full_name, p.exit_requested_at, xr.full_name, p.exit_approved_at,
         xa.full_name, p.exit_rejected_note;

grant select on v_pickup_status to authenticated;

-- ===========================================================================
-- GRANTS AND RLS
-- ===========================================================================

alter table batch_load_approvals enable row level security;
alter table batch_load_approvals force row level security;
revoke all on batch_load_approvals from anon, authenticated;
grant select, insert, update on batch_load_approvals to authenticated;
revoke delete on batch_load_approvals from authenticated;

create policy load_approvals_read on batch_load_approvals
  for select to authenticated using (true);

-- The guard counts. Ops may also count when covering the bay, which is the same
-- allowance the inbound count has.
create policy load_approvals_insert on batch_load_approvals
  for insert to authenticated
  with check (
    has_role('security_guard', 'ops_manager', 'admin')
    and counted_by = auth.uid()
  );

-- Only Ops decides, and the supersede trigger needs to flip is_current on rows
-- it does not own — hence the two-branch policy rather than one.
create policy load_approvals_update on batch_load_approvals
  for update to authenticated
  using (has_role('security_guard', 'ops_manager', 'admin'))
  with check (has_role('security_guard', 'ops_manager', 'admin'));

create trigger trg_load_approvals_audit
  after insert or update on batch_load_approvals
  for each row execute function fn_audit();

create trigger trg_load_approvals_no_delete
  before delete on batch_load_approvals
  for each row execute function fn_no_delete();

comment on table batch_load_approvals is
  'Guard''s physical carton count on a finished batch and the Ops decision on it. '
  'A batch cannot be released to the pickup area without an approved count.';
