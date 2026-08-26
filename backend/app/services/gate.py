"""Gate entry service — PRD Steps 1 and 2, CONTROL POINTS 1 and 2."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.deps import CurrentUser
from app.core.config import get_settings
from app.core.errors import AppError, ControlPointError
from app.schemas.gate import GateEntryCreate, PersonIn, VisitorLookup
from app.services import notifications


# ---------------------------------------------------------------------------
# Visitor identity (DECISIONS.md §2 — photo on first visit, refreshed at 180d)
# ---------------------------------------------------------------------------


async def lookup_visitor(conn: AsyncConnection, mobile: str) -> VisitorLookup:
    settings = get_settings()

    row = (
        await conn.execute(
            text(
                """
                select id, full_name, id_photo_path, id_photo_captured_at,
                       last_seen_at, is_blocked, blocked_reason
                  from visitors
                 where mobile = :mobile
                """
            ),
            {"mobile": mobile},
        )
    ).mappings().first()

    if row is None:
        return VisitorLookup(
            found=False,
            photo_required=True,
            reason="First-time visitor — identity photo required.",
        )

    if row["is_blocked"]:
        return VisitorLookup(
            found=True,
            visitor_id=row["id"],
            full_name=row["full_name"],
            photo_required=False,
            reason="This person is blocked from entering.",
            last_seen_at=row["last_seen_at"],
            is_blocked=True,
            blocked_reason=row["blocked_reason"],
        )

    captured = row["id_photo_captured_at"]
    if captured is None:
        return VisitorLookup(
            found=True,
            visitor_id=row["id"],
            full_name=row["full_name"],
            photo_required=True,
            reason="No identity photo on file — please capture one.",
            last_seen_at=row["last_seen_at"],
        )

    age_days = (datetime.now(timezone.utc) - captured).days
    if age_days >= settings.id_photo_revalidation_days:
        return VisitorLookup(
            found=True,
            visitor_id=row["id"],
            full_name=row["full_name"],
            photo_required=True,
            reason=f"Identity photo is {age_days} days old — please recapture.",
            last_seen_at=row["last_seen_at"],
        )

    return VisitorLookup(
        found=True,
        visitor_id=row["id"],
        full_name=row["full_name"],
        photo_required=False,
        reason="Known visitor — confirm identity against the photo on file.",
        last_seen_at=row["last_seen_at"],
    )


async def upsert_visitor(conn: AsyncConnection, person: PersonIn) -> Dict[str, Any]:
    """Find or create the visitor, and enforce the photo requirement.

    Shared with the outbound pickup flow, so a driver who both delivers and
    collects is one person in the registry rather than two.

    Deduplication is on mobile number. Two people genuinely sharing a number is
    rare enough, and far less costly, than a new visitor row on every visit —
    which would make the "first visit only" photo rule meaningless.
    """
    existing = await lookup_visitor(conn, person.mobile)

    if existing.is_blocked:
        raise AppError(
            f"{existing.full_name} is blocked from entering the premises.",
            code="visitor_blocked",
            http_status=403,
            hint=existing.blocked_reason,
        )

    if existing.photo_required and not person.id_photo_path:
        raise AppError(
            f"An identity photo is required for {person.full_name}.",
            code="photo_required",
            http_status=422,
            hint=existing.reason,
            details={"mobile": person.mobile},
        )

    row = (
        await conn.execute(
            text(
                """
                insert into visitors (mobile, full_name, id_photo_path, id_photo_captured_at)
                values (:mobile, :full_name, :photo,
                        case when cast(:photo as text) is null then null else now() end)
                on conflict (mobile) do update
                   set full_name = excluded.full_name,
                       last_seen_at = now(),
                       id_photo_path = coalesce(excluded.id_photo_path, visitors.id_photo_path),
                       id_photo_captured_at = case
                           when excluded.id_photo_path is not null then now()
                           else visitors.id_photo_captured_at
                       end
                returning id, id_photo_path, (xmax = 0) as inserted
                """
            ),
            {
                "mobile": person.mobile,
                "full_name": person.full_name,
                "photo": person.id_photo_path,
            },
        )
    ).mappings().one()

    return {
        "visitor_id": row["id"],
        "id_photo_path": row["id_photo_path"],
        "is_returning": not row["inserted"],
    }


# ---------------------------------------------------------------------------
# CONTROL POINT 1 — entry request and approval
# ---------------------------------------------------------------------------


async def create_entry(
    conn: AsyncConnection, user: CurrentUser, payload: GateEntryCreate
) -> UUID:
    """Register everyone on the truck and send the request to Admin.

    Everything here happens in one transaction: if any person fails validation,
    no gate entry exists at all. A half-registered truck is worse than none —
    it looks approved-able in the Admin queue while missing a laborer.
    """
    if payload.purchase_order_id is not None:
        po = (
            await conn.execute(
                text(
                    """
                    select po.id, po.status::text as status, po.vendor_id, v.name as vendor_name
                      from purchase_orders po join vendors v on v.id = po.vendor_id
                     where po.id = :po_id
                    """
                ),
                {"po_id": str(payload.purchase_order_id)},
            )
        ).mappings().first()

        if po is None:
            raise AppError("That purchase order does not exist.", code="unknown_po", http_status=422)
        if po["status"] in ("closed", "cancelled"):
            raise AppError(
                f"Purchase order is {po['status']} and cannot receive goods.",
                code="po_not_open",
                http_status=422,
            )
        if po["vendor_id"] != payload.vendor_id:
            raise AppError(
                f"That purchase order belongs to {po['vendor_name']}, not the vendor selected.",
                code="po_vendor_mismatch",
                http_status=422,
                hint="Check the PO number on the delivery challan.",
            )

    entry_id = (
        await conn.execute(
            text(
                """
                insert into gate_entries
                  (status, vehicle_number, vendor_id, purchase_order_id,
                   transporter_name, requested_by, requested_at)
                values
                  ('pending_approval', :vehicle, :vendor_id, :po_id,
                   :transporter, auth.uid(), now())
                returning id
                """
            ),
            {
                "vehicle": payload.vehicle_number,
                "vendor_id": str(payload.vendor_id),
                "po_id": str(payload.purchase_order_id) if payload.purchase_order_id else None,
                "transporter": payload.transporter_name,
            },
        )
    ).scalar_one()

    for person in payload.persons:
        v = await upsert_visitor(conn, person)
        await conn.execute(
            text(
                """
                insert into gate_entry_persons
                  (gate_entry_id, visitor_id, visitor_role, id_photo_path)
                values (:entry_id, :visitor_id, :role, :photo)
                """
            ),
            {
                "entry_id": str(entry_id),
                "visitor_id": str(v["visitor_id"]),
                "role": person.visitor_role,
                # Snapshot: refreshing the visitor's photo later must not rewrite
                # which photo was used to admit them on this visit.
                "photo": person.id_photo_path or v["id_photo_path"],
            },
        )

    driver = next(p for p in payload.persons if p.visitor_role == "driver")
    entry = await get_entry(conn, entry_id)

    await notifications.notify_ops(
        conn,
        title="New truck arrived — approval needed",
        body=(
            f"Driver: {driver.full_name} ({driver.mobile})\n"
            f"Vehicle: {payload.vehicle_number}\n"
            f"Vendor: {entry['vendor_name']}\n"
            f"Entry: {entry['entry_code']}"
        ),
        payload={"entry_code": entry["entry_code"], "vehicle": payload.vehicle_number},
        gate_entry_id=entry_id,
    )

    return entry_id


async def decide_entry(
    conn: AsyncConnection,
    user: CurrentUser,
    entry_id: UUID,
    approve: bool,
    note: Optional[str],
) -> Dict[str, Any]:
    """Admin approves or rejects. CONTROL POINT 1.

    The self-approval bar, the role requirement and the legal-transition check
    all live in the database (0002/0004). What is added here is the human-facing
    message when one of them fires.
    """
    current = (
        await conn.execute(
            text("select status::text as status, requested_by from gate_entries where id = :id"),
            {"id": str(entry_id)},
        )
    ).mappings().first()

    if current is None:
        raise AppError("Gate entry not found.", code="not_found", http_status=404)

    if current["status"] != "pending_approval":
        raise AppError(
            f"This entry is already {current['status'].replace('_', ' ')}.",
            code="already_decided",
            http_status=409,
        )

    if str(current["requested_by"]) == user.id:
        raise ControlPointError(
            "You cannot approve an entry you requested yourself.",
            hint="Ask another Admin to review it.",
        )

    await conn.execute(
        text(
            """
            update gate_entries
               set status = case when :approve then 'approved'::gate_entry_status
                                 else 'rejected'::gate_entry_status end,
                   decided_by = auth.uid(),
                   decided_at = now(),
                   decision_note = :note
             where id = :id
            """
        ),
        {"approve": approve, "note": note, "id": str(entry_id)},
    )

    entry = await get_entry(conn, entry_id)

    await notifications.notify(
        conn,
        title="Entry approved — open the gate" if approve else "Entry rejected",
        body=(
            f"{entry['entry_code']} · {entry['vehicle_number']}"
            + (f"\nReason: {note}" if note else "")
        ),
        recipient_id=entry["requested_by"],
        payload={"approved": approve},
        gate_entry_id=entry_id,
    )

    return entry


async def admit_vehicle(conn: AsyncConnection, entry_id: UUID) -> Dict[str, Any]:
    """Guard opens the gate. Stamps time_in. Refused unless status is 'approved'.

    The status is checked here as well as in the trigger, and that is not
    redundant. RLS hides a `pending_approval` row from a guard's UPDATE, so the
    statement matches zero rows, the trigger never fires, and the update
    silently does nothing. The control point holds either way — the truck does
    not get in — but without this check the API would answer 200 and the guard
    would drive a vehicle through an unopened gate.
    """
    current = (
        await conn.execute(
            text("select status::text as status from gate_entries where id = :id"),
            {"id": str(entry_id)},
        )
    ).mappings().first()

    if current is None:
        raise AppError("Gate entry not found.", code="not_found", http_status=404)

    if current["status"] != "approved":
        if current["status"] == "pending_approval":
            raise ControlPointError(
                "Vehicle cannot enter without Admin approval (CONTROL POINT 1).",
                hint="The approval request is still waiting with Admin.",
            )
        raise ControlPointError(
            f"This entry is {current['status'].replace('_', ' ')} and cannot be admitted.",
            hint="Only an approved entry can open the gate.",
        )

    result = await conn.execute(
        text("update gate_entries set status = 'inside' where id = :id and status = 'approved'"),
        {"id": str(entry_id)},
    )

    if result.rowcount == 0:
        # Belt and braces: RLS refused the row even though the status looked
        # right. Never report success for an update that did not happen.
        raise ControlPointError(
            "You are not permitted to admit this vehicle.",
            hint="Ask Admin to open the gate.",
        )

    return await get_entry(conn, entry_id)


# ---------------------------------------------------------------------------
# CONTROL POINT 2 — box count
# ---------------------------------------------------------------------------


async def declare_box_count(
    conn: AsyncConnection, entry_id: UUID, box_count: int
) -> Dict[str, Any]:
    """The guard's physical count. Recorded before Admin issues any stickers, so
    that the sticker count is derived from it rather than negotiated with it."""
    entry = (
        await conn.execute(
            text(
                """
                select status::text as status, declared_box_count, issued_box_sticker_count
                  from gate_entries where id = :id
                """
            ),
            {"id": str(entry_id)},
        )
    ).mappings().first()

    if entry is None:
        raise AppError("Gate entry not found.", code="not_found", http_status=404)

    if entry["status"] not in ("inside", "counting"):
        raise AppError(
            "The vehicle must be admitted before boxes can be counted.",
            code="wrong_status",
            http_status=409,
            hint=f"Current status: {entry['status'].replace('_', ' ')}.",
        )

    # Re-declaring after stickers are printed would let someone quietly move the
    # target to match whatever was scanned. Admin must reissue instead.
    if entry["issued_box_sticker_count"] > 0 and entry["declared_box_count"] != box_count:
        raise ControlPointError(
            f"{entry['issued_box_sticker_count']} stickers have already been issued "
            f"for a count of {entry['declared_box_count']}.",
            hint="Ask Admin to void the sheet and reissue before changing the count.",
        )

    await conn.execute(
        text(
            """
            update gate_entries
               set declared_box_count = :count,
                   declared_by = auth.uid(),
                   declared_at = now(),
                   status = 'counting'
             where id = :id
            """
        ),
        {"count": box_count, "id": str(entry_id)},
    )

    return await get_entry(conn, entry_id)


async def verify_box_count(conn: AsyncConnection, entry_id: UUID) -> Dict[str, Any]:
    """Close CONTROL POINT 2.

    On a mismatch this returns `verified: False` and logs an exception rather
    than raising. The route still answers 409 and the boxes still cannot move
    inside — but the exception record has to survive the request, and raising
    would roll back the transaction that created it.
    """
    entry = await get_entry(conn, entry_id)

    declared = entry["declared_box_count"] or 0
    issued = entry["issued_box_sticker_count"] or 0
    scanned = entry["scanned_box_count"] or 0

    if scanned != declared or issued != declared:
        code = await raise_count_exception(
            conn,
            entry_id=entry_id,
            title=f"Box count mismatch on {entry['entry_code']}",
            details={
                "declared_box_count": declared,
                "issued_sticker_count": issued,
                "scanned_box_count": scanned,
            },
        )
        return {
            "verified": False,
            "entry": entry,
            "exception_code": code,
            "message": (
                f"Count mismatch: {scanned} of {declared} boxes scanned. Contact Admin."
            ),
        }

    await conn.execute(
        text("update gate_entries set status = 'box_verified' where id = :id"),
        {"id": str(entry_id)},
    )

    return {
        "verified": True,
        "entry": await get_entry(conn, entry_id),
        "exception_code": None,
        "message": "All boxes verified — move to next step",
    }


async def raise_count_exception(
    conn: AsyncConnection,
    *,
    entry_id: UUID,
    title: str,
    details: Dict[str, Any],
    box_id: Optional[UUID] = None,
    exception_type: str = "box_count_mismatch",
) -> str:
    import json

    row = (
        await conn.execute(
            text(
                """
                insert into exceptions
                  (exception_type, gate_entry_id, box_id, purchase_order_id, vendor_id,
                   title, details, reported_by)
                -- cast(... as ...) rather than `:etype::exception_type`:
                -- SQLAlchemy's text() reads the `::` as part of the bind name
                -- and never substitutes the parameter.
                select cast(:etype as exception_type), ge.id, cast(:box_id as uuid),
                       ge.purchase_order_id, ge.vendor_id,
                       :title, cast(:details as jsonb), auth.uid()
                  from gate_entries ge where ge.id = :entry_id
                returning id, exception_code
                """
            ),
            {
                "etype": exception_type,
                "box_id": str(box_id) if box_id else None,
                "title": title,
                "details": json.dumps(details),
                "entry_id": str(entry_id),
            },
        )
    ).mappings().one()

    await notifications.notify_ops(
        conn,
        title=title,
        body="Goods are held pending your decision.",
        payload=details,
        gate_entry_id=entry_id,
        exception_id=row["id"],
    )
    return row["exception_code"]


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

_ENTRY_SELECT = """
    select ge.id, ge.entry_code, ge.status::text as status, ge.vehicle_number,
           ge.vendor_id, v.name as vendor_name,
           ge.purchase_order_id, po.po_number,
           ge.transporter_name,
           ge.requested_by, rp.full_name as requested_by_name, ge.requested_at,
           ge.decided_by, dp.full_name as decided_by_name, ge.decided_at, ge.decision_note,
           ge.sla_breached, ge.escalated_at,
           ge.time_in, ge.time_out,
           ge.declared_box_count, ge.issued_box_sticker_count,
           ge.created_at,
           (select count(*) from boxes b
             where b.gate_entry_id = ge.id and b.status <> 'pending')::int as scanned_box_count
      from gate_entries ge
      join vendors v on v.id = ge.vendor_id
      left join purchase_orders po on po.id = ge.purchase_order_id
      left join profiles rp on rp.id = ge.requested_by
      left join profiles dp on dp.id = ge.decided_by
"""


async def get_entry(conn: AsyncConnection, entry_id: UUID) -> Dict[str, Any]:
    row = (
        await conn.execute(text(_ENTRY_SELECT + " where ge.id = :id"), {"id": str(entry_id)})
    ).mappings().first()

    if row is None:
        raise AppError("Gate entry not found.", code="not_found", http_status=404)

    entry = dict(row)
    entry["persons"] = await _persons_for(conn, entry_id)
    return entry


async def _persons_for(conn: AsyncConnection, entry_id: UUID) -> List[Dict[str, Any]]:
    rows = await conn.execute(
        text(
            """
            select gep.visitor_id, vi.full_name, vi.mobile,
                   gep.visitor_role::text as visitor_role,
                   (gep.id_photo_path is not null) as has_id_photo,
                   (vi.first_seen_at < gep.created_at) as is_returning_visitor
              from gate_entry_persons gep
              join visitors vi on vi.id = gep.visitor_id
             where gep.gate_entry_id = :id
             order by gep.visitor_role, vi.full_name
            """
        ),
        {"id": str(entry_id)},
    )
    return [dict(r) for r in rows.mappings()]


async def list_entries(
    conn: AsyncConnection,
    *,
    status_filter: Optional[List[str]] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    clause = ""
    params: Dict[str, Any] = {"limit": limit, "offset": offset}

    if status_filter:
        clause = " where ge.status::text = any(cast(:statuses as text[])) "
        params["statuses"] = status_filter

    rows = await conn.execute(
        text(_ENTRY_SELECT + clause + " order by ge.created_at desc limit :limit offset :offset"),
        params,
    )
    entries = [dict(r) for r in rows.mappings()]
    for e in entries:
        e["persons"] = await _persons_for(conn, e["id"])
    return entries


# ---------------------------------------------------------------------------
# SLA escalation (DECISIONS.md §4). Called by the worker, never auto-approves.
# ---------------------------------------------------------------------------


async def escalate_overdue_approvals(conn: AsyncConnection) -> Dict[str, int]:
    settings = get_settings()

    # `make_interval(mins => :n)` rather than `cast(:n as interval)`.
    #
    # The cast form types the bind parameter as `interval`, so asyncpg tries to
    # encode the Python value as one and rejects a string outright — the whole
    # sweep died on this line, every cycle, meaning no escalation ever fired and
    # email dispatch below was never reached. make_interval takes an integer,
    # which asyncpg encodes without ambiguity.
    to_backup = await conn.execute(
        text(
            """
            update gate_entries
               set escalated_at = now()
             where status = 'pending_approval'
               and escalated_at is null
               and requested_at < now() - make_interval(mins => :sla)
            returning id, entry_code, vehicle_number
            """
        ),
        {"sla": settings.gate_approval_sla_minutes},
    )
    backup_rows = list(to_backup.mappings())

    for row in backup_rows:
        await notifications.notify(
            conn,
            title=f"Approval overdue: {row['entry_code']}",
            body=(
                f"Vehicle {row['vehicle_number']} has been waiting at the gate for "
                f"{settings.gate_approval_sla_minutes} minutes. Please decide."
            ),
            recipient_role="admin",
            channel="email",
            gate_entry_id=row["id"],
        )

    to_admin = await conn.execute(
        text(
            """
            update gate_entries
               set sla_breached = true
             where status = 'pending_approval'
               and not sla_breached
               and requested_at < now() - make_interval(mins => :hard)
            returning id, entry_code, vehicle_number
            """
        ),
        {"hard": settings.gate_escalation_minutes},
    )
    admin_rows = list(to_admin.mappings())

    for row in admin_rows:
        await notifications.notify_admin(
            conn,
            title=f"SLA breached: {row['entry_code']}",
            body=(
                f"Vehicle {row['vehicle_number']} has waited over "
                f"{settings.gate_escalation_minutes} minutes with no Admin decision. "
                "The gate remains locked — no entry has been auto-approved."
            ),
            gate_entry_id=row["id"],
        )

    return {"escalated": len(backup_rows), "breached": len(admin_rows)}
