"""Admin dashboard (PRD §5.8) and reports (PRD §5.10).

The five reports in the PRD are all derived — nothing here maintains a counter
that could drift away from the ledger it is supposed to summarise.
"""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.deps import CurrentUser, get_current_user, get_db, require_ops_manager

router = APIRouter(tags=["reports"])


@router.get("/dashboard")
async def dashboard(
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Everything the Admin landing page needs, in one round trip.

    One query per tile would be four requests from a tablet on warehouse wifi;
    the whole point of this page is that it loads before someone gives up on it.
    """
    counters = (
        await conn.execute(
            text(
                """
                select
                  (select count(*) from gate_entries
                    where status = 'pending_approval')::int              as pending_approvals,
                  (select count(*) from gate_entries
                    where status = 'pending_approval' and sla_breached)::int as sla_breached,
                  (select count(*) from exceptions
                    where status in ('open', 'escalated'))::int           as open_exceptions,
                  (select count(*) from boxes where status = 'held')::int as held_boxes,
                  (select count(*) from gate_entries
                    where time_in::date = current_date)::int              as trucks_today,
                  (select count(*) from gate_entries
                    where status not in ('departed','rejected','cancelled')
                      and time_in is not null)::int                       as trucks_onsite,
                  (select count(*) from scan_events
                    where accepted and recorded_at::date = current_date)::int as scans_today,
                  (select count(*) from boxes b
                     join gate_entries ge on ge.id = b.gate_entry_id
                    where b.completed_at::date = current_date)::int       as boxes_closed_today
                """
            )
        )
    ).mappings().one()

    activity = await conn.execute(
        text(
            """
            select ge.id, ge.entry_code, ge.status::text as status, ge.vehicle_number,
                   v.name as vendor_name, ge.requested_at, ge.time_in,
                   ge.declared_box_count,
                   (select count(*) from boxes b
                     where b.gate_entry_id = ge.id and b.status = 'held')::int as held_boxes,
                   extract(epoch from (now() - ge.requested_at))::int as waiting_seconds
              from gate_entries ge
              join vendors v on v.id = ge.vendor_id
             where ge.created_at > now() - interval '2 days'
               and ge.status not in ('departed', 'rejected', 'cancelled')
             order by ge.requested_at desc nulls last
             limit 25
            """
        )
    )

    recent_exceptions = await conn.execute(
        text(
            """
            select e.id, e.exception_code, e.title, e.status::text as status,
                   e.exception_type::text as exception_type,
                   v.name as vendor_name, e.reported_at
              from exceptions e
              left join vendors v on v.id = e.vendor_id
             where e.status in ('open', 'escalated')
             order by e.reported_at desc
             limit 10
            """
        )
    )

    return {
        "counters": dict(counters),
        "activity": [dict(r) for r in activity.mappings()],
        "open_exceptions": [dict(r) for r in recent_exceptions.mappings()],
    }


@router.get("/reports/vendor-accuracy")
async def vendor_accuracy(
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_ops_manager),
):
    """Which vendors send what they said they would (PRD §5.10).

    Ordered worst-first: this report exists to start conversations with the
    vendors at the top of it.
    """
    rows = await conn.execute(
        text(
            """
            select *,
                   case when units_expected > 0
                        then round(100.0 * units_received / units_expected, 1)
                        else null end as fill_rate_pct
              from v_vendor_accuracy
             where deliveries > 0
             order by count_exceptions desc, fill_rate_pct asc nulls last
            """
        )
    )
    return [dict(r) for r in rows.mappings()]


@router.get("/reports/exception-log")
async def exception_log(
    from_date: Optional[date] = Query(default=None),
    to_date: Optional[date] = Query(default=None),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_ops_manager),
):
    rows = await conn.execute(
        text(
            """
            select e.exception_code, e.exception_type::text as exception_type,
                   e.status::text as status, e.title,
                   v.name as vendor_name, po.po_number, ge.entry_code,
                   rp.full_name as reported_by, e.reported_at,
                   e.resolution::text as resolution, e.resolution_note,
                   sp.full_name as resolved_by, e.resolved_at,
                   case when e.resolved_at is not null
                        then round(extract(epoch from (e.resolved_at - e.reported_at)) / 60.0, 1)
                        end as minutes_to_resolve
              from exceptions e
              left join vendors v on v.id = e.vendor_id
              left join purchase_orders po on po.id = e.purchase_order_id
              left join gate_entries ge on ge.id = e.gate_entry_id
              left join profiles rp on rp.id = e.reported_by
              left join profiles sp on sp.id = e.resolved_by
             where (cast(:from_date as date) is null or e.reported_at::date >= cast(:from_date as date))
               and (cast(:to_date as date) is null or e.reported_at::date <= cast(:to_date as date))
             order by e.reported_at desc
             limit 1000
            """
        ),
        {"from_date": from_date, "to_date": to_date},
    )
    return [dict(r) for r in rows.mappings()]


@router.get("/reports/gate-register")
async def gate_register(
    on_date: Optional[date] = Query(default=None),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_ops_manager),
):
    """Every person in and out, with timestamps — the statutory gate register."""
    rows = await conn.execute(
        text(
            """
            select ge.entry_code, ge.vehicle_number, v.name as vendor_name,
                   vi.full_name, vi.mobile, gep.visitor_role::text as visitor_role,
                   ge.time_in, ge.time_out,
                   ge.status::text as status,
                   rp.full_name as registered_by,
                   dp.full_name as approved_by, ge.decided_at as approved_at,
                   (gep.id_photo_path is not null) as has_id_photo
              from gate_entry_persons gep
              join gate_entries ge on ge.id = gep.gate_entry_id
              join visitors vi on vi.id = gep.visitor_id
              join vendors v on v.id = ge.vendor_id
              left join profiles rp on rp.id = ge.requested_by
              left join profiles dp on dp.id = ge.decided_by
             where (cast(:on_date as date) is null or coalesce(ge.time_in, ge.created_at)::date = cast(:on_date as date))
             order by coalesce(ge.time_in, ge.created_at) desc
             limit 1000
            """
        ),
        {"on_date": on_date},
    )
    return [dict(r) for r in rows.mappings()]


@router.get("/reports/outbound-register")
async def outbound_register(
    on_date: Optional[date] = Query(default=None),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_ops_manager),
):
    """Every collecting vehicle in and out, with who verified and released it.

    The outbound half of the statutory gate register. Kept separate from the
    inbound one because the columns that matter differ — a batch and a carton
    count rather than a vendor and a box count.
    """
    rows = await conn.execute(
        text(
            """
            select p.pickup_code, p.vehicle_number, p.courier_name,
                   b.batch_code,
                   vi.full_name, vi.mobile, pp.visitor_role::text as visitor_role,
                   ps.released_cartons, ps.verified_cartons,
                   p.time_in, p.time_out,
                   p.status::text as status,
                   rb.full_name as registered_by,
                   vb.full_name as verified_by,
                   lb.full_name as released_by,
                   (pp.id_photo_path is not null) as has_id_photo
              from pickup_persons pp
              join pickups p on p.id = pp.pickup_id
              join v_pickup_status ps on ps.pickup_id = p.id
              join batches b on b.id = p.batch_id
              join visitors vi on vi.id = pp.visitor_id
              left join profiles rb on rb.id = p.registered_by
              left join profiles vb on vb.id = p.verified_by
              left join profiles lb on lb.id = p.released_by
             where (cast(:on_date as date) is null
                    or coalesce(p.time_in, p.registered_at)::date = cast(:on_date as date))
             order by coalesce(p.time_in, p.registered_at) desc
             limit 1000
            """
        ),
        {"on_date": on_date},
    )
    return [dict(r) for r in rows.mappings()]


@router.get("/reports/daily-activity")
async def daily_activity(
    on_date: Optional[date] = Query(default=None),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_ops_manager),
):
    row = (
        await conn.execute(
            text(
                """
                with d as (select coalesce(cast(:on_date as date), current_date) as day)
                select
                  (select day from d) as day,
                  (select count(*) from gate_entries, d
                    where time_in::date = d.day)::int                    as trucks_admitted,
                  (select count(*) from gate_entries, d
                    where status = 'rejected' and decided_at::date = d.day)::int as entries_rejected,
                  (select count(*) from boxes b, d
                    where b.verified_at::date = d.day)::int              as boxes_verified,
                  (select count(*) from boxes b, d
                    where b.completed_at::date = d.day)::int             as boxes_closed,
                  (select count(*) from scan_events se, d
                    where se.accepted and se.scan_type = 'unit_verify'
                      and se.recorded_at::date = d.day)::int             as units_scanned,
                  (select count(*) from scan_events se, d
                    where not se.accepted and se.recorded_at::date = d.day)::int as scans_rejected,
                  (select count(*) from exceptions e, d
                    where e.reported_at::date = d.day)::int              as exceptions_raised,
                  (select count(*) from exceptions e, d
                    where e.resolved_at::date = d.day)::int              as exceptions_resolved,
                  (select count(*) from packing_records pr, d
                    where pr.packed_at::date = d.day)::int               as cartons_packed,
                  (select count(*) from batches b, d
                    where b.released_at::date = d.day)::int              as batches_released,
                  (select count(*) from pickups p, d
                    where p.time_out::date = d.day)::int                 as vehicles_departed
                """
            ),
            {"on_date": on_date},
        )
    ).mappings().one()
    return dict(row)


@router.get("/reports/operator-productivity")
async def operator_productivity(
    from_date: Optional[date] = Query(default=None),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_ops_manager),
):
    """Scans per operator, with the rejection rate alongside.

    Volume without the rejection rate would reward scanning fast and wrong, so
    the two are always reported together.
    """
    rows = await conn.execute(
        text(
            """
            select p.full_name, p.employee_code, p.role::text as role,
                   count(*)::int as total_scans,
                   count(*) filter (where se.accepted)::int as accepted_scans,
                   count(*) filter (where not se.accepted)::int as rejected_scans,
                   round(100.0 * count(*) filter (where not se.accepted)
                         / nullif(count(*), 0), 1) as reject_rate_pct,
                   min(se.recorded_at) as first_scan,
                   max(se.recorded_at) as last_scan
              from scan_events se
              join profiles p on p.id = se.scanned_by
             where (cast(:from_date as date) is null or se.recorded_at::date >= cast(:from_date as date))
             group by p.full_name, p.employee_code, p.role
             order by total_scans desc
            """
        ),
        {"from_date": from_date},
    )
    return [dict(r) for r in rows.mappings()]


@router.get("/reports/audit-trail")
async def audit_trail(
    table_name: Optional[str] = Query(default=None),
    record_id: Optional[str] = Query(default=None),
    limit: int = Query(default=200, le=1000),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_ops_manager),
):
    """Who did what, when. PRD §7 — nothing is ever deleted, so this is complete."""
    rows = await conn.execute(
        text(
            """
            select al.id, al.table_name, al.record_id, al.action,
                   al.actor_role, al.actor_source, al.changed_keys, al.occurred_at,
                   p.full_name as actor_name
              from audit_log al
              left join profiles p on p.id = al.actor_id
             where (cast(:table_name as text) is null or al.table_name = cast(:table_name as text))
               and (cast(:record_id as uuid) is null or al.record_id = cast(:record_id as uuid))
             order by al.occurred_at desc
             limit :limit
            """
        ),
        {"table_name": table_name, "record_id": record_id, "limit": limit},
    )
    return [dict(r) for r in rows.mappings()]
