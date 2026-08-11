-- 0003_audit_and_immutability.sql
-- PRD §7 (ACID, no deletions) and the "100% audit trail" success metric.
--
-- Two guarantees are set up here:
--   1. Nothing in a business table can be DELETEd. Corrections are reversing
--      entries. Enforced by trigger, so it survives a mistaken GRANT.
--   2. Every INSERT/UPDATE is captured with full before/after state and the
--      acting user, in a table nobody can write to directly.

-- ===========================================================================
-- AUDIT LOG
-- ===========================================================================

create table audit_log (
  id           bigserial primary key,
  table_name   text not null,
  record_id    uuid,
  action       text not null check (action in ('INSERT', 'UPDATE')),

  actor_id     uuid,          -- profiles.id; null only for system/migration writes
  actor_role   text,
  actor_source text not null default 'api',  -- api | worker | migration

  before_data  jsonb,
  after_data   jsonb,
  changed_keys text[],        -- convenience for reports; derived from the two above

  occurred_at  timestamptz not null default now()
);

create index audit_log_record_idx on audit_log(table_name, record_id, occurred_at desc);
create index audit_log_actor_idx on audit_log(actor_id, occurred_at desc);
create index audit_log_time_idx on audit_log(occurred_at desc);

-- Resolve the acting user. auth.uid() is set for anything coming through the
-- API (FastAPI sets request.jwt.claims per transaction); background workers set
-- app.actor_id explicitly instead.
create or replace function current_actor_id()
returns uuid
language plpgsql
stable
as $$
declare
  v uuid;
begin
  begin
    v := auth.uid();
  exception when others then
    v := null;
  end;

  if v is null then
    begin
      v := nullif(current_setting('app.actor_id', true), '')::uuid;
    exception when others then
      v := null;
    end;
  end if;

  return v;
end;
$$;

create or replace function current_actor_role()
returns user_role
language sql
stable
security definer
set search_path = public
as $$
  select role from profiles where id = current_actor_id();
$$;

-- SECURITY DEFINER: the audit row must be written even though `authenticated`
-- has no INSERT grant on audit_log. That is the point — the trail cannot be
-- forged or suppressed by the same session it is recording.
create or replace function fn_audit()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
  v_before jsonb;
  v_after  jsonb;
  v_keys   text[];
  v_id     uuid;
begin
  if tg_op = 'UPDATE' then
    v_before := to_jsonb(old);
    v_after  := to_jsonb(new);
    select array_agg(key order by key)
      into v_keys
      from jsonb_each(v_after)
     where value is distinct from (v_before -> key);

    -- Nothing actually changed (a no-op UPDATE). Don't pollute the trail.
    if v_keys is null then
      return new;
    end if;
  else
    v_after := to_jsonb(new);
  end if;

  begin
    v_id := (v_after ->> 'id')::uuid;
  exception when others then
    v_id := null;
  end;

  insert into audit_log (
    table_name, record_id, action, actor_id, actor_role, actor_source,
    before_data, after_data, changed_keys
  ) values (
    tg_table_name, v_id, tg_op, current_actor_id(), current_actor_role()::text,
    coalesce(nullif(current_setting('app.actor_source', true), ''), 'api'),
    v_before, v_after, v_keys
  );

  return new;
end;
$$;

-- ===========================================================================
-- NO DELETIONS
-- ===========================================================================

create or replace function fn_no_delete()
returns trigger
language plpgsql
as $$
begin
  raise exception
    'Deletion is not permitted on %. Create a reversing entry instead (PRD §7).',
    tg_table_name
    using errcode = 'restrict_violation',
          hint = 'Set a cancelled/withdrawn status, or insert a row with reverses_id set.';
end;
$$;

-- ===========================================================================
-- updated_at maintenance
-- ===========================================================================

create or replace function fn_touch_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

-- ===========================================================================
-- Attach the three triggers to every business table
-- ===========================================================================

do $$
declare
  t text;
  audited text[] := array[
    'profiles', 'vendors', 'locations',
    'purchase_orders', 'purchase_order_lines',
    'visitors', 'gate_entries', 'gate_entry_persons',
    'sticker_sheets', 'stickers', 'boxes', 'damage_photos',
    'scan_events', 'inbound_reconciliations',
    'exceptions', 'notifications',
    'putaways', 'invoices', 'invoice_verifications', 'packing_records', 'batches'
  ];
  has_updated_at boolean;
begin
  foreach t in array audited loop
    execute format(
      'create trigger trg_%1$s_audit after insert or update on %1$I
         for each row execute function fn_audit()', t);

    execute format(
      'create trigger trg_%1$s_no_delete before delete on %1$I
         for each row execute function fn_no_delete()', t);

    select exists (
      select 1 from information_schema.columns
       where table_schema = 'public' and table_name = t and column_name = 'updated_at'
    ) into has_updated_at;

    if has_updated_at then
      execute format(
        'create trigger trg_%1$s_touch before update on %1$I
           for each row execute function fn_touch_updated_at()', t);
    end if;
  end loop;
end;
$$;

-- scan_events is append-only in the strongest sense: a scan that happened
-- cannot be edited into a scan that didn't. Corrections are new rows.
create or replace function fn_no_update()
returns trigger
language plpgsql
as $$
begin
  raise exception '% is append-only; rows cannot be modified after insert.', tg_table_name
    using errcode = 'restrict_violation';
end;
$$;

create trigger trg_scan_events_no_update before update on scan_events
  for each row execute function fn_no_update();

create trigger trg_audit_log_no_update before update on audit_log
  for each row execute function fn_no_update();
create trigger trg_audit_log_no_delete before delete on audit_log
  for each row execute function fn_no_delete();

-- ===========================================================================
-- HUMAN-READABLE CODES  (GE-20260810-0007, EX-20260810-0003, ...)
-- ===========================================================================

create table code_counters (
  prefix     text not null,
  for_date   date not null,
  last_value int  not null default 0,
  primary key (prefix, for_date)
);

-- SECURITY DEFINER so ordinary roles can mint a code without write access to
-- the counter table. The ON CONFLICT ... RETURNING makes it safe under
-- concurrency: two guards submitting at once get 0007 and 0008, never a clash.
create or replace function next_code(p_prefix text)
returns text
language plpgsql
security definer
set search_path = public
as $$
declare
  v_date date := (now() at time zone 'Asia/Kolkata')::date;
  v_val  int;
begin
  insert into code_counters (prefix, for_date, last_value)
  values (p_prefix, v_date, 1)
  on conflict (prefix, for_date)
  do update set last_value = code_counters.last_value + 1
  returning last_value into v_val;

  return format('%s-%s-%s', p_prefix, to_char(v_date, 'YYYYMMDD'), lpad(v_val::text, 4, '0'));
end;
$$;

create or replace function fn_set_entry_code()
returns trigger language plpgsql as $$
begin
  if new.entry_code is null or new.entry_code = '' then
    new.entry_code := next_code('GE');
  end if;
  return new;
end;
$$;

create trigger trg_gate_entries_code before insert on gate_entries
  for each row execute function fn_set_entry_code();

create or replace function fn_set_exception_code()
returns trigger language plpgsql as $$
begin
  if new.exception_code is null or new.exception_code = '' then
    new.exception_code := next_code('EX');
  end if;
  return new;
end;
$$;

create trigger trg_exceptions_code before insert on exceptions
  for each row execute function fn_set_exception_code();

-- ===========================================================================
-- PROFILE PROVISIONING
-- A profile is created for every auth user. Role comes from signup metadata and
-- defaults to the least-privileged role rather than the most useful one.
-- ===========================================================================

create or replace function fn_handle_new_auth_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into profiles (id, full_name, role, employee_code, mobile)
  values (
    new.id,
    coalesce(nullif(new.raw_user_meta_data ->> 'full_name', ''), split_part(new.email, '@', 1)),
    coalesce((new.raw_user_meta_data ->> 'role')::user_role, 'security_guard'),
    nullif(new.raw_user_meta_data ->> 'employee_code', ''),
    nullif(new.raw_user_meta_data ->> 'mobile', '')
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

create trigger trg_auth_user_created
  after insert on auth.users
  for each row execute function fn_handle_new_auth_user();

-- Random, non-guessable attribution badge token (DECISIONS.md §1).
create or replace function generate_badge_code()
returns text language sql volatile as $$
  select 'BDG-' || encode(gen_random_bytes(8), 'hex');
$$;
