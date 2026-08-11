"""Putaway — PRD Step 6 (Phase 2).

Moving goods from the offloading bay to a rack. The database enforces the rules
(0007_putaway.sql); this module resolves scanned location codes, splits a box
across racks when it needs to, and produces the messages the floor reads.
"""

from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.errors import AppError

LOCATION_FORMAT_HINT = "Locations look like A-01-04-02-03 (zone, aisle, rack, level, bin)."


async def putaway_queue(
    conn: AsyncConnection, entry_id: Optional[UUID] = None
) -> List[Dict[str, Any]]:
    """Boxes cleared for shelving, with how many units are still unplaced.

    Restricted to reconciled entries by the view itself, so a box whose counts
    the inbound team has not agreed simply never appears as work.
    """
    rows = await conn.execute(
        text(
            """
            select box_id, gate_entry_id, box_number, box_status,
                   stock_remaining, quarantine_remaining,
                   entry_code, vehicle_number, vendor_name, po_number, sku, description
              from v_putaway_queue
             where (cast(:entry_id as uuid) is null or gate_entry_id = cast(:entry_id as uuid))
             order by entry_code, box_number
             limit 200
            """
        ),
        {"entry_id": str(entry_id) if entry_id else None},
    )
    return [dict(r) for r in rows.mappings()]


async def resolve_location(conn: AsyncConnection, code: str) -> Dict[str, Any]:
    """Look up a rack by its scanned or typed code.

    Locations are seeded, never created on the fly, so a typo cannot invent a
    rack that nothing will ever be found in again.
    """
    normalised = code.strip().upper()

    row = (
        await conn.execute(
            text(
                """
                select id, code, zone, description, is_quarantine, is_active
                  from locations where code = :code
                """
            ),
            {"code": normalised},
        )
    ).mappings().first()

    if row is None:
        raise AppError(
            f"No storage location with code {normalised}.",
            code="unknown_location",
            http_status=404,
            hint=LOCATION_FORMAT_HINT,
        )

    if not row["is_active"]:
        raise AppError(
            f"Location {normalised} is not in use.",
            code="location_inactive",
            http_status=409,
        )

    return dict(row)


async def search_locations(
    conn: AsyncConnection, q: Optional[str], quarantine_only: bool = False
) -> List[Dict[str, Any]]:
    rows = await conn.execute(
        text(
            """
            select id, code, zone, description, is_quarantine
              from locations
             where is_active
               and (cast(:q as text) is null or code like '%' || upper(cast(:q as text)) || '%')
               and (not cast(:qonly as boolean) or is_quarantine)
             order by code
             limit 100
            """
        ),
        {"q": q, "qonly": quarantine_only},
    )
    return [dict(r) for r in rows.mappings()]


async def box_putaway_status(conn: AsyncConnection, box_id: UUID) -> Dict[str, Any]:
    row = (
        await conn.execute(
            text(
                """
                select s.box_id, s.box_number, s.box_status,
                       s.scanned_units, s.quarantined_units, s.stock_units,
                       s.stock_placed, s.quarantine_placed,
                       s.stock_remaining, s.quarantine_remaining,
                       pol.sku, pol.description,
                       ge.entry_code, ge.status::text as entry_status
                  from v_box_putaway_status s
                  join gate_entries ge on ge.id = s.gate_entry_id
                  left join purchase_order_lines pol on pol.id = s.purchase_order_line_id
                 where s.box_id = :box_id
                """
            ),
            {"box_id": str(box_id)},
        )
    ).mappings().first()

    if row is None:
        raise AppError("Box not found.", code="not_found", http_status=404)
    return dict(row)


async def record_putaway(
    conn: AsyncConnection,
    box_id: UUID,
    location_code: str,
    units: int,
    disposition: str,
) -> Dict[str, Any]:
    """Place some or all of a box's units on a rack.

    Splitting is allowed — a 10-unit box can go 6 to one bin and 4 to another —
    because forcing a single location would mean lying about where half the
    stock is when a bin is full.
    """
    status = await box_putaway_status(conn, box_id)
    location = await resolve_location(conn, location_code)

    remaining = (
        status["quarantine_remaining"]
        if disposition == "quarantine"
        else status["stock_remaining"]
    )

    if remaining <= 0:
        raise AppError(
            f"Box {status['box_number']} has no {disposition} units left to place.",
            code="nothing_to_place",
            http_status=409,
            hint="This box may already be shelved.",
        )

    if units > remaining:
        raise AppError(
            f"Only {remaining} {disposition} unit(s) left to place on box "
            f"{status['box_number']}.",
            code="too_many_units",
            http_status=422,
        )

    await conn.execute(
        text(
            """
            insert into putaways
              (box_id, location_id, purchase_order_line_id, units, disposition, moved_by)
            select :box_id, :location_id, b.purchase_order_line_id, :units,
                   cast(:disposition as unit_disposition), auth.uid()
              from boxes b where b.id = :box_id
            """
        ),
        {
            "box_id": str(box_id),
            "location_id": str(location["id"]),
            "units": units,
            "disposition": disposition,
        },
    )

    after = await box_putaway_status(conn, box_id)
    done = after["stock_remaining"] <= 0 and after["quarantine_remaining"] <= 0

    if done:
        message = (
            f"Box {after['box_number']} fully shelved — {units} unit(s) to {location['code']}. "
            "Carton can go to the outside rack."
        )
    else:
        left = after["stock_remaining"] + after["quarantine_remaining"]
        message = (
            f"{units} unit(s) placed at {location['code']}. "
            f"{left} unit(s) still to place from box {after['box_number']}."
        )

    return {
        "box": after,
        "location": location,
        "units_placed": units,
        "complete": done,
        "message": message,
    }


async def box_history(conn: AsyncConnection, box_id: UUID) -> List[Dict[str, Any]]:
    rows = await conn.execute(
        text(
            """
            select p.id, l.code as location_code, l.is_quarantine,
                   p.units, p.disposition::text as disposition,
                   p.moved_at, pr.full_name as moved_by_name
              from putaways p
              join locations l on l.id = p.location_id
              left join profiles pr on pr.id = p.moved_by
             where p.box_id = :box_id
             order by p.moved_at
            """
        ),
        {"box_id": str(box_id)},
    )
    return [dict(r) for r in rows.mappings()]


async def stock_by_location(
    conn: AsyncConnection, sku: Optional[str] = None, zone: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Where things are. The lookup Phase 3 picking will run against."""
    rows = await conn.execute(
        text(
            """
            select location_code, zone, is_quarantine, sku, description, units, last_movement
              from v_stock_by_location
             where (cast(:sku as text) is null or sku = upper(cast(:sku as text)))
               and (cast(:zone as text) is null or zone = upper(cast(:zone as text)))
               and units > 0
             order by sku, location_code
             limit 500
            """
        ),
        {"sku": sku, "zone": zone},
    )
    return [dict(r) for r in rows.mappings()]
