"""Pickup verification and gate exit — PRD §5.7, Step 10, CONTROL POINT 7.

The last hard stop and the least negotiable one. Everything before this point can
be corrected inside the building; once a vehicle is on the road a missing carton
is somebody else's problem and nobody's record.
"""

from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.errors import AppError, ControlPointError
from app.schemas.gate import PersonIn
from app.services import gate as gate_service
from app.services import notifications

_SELECT = """
    select pickup_id, pickup_code, status, vehicle_number, courier_name,
           transporter_name, batch_id, batch_code,
           released_cartons, verified_cartons, remaining_cartons,
           registered_at, registered_by_name,
           verified_at, verified_by_name,
           time_in, time_out, released_by_name,
           exit_requested_at, exit_requested_by_name,
           exit_approved_at, exit_approved_by_name,
           exit_rejected_note, exit_waiting_seconds
      from v_pickup_status
"""


def _message(row: Dict[str, Any]) -> str:
    if row["status"] == "departed":
        return (
            f"Vehicle {row['vehicle_number']} released at "
            f"{row['time_out']:%H:%M} with {row['verified_cartons']} cartons."
        )
    if row["status"] == "exit_pending":
        return (
            f"All {row['released_cartons']} cartons verified — waiting for Ops to "
            "approve the gate"
        )
    if row["status"] == "verified":
        if row.get("exit_rejected_note"):
            return f"Ops sent this back: {row['exit_rejected_note']}"
        return (
            f"All {row['released_cartons']} cartons verified — request the gate to be "
            "opened"
        )
    if row["status"] == "cancelled":
        return "Pickup cancelled."
    if row["remaining_cartons"] == 0 and row["released_cartons"] > 0:
        return "All cartons scanned — confirm to release the vehicle"
    return (
        f"{row['verified_cartons']} of {row['released_cartons']} cartons loaded"
    )


async def list_awaiting_pickup(conn: AsyncConnection) -> List[Dict[str, Any]]:
    """Released batches that no vehicle has been registered against yet.

    This is the guard's worklist: what is sitting in the pickup area waiting for
    a courier to arrive.
    """
    rows = await conn.execute(
        text(
            """
            select b.id as batch_id, b.batch_code, b.released_at,
                   count(pr.id)::int as carton_count,
                   rb.full_name as released_by_name
              from batches b
              left join packing_records pr on pr.batch_id = b.id
              left join profiles rb on rb.id = b.released_by
             where b.status = 'released'
               and not exists (select 1 from pickups p where p.batch_id = b.id)
             group by b.id, b.batch_code, b.released_at, rb.full_name
             order by b.released_at
            """
        )
    )
    return [dict(r) for r in rows.mappings()]


async def list_pickups(
    conn: AsyncConnection, status_filter: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    clause = ""
    params: Dict[str, Any] = {}

    if status_filter:
        clause = " where status = any(cast(:statuses as text[])) "
        params["statuses"] = status_filter

    rows = await conn.execute(
        text(_SELECT + clause + " order by registered_at desc limit 100"), params
    )
    return [dict(r) | {"message": _message(dict(r)), "persons": []} for r in rows.mappings()]


async def get_pickup(conn: AsyncConnection, pickup_id: UUID) -> Dict[str, Any]:
    row = (
        await conn.execute(
            text(_SELECT + " where pickup_id = :id"), {"id": str(pickup_id)}
        )
    ).mappings().first()

    if row is None:
        raise AppError("Pickup not found.", code="not_found", http_status=404)

    pickup = dict(row)
    pickup["message"] = _message(pickup)
    pickup["persons"] = await _persons(conn, pickup_id)
    pickup["cartons"] = await cartons(conn, pickup["batch_id"])
    return pickup


async def _persons(conn: AsyncConnection, pickup_id: UUID) -> List[Dict[str, Any]]:
    rows = await conn.execute(
        text(
            """
            select pp.visitor_id, vi.full_name, vi.mobile,
                   pp.visitor_role::text as visitor_role,
                   (pp.id_photo_path is not null) as has_id_photo,
                   (vi.first_seen_at < pp.created_at) as is_returning_visitor
              from pickup_persons pp
              join visitors vi on vi.id = pp.visitor_id
             where pp.pickup_id = :id
             order by pp.visitor_role, vi.full_name
            """
        ),
        {"id": str(pickup_id)},
    )
    return [dict(r) for r in rows.mappings()]


async def cartons(conn: AsyncConnection, batch_id: UUID) -> List[Dict[str, Any]]:
    rows = await conn.execute(
        text(
            """
            select i.id as invoice_id, i.invoice_number, i.sku, i.units,
                   i.customer_name,
                   pp.full_name as packed_by_name,
                   pr.out_scanned_at,
                   pr.exit_scanned_at,
                   ep.full_name as exit_scanned_by_name
              from packing_records pr
              join invoices i on i.id = pr.invoice_id
              left join profiles pp on pp.id = pr.packed_by
              left join profiles ep on ep.id = pr.exit_scanned_by
             where pr.batch_id = :id
             order by i.invoice_number
            """
        ),
        {"id": str(batch_id)},
    )
    return [dict(r) for r in rows.mappings()]


async def register_pickup(
    conn: AsyncConnection,
    batch_id: UUID,
    vehicle_number: str,
    persons: List[PersonIn],
    courier_name: Optional[str] = None,
    transporter_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Log the collecting vehicle and its people at the gate.

    The identity rules are the same as inbound: a first-time courier is
    photographed once, a known one is confirmed against the photo on file. Reusing
    the inbound visitor registry means a driver who delivers and collects is one
    person in the system, not two.

    One transaction: if any person fails validation no pickup exists at all.
    """
    existing = (
        await conn.execute(
            text(
                """
                select p.pickup_code from pickups p where p.batch_id = :batch_id
                """
            ),
            {"batch_id": str(batch_id)},
        )
    ).scalar()

    if existing:
        raise AppError(
            f"This batch has already been registered for pickup as {existing}.",
            code="already_registered",
            http_status=409,
        )

    pickup_id = (
        await conn.execute(
            text(
                """
                insert into pickups
                  (batch_id, vehicle_number, courier_name, transporter_name,
                   registered_by, registered_at, time_in)
                values (:batch_id, :vehicle, :courier, :transporter,
                        auth.uid(), now(), now())
                returning id
                """
            ),
            {
                "batch_id": str(batch_id),
                "vehicle": vehicle_number,
                "courier": courier_name,
                "transporter": transporter_name,
            },
        )
    ).scalar_one()

    for person in persons:
        visitor = await gate_service.upsert_visitor(conn, person)
        await conn.execute(
            text(
                """
                insert into pickup_persons
                  (pickup_id, visitor_id, visitor_role, id_photo_path)
                values (:pickup_id, :visitor_id, :role, :photo)
                """
            ),
            {
                "pickup_id": str(pickup_id),
                "visitor_id": str(visitor["visitor_id"]),
                "role": person.visitor_role,
                "photo": person.id_photo_path or visitor["id_photo_path"],
            },
        )

    pickup = await get_pickup(conn, pickup_id)

    await notifications.notify_ops(
        conn,
        title=f"Pickup registered: {pickup['pickup_code']}",
        body=(
            f"Vehicle {vehicle_number} is collecting {pickup['batch_code']} "
            f"({pickup['released_cartons']} cartons)."
        ),
        payload={"pickup_code": pickup["pickup_code"], "vehicle": vehicle_number},
    )

    return pickup


async def verify_pickup(conn: AsyncConnection, pickup_id: UUID) -> Dict[str, Any]:
    """Close CONTROL POINT 7.

    Returns `verified: False` with a 409 rather than raising, so the guard sees
    exactly which cartons are missing — and so the notification written alongside
    survives the request.
    """
    pickup = await get_pickup(conn, pickup_id)

    if pickup["status"] in ("verified", "departed"):
        return {
            "pickup": pickup,
            "verified": True,
            "message": f"{pickup['pickup_code']} is already {pickup['status']}.",
        }

    if pickup["status"] == "cancelled":
        raise AppError(
            f"{pickup['pickup_code']} was cancelled.", code="cancelled", http_status=409
        )

    if pickup["remaining_cartons"] > 0:
        missing = [c["invoice_number"] for c in pickup["cartons"] if not c["exit_scanned_at"]]

        await notifications.notify_ops(
            conn,
            title=f"Pickup {pickup['pickup_code']} is short",
            body=(
                f"{pickup['verified_cartons']} of {pickup['released_cartons']} cartons "
                f"loaded. Missing: {', '.join(missing[:10])}"
            ),
            payload={"missing": missing},
        )

        return {
            "pickup": pickup,
            "verified": False,
            "message": (
                f"{pickup['verified_cartons']} of {pickup['released_cartons']} cartons "
                f"verified. Missing: {', '.join(missing[:5])}"
                + (" …" if len(missing) > 5 else "")
            ),
        }

    await conn.execute(
        text(
            """
            update pickups
               set status = 'verified', verified_by = auth.uid(), verified_at = now()
             where id = :id
            """
        ),
        {"id": str(pickup_id)},
    )

    after = await get_pickup(conn, pickup_id)
    return {
        "pickup": after,
        "verified": True,
        "message": f"All {after['released_cartons']} cartons verified — vehicle can leave",
    }


async def release_vehicle(conn: AsyncConnection, pickup_id: UUID) -> Dict[str, Any]:
    """Open the gate and stamp time out. Only possible once verified."""
    pickup = await get_pickup(conn, pickup_id)

    if pickup["status"] == "departed":
        raise AppError(
            f"{pickup['pickup_code']} already departed at {pickup['time_out']:%H:%M}.",
            code="already_departed",
            http_status=409,
        )

    if pickup["status"] == "verified":
        raise ControlPointError(
            f"Vehicle {pickup['vehicle_number']} has not been approved to leave yet.",
            hint="Request exit approval, then Ops opens the gate.",
        )

    if pickup["status"] != "exit_pending":
        raise ControlPointError(
            "Vehicle cannot leave until every released carton is verified present "
            "(CONTROL POINT 7).",
            hint=f"{pickup['remaining_cartons']} carton(s) still to scan.",
        )

    if pickup["exit_approved_at"] is None:
        raise ControlPointError(
            f"Ops has not approved {pickup['vehicle_number']} leaving yet.",
            hint="The gate stays shut until the approval is recorded.",
        )

    result = await conn.execute(
        text(
            """
            update pickups
               set status = 'departed', released_by = auth.uid(), time_out = now()
             where id = :id and status = 'exit_pending'
            """
        ),
        {"id": str(pickup_id)},
    )

    if result.rowcount == 0:
        # RLS hid the row, or it changed underneath us. Never report a gate
        # opened when it did not.
        raise ControlPointError(
            "You are not permitted to release this vehicle.",
            hint="Ask the Ops team to release it.",
        )

    return await get_pickup(conn, pickup_id)


async def cancel_pickup(
    conn: AsyncConnection, pickup_id: UUID, reason: str
) -> Dict[str, Any]:
    """Courier left without loading, wrong vehicle, and so on.

    Cancelling does not un-release the batch — the goods are still in the pickup
    area — so a new pickup cannot simply be registered against the same batch
    without Ops involvement. That is deliberate: two pickups for one batch would
    make "all cartons present" ambiguous.
    """
    pickup = await get_pickup(conn, pickup_id)

    if pickup["status"] == "departed":
        raise AppError(
            "The vehicle has already left; this cannot be cancelled.",
            code="already_departed",
            http_status=409,
        )

    await conn.execute(
        text(
            """
            update pickups set status = 'cancelled', cancel_reason = :reason
             where id = :id
            """
        ),
        {"reason": reason, "id": str(pickup_id)},
    )

    await notifications.notify_ops(
        conn,
        title=f"Pickup {pickup['pickup_code']} cancelled",
        body=reason,
    )

    return await get_pickup(conn, pickup_id)


# ---------------------------------------------------------------------------
# Exit approval (Phase 5)
# ---------------------------------------------------------------------------


async def request_exit(conn: AsyncConnection, pickup_id: UUID) -> Dict[str, Any]:
    """The guard asks for the gate to be opened.

    Separate from verification on purpose. CONTROL POINT 7 answers "is every
    carton on the truck"; this answers "may it go". Collapsing them would mean
    the last carton scan opened a gate, and DECISIONS.md §CD4 already argues why
    that is the wrong basis for the decision.
    """
    pickup = await get_pickup(conn, pickup_id)

    if pickup["status"] == "departed":
        raise AppError(
            f"{pickup['pickup_code']} already departed at {pickup['time_out']:%H:%M}.",
            code="already_departed",
            http_status=409,
        )

    if pickup["status"] == "exit_pending":
        return {
            "pickup": pickup,
            "requested": True,
            "message": f"Already waiting for Ops — asked at {pickup['exit_requested_at']:%H:%M}.",
        }

    if pickup["status"] != "verified":
        raise ControlPointError(
            "Every carton has to be verified on the vehicle first (CONTROL POINT 7).",
            hint=f"{pickup['remaining_cartons']} carton(s) still to scan.",
        )

    result = await conn.execute(
        text(
            """
            update pickups
               set status = 'exit_pending',
                   exit_requested_by = auth.uid(),
                   exit_requested_at = now(),
                   exit_rejected_note = null,
                   -- A new request carries no approval, regardless of how the
                   -- row was left by a previous round.
                   exit_approved_by = null,
                   exit_approved_at = null
             where id = :id and status = 'verified'
            """
        ),
        {"id": str(pickup_id)},
    )

    if result.rowcount == 0:
        raise AppError(
            "You are not permitted to request exit for this vehicle.",
            code="not_permitted",
            http_status=403,
        )

    await notifications.notify_ops(
        conn,
        title=f"Gate exit requested: {pickup['vehicle_number']}",
        body=(
            f"{pickup['pickup_code']} — all {pickup['released_cartons']} cartons of batch "
            f"{pickup['batch_code']} are loaded and verified. The vehicle is waiting at "
            "the gate for your approval."
        ),
    )

    after = await get_pickup(conn, pickup_id)
    return {
        "pickup": after,
        "requested": True,
        "message": f"Sent to Ops. {after['vehicle_number']} waits until they approve.",
    }


async def decide_exit(
    conn: AsyncConnection, pickup_id: UUID, approve: bool, note: Optional[str] = None
) -> Dict[str, Any]:
    """Ops decides whether the vehicle may leave.

    Approving records the approval but does *not* open the gate. The guard still
    performs the release, so the gate opening stays attached to the person
    standing at it — which is the same reasoning as §CD4.
    """
    pickup = await get_pickup(conn, pickup_id)

    if pickup["status"] == "departed":
        raise AppError(
            f"{pickup['pickup_code']} already departed.",
            code="already_departed",
            http_status=409,
        )

    if pickup["status"] != "exit_pending":
        raise AppError(
            f"{pickup['pickup_code']} has not requested exit approval.",
            code="wrong_state",
            http_status=409,
            hint="The guard requests it once every carton is loaded.",
        )

    if not approve and not (note or "").strip():
        raise AppError(
            "Say why the vehicle is being held.",
            code="missing_field",
            http_status=422,
            hint="The guard needs to know what to fix.",
        )

    if approve:
        result = await conn.execute(
            text(
                """
                update pickups
                   set exit_approved_by = auth.uid(), exit_approved_at = now(),
                       exit_rejected_note = null
                 where id = :id and status = 'exit_pending'
                """
            ),
            {"id": str(pickup_id)},
        )
    else:
        # Back to 'verified' so the guard can re-request once whatever Ops asked
        # about is dealt with, rather than the pickup being stuck.
        #
        # The approval columns are cleared too, and that is the important part.
        # A pickup stays `exit_pending` after an approval, so Ops changing their
        # mind while the vehicle is still on the pad is a legitimate second
        # decision — and if the reject only withdrew the *request*, the guard
        # could ask again and release against consent that had been taken back.
        result = await conn.execute(
            text(
                """
                update pickups
                   set status = 'verified', exit_rejected_note = :note,
                       exit_requested_by = null, exit_requested_at = null,
                       exit_approved_by = null, exit_approved_at = null
                 where id = :id and status = 'exit_pending'
                """
            ),
            {"id": str(pickup_id), "note": note},
        )

    if result.rowcount == 0:
        raise AppError(
            "You are not permitted to decide gate exits.",
            code="not_permitted",
            http_status=403,
        )

    after = await get_pickup(conn, pickup_id)
    return {
        "pickup": after,
        "approved": approve,
        "message": (
            f"{after['vehicle_number']} approved to leave — the guard opens the gate."
            if approve
            else f"{after['vehicle_number']} held: {note}"
        ),
    }


async def awaiting_exit_approval(conn: AsyncConnection) -> List[Dict[str, Any]]:
    """Vehicles loaded, verified, and waiting on Ops. Oldest first — a truck at a
    gate with the engine running is the most expensive thing to keep waiting."""
    rows = await conn.execute(
        text(_SELECT + " where status = 'exit_pending' order by exit_requested_at")
    )
    return [dict(r) for r in rows.mappings()]
