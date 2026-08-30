-- 0028_cancel_inflight_gate_entry.sql
--
-- Cancelling a gate entry was only legal before the vehicle was admitted
-- (draft/pending_approval/approved -> cancelled, 0004_control_points.sql).
-- Real use surfaced a truck stuck mid-flow with no way out — e.g. a guard
-- submitted with no PO linked and Ops Manager has no way to attach one after
-- the fact (0025), so the entry can never reach CP2. Ops Manager needs to be
-- able to abandon a truck's process at any in-progress stage, not just
-- pre-admission — nothing here lets anyone skip a control point forward,
-- only exit sideways into a terminal state.
--
-- Left out of this: offloaded/reconciled/departed. By 'offloaded' the goods
-- have physically arrived and CP3 has already passed for every box —
-- "cancelling" that is a receiving discrepancy, not an abandoned truck, and
-- belongs in the exceptions flow instead.

create or replace function fn_gate_entry_transition_ok(old_status gate_entry_status,
                                                       new_status gate_entry_status)
returns boolean language sql immutable as $$
  select (old_status, new_status) in (
    ('draft',            'pending_approval'),
    ('draft',            'cancelled'),
    ('pending_approval', 'approved'),
    ('pending_approval', 'rejected'),
    ('pending_approval', 'cancelled'),
    ('approved',         'inside'),
    ('approved',         'cancelled'),
    ('inside',           'counting'),
    ('inside',           'cancelled'),
    ('counting',         'box_verified'),
    ('counting',         'cancelled'),
    ('box_verified',     'offloading'),
    ('box_verified',     'cancelled'),
    ('offloading',       'offloaded'),
    ('offloading',       'cancelled'),
    ('offloaded',        'reconciled'),
    ('reconciled',       'departed')
  ) or old_status = new_status;
$$;
