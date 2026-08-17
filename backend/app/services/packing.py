"""Invoice matching, packing attribution, out-scan and batch release.

PRD Steps 7-9, CONTROL POINTS 5 and 6.

The recurring idea in this module is that a badge scan is *attribution*, not
authentication. The station tablet is signed in as a station account; the badge
identifies which of several matchers or packers physically handled the item. So
`verified_by` and `packed_by` are the badge holder, while the RLS policy checks
the session's role. Both have to be right for a record to exist.
"""

import re
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.errors import AppError

BADGE_HINT = "Hold the badge steady under the scanner, or type the code on it."

# The Order No printed on the delivery challan header, e.g. CP002458380_0001.
# Kept identical to the check constraint in 0015_order_no_ocr.sql — the database
# is the authority, and this copy exists only so a bad read is refused with a
# usable message instead of an integrity error.
ORDER_NO_RE = re.compile(r"^CP\d{9}_\d{4}$")
ORDER_NO_HINT = "Expected the form CP002458380_0001 — two letters, nine digits, underscore, four digits."


# ---------------------------------------------------------------------------
# Badges
# ---------------------------------------------------------------------------


async def resolve_badge(
    conn: AsyncConnection, badge_code: str, expected_roles: List[str]
) -> Dict[str, Any]:
    """Turn a scanned badge into the person it belongs to.

    Returns only the name, role and id — never anything that could be used to
    act as that person. A badge is a label, and this endpoint treats it as one.
    """
    code = badge_code.strip()

    # Through the definer function, not a direct read: `profiles.badge_code` is
    # not selectable by `authenticated` at all, because being able to read
    # someone's badge code is equivalent to being able to present their badge.
    row = (
        await conn.execute(
            text(
                """
                select id, full_name, role, employee_code, badge_active, is_active
                  from resolve_badge_holder(:code)
                """
            ),
            {"code": code},
        )
    ).mappings().first()

    if row is None:
        raise AppError(
            "Badge not recognised.",
            code="unknown_badge",
            http_status=404,
            hint=BADGE_HINT,
        )

    if not row["badge_active"] or not row["is_active"]:
        raise AppError(
            f"{row['full_name']}'s badge has been deactivated.",
            code="badge_inactive",
            http_status=403,
            hint="Ask Ops to issue a replacement badge.",
        )

    if row["role"] not in expected_roles and row["role"] not in ("ops_manager", "admin"):
        readable = " or ".join(r.replace("_", " ") for r in expected_roles)
        raise AppError(
            f"{row['full_name']} is not a {readable}.",
            code="wrong_badge_role",
            http_status=403,
        )

    return {
        "profile_id": row["id"],
        "full_name": row["full_name"],
        "role": row["role"],
        "employee_code": row["employee_code"],
    }


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------

_INVOICE_SELECT = """
    select invoice_id, invoice_number, order_no, sku, units, customer_name, description,
           is_open, stage,
           verified_by, verified_by_name, verified_at,
           packed_by, packed_by_name, packed_at,
           batch_id, batch_code, batch_status, out_scanned_at
      from v_invoice_status
"""


async def list_invoices(
    conn: AsyncConnection, stage: Optional[str] = None, limit: int = 200
) -> List[Dict[str, Any]]:
    rows = await conn.execute(
        text(
            _INVOICE_SELECT
            + """
             where (cast(:stage as text) is null or stage = cast(:stage as text))
             order by invoice_number
             limit :limit
            """
        ),
        {"stage": stage, "limit": limit},
    )
    return [dict(r) for r in rows.mappings()]


async def get_invoice_by_number(conn: AsyncConnection, invoice_number: str) -> Dict[str, Any]:
    row = (
        await conn.execute(
            text(_INVOICE_SELECT + " where upper(invoice_number) = :num"),
            {"num": invoice_number.strip().upper()},
        )
    ).mappings().first()

    if row is None:
        raise AppError(
            f"No invoice with number {invoice_number.strip().upper()}.",
            code="unknown_invoice",
            http_status=404,
            hint="Check the number on the invoice sheet.",
        )
    return dict(row)


async def get_invoice(conn: AsyncConnection, invoice_id: UUID) -> Dict[str, Any]:
    row = (
        await conn.execute(
            text(_INVOICE_SELECT + " where invoice_id = :id"), {"id": str(invoice_id)}
        )
    ).mappings().first()

    if row is None:
        raise AppError("Invoice not found.", code="not_found", http_status=404)
    return dict(row)


async def lookup_for_matching(conn: AsyncConnection, invoice_number: str) -> Dict[str, Any]:
    """PRD §5.4 step 1: is this a valid, open invoice waiting to be matched?

    Answers before the matcher fetches the product from the rack, so a wasted
    trip to the wrong aisle is avoided rather than discovered.
    """
    invoice = await get_invoice_by_number(conn, invoice_number)

    if not invoice["is_open"]:
        raise AppError(
            f"Invoice {invoice['invoice_number']} is closed.",
            code="invoice_closed",
            http_status=409,
        )

    if invoice["verified_at"] is not None:
        raise AppError(
            f"Invoice {invoice['invoice_number']} was already verified by "
            f"{invoice['verified_by_name']}.",
            code="already_verified",
            http_status=409,
            hint="Pass it to a packer instead.",
        )

    # Where the stock for this SKU is sitting, so the matcher knows which rack to
    # walk to. Phase 2 put it there; this is the first thing that reads it back.
    locations = await conn.execute(
        text(
            """
            select location_code, units
              from v_stock_by_location
             where sku = :sku and not is_quarantine and units > 0
             order by units desc
             limit 5
            """
        ),
        {"sku": invoice["sku"]},
    )

    invoice["suggested_locations"] = [dict(r) for r in locations.mappings()]
    return invoice


# ---------------------------------------------------------------------------
# Order No capture (OCR)
# ---------------------------------------------------------------------------


async def record_order_no(
    conn: AsyncConnection,
    invoice_number: str,
    order_no: Optional[str],
    source: str,
    actor_id: str,
    raw_text: Optional[str] = None,
    confidence: Optional[float] = None,
    was_corrected: bool = False,
) -> Dict[str, Any]:
    """Attach the challan's Order No to an invoice, and log how it was read.

    Two properties this function is built around:

    **A failed read is still recorded.** `order_no` may be None — the camera saw
    the challan and could not produce a conforming string. That row is the point:
    a station whose reads miss all morning has a smeared lens or a bad crop
    region, and that is only visible if the misses were written down. A miss
    leaves `invoices.order_no` untouched rather than nulling it, so a good value
    already captured is never destroyed by a later bad attempt.

    **Re-reading is allowed; silently changing the value is not.** If a different
    Order No is already attached, this refuses. Two challans disagreeing about
    which order a shipment belongs to is a discrepancy for a human to resolve,
    not something to settle by letting the most recent scan win — the same
    principle as the count-mismatch handling at CONTROL POINT 3.
    """
    invoice = await get_invoice_by_number(conn, invoice_number)

    if order_no is not None:
        order_no = order_no.strip().upper()
        if not ORDER_NO_RE.match(order_no):
            raise AppError(
                f"{order_no} is not a valid Order No.",
                code="bad_order_no",
                http_status=422,
                hint=ORDER_NO_HINT,
            )

    existing = invoice["order_no"]
    if order_no is not None and existing is not None and existing != order_no:
        raise AppError(
            f"Invoice {invoice['invoice_number']} is already booked against order "
            f"{existing}.",
            code="order_no_conflict",
            http_status=409,
            hint=(
                "Raise an exception rather than overwriting — two different order "
                "numbers on one invoice needs a human decision."
            ),
        )

    # The scan log is written whether or not the read succeeded, and before the
    # invoice is touched. If the update below were to fail, the evidence that
    # someone pointed a camera at this challan still survives.
    await conn.execute(
        text(
            """
            insert into order_no_scans
                (invoice_id, raw_text, parsed_order_no, confidence, source, was_corrected,
                 scanned_by)
            values
                (:invoice_id, :raw_text, :parsed, :confidence, :source, :was_corrected,
                 :who)
            """
        ),
        {
            "invoice_id": str(invoice["invoice_id"]),
            "raw_text": raw_text,
            "parsed": order_no,
            "confidence": confidence,
            "source": source,
            "was_corrected": was_corrected,
            "who": actor_id,
        },
    )

    if order_no is not None and existing is None:
        await conn.execute(
            text("update invoices set order_no = :order_no where id = :id"),
            {"order_no": order_no, "id": str(invoice["invoice_id"])},
        )

    after = await get_invoice(conn, invoice["invoice_id"])
    return {
        "invoice": after,
        "recorded": order_no is not None,
        "message": (
            f"Order {order_no} attached to invoice {after['invoice_number']}."
            if order_no is not None
            else "Could not read an Order No from the challan. Type it instead."
        ),
    }


async def verify_invoice(
    conn: AsyncConnection, invoice_number: str, badge_code: str
) -> Dict[str, Any]:
    """CONTROL POINT 5, first half: the matcher confirms product against invoice."""
    invoice = await lookup_for_matching(conn, invoice_number)
    badge = await resolve_badge(conn, badge_code, ["invoice_matcher"])

    await conn.execute(
        text(
            """
            insert into invoice_verifications (invoice_id, verified_by)
            values (:invoice_id, :who)
            """
        ),
        {"invoice_id": str(invoice["invoice_id"]), "who": str(badge["profile_id"])},
    )

    after = await get_invoice(conn, invoice["invoice_id"])
    return {
        "invoice": after,
        "verified_by": badge,
        "message": (
            f"Invoice {after['invoice_number']} verified by {badge['full_name']} — "
            "ready for packing"
        ),
    }


async def pack_invoice(
    conn: AsyncConnection,
    invoice_number: str,
    badge_code: str,
    carton_code: Optional[str] = None,
) -> Dict[str, Any]:
    """CONTROL POINT 5, second half: the packer's badge is bound to the invoice.

    The database refuses if the invoice was never verified, or if the packer is
    the same person who verified it.
    """
    invoice = await get_invoice_by_number(conn, invoice_number)

    if not invoice["is_open"]:
        raise AppError(
            f"Invoice {invoice['invoice_number']} is closed.",
            code="invoice_closed",
            http_status=409,
        )

    if invoice["packed_at"] is not None:
        raise AppError(
            f"Invoice {invoice['invoice_number']} was already packed by "
            f"{invoice['packed_by_name']}.",
            code="already_packed",
            http_status=409,
        )

    if invoice["verified_at"] is None:
        raise AppError(
            f"Invoice {invoice['invoice_number']} has not been verified by an "
            "invoice matcher (CONTROL POINT 5).",
            code="control_point_failed",
            http_status=409,
            hint="The matcher must scan the invoice and their badge first.",
        )

    badge = await resolve_badge(conn, badge_code, ["packer"])

    if str(badge["profile_id"]) == str(invoice["verified_by"]):
        raise AppError(
            "The invoice matcher and the packer must be different people "
            "(CONTROL POINT 5).",
            code="control_point_failed",
            http_status=409,
            hint=f"{invoice['verified_by_name']} verified this invoice.",
        )

    await conn.execute(
        text(
            """
            insert into packing_records (invoice_id, packed_by, carton_code)
            values (:invoice_id, :who, :carton)
            """
        ),
        {
            "invoice_id": str(invoice["invoice_id"]),
            "who": str(badge["profile_id"]),
            "carton": carton_code,
        },
    )

    after = await get_invoice(conn, invoice["invoice_id"])
    return {
        "invoice": after,
        "packed_by": badge,
        "message": (
            f"Invoice {after['invoice_number']} packed by {badge['full_name']} at "
            f"{after['packed_at']:%H:%M}"
        ),
    }


# ---------------------------------------------------------------------------
# Batches and out-scan (CONTROL POINT 6)
# ---------------------------------------------------------------------------


async def create_batch(
    conn: AsyncConnection, invoice_ids: List[UUID], notes: Optional[str] = None
) -> Dict[str, Any]:
    """Plan a batch from packed-but-unbatched cartons.

    The batch is planned *before* out-scanning so that CONTROL POINT 6 compares
    two independent numbers — cartons assigned against cartons physically
    scanned — in the same shape as the box count at the gate. A batch assembled
    from whatever happened to get scanned could never fail.
    """
    if not invoice_ids:
        raise AppError("Select at least one carton.", code="empty_batch", http_status=422)

    rows = list(
        (
            await conn.execute(
                text(
                    """
                    select pr.id as packing_record_id, pr.invoice_id, pr.batch_id,
                           i.invoice_number
                      from packing_records pr
                      join invoices i on i.id = pr.invoice_id
                     where pr.invoice_id = any(cast(:ids as uuid[]))
                    """
                ),
                {"ids": [str(i) for i in invoice_ids]},
            )
        ).mappings()
    )

    found = {str(r["invoice_id"]) for r in rows}
    missing = [str(i) for i in invoice_ids if str(i) not in found]
    if missing:
        raise AppError(
            f"{len(missing)} selected invoice(s) have not been packed yet.",
            code="not_packed",
            http_status=409,
            hint="Only packed cartons can go into a batch.",
        )

    already = [r["invoice_number"] for r in rows if r["batch_id"] is not None]
    if already:
        raise AppError(
            f"Already in a batch: {', '.join(already)}.",
            code="already_batched",
            http_status=409,
        )

    batch_id = (
        await conn.execute(
            text(
                """
                insert into batches (planned_carton_count, created_by, notes, status)
                values (:count, auth.uid(), :notes, 'open')
                returning id
                """
            ),
            {"count": len(rows), "notes": notes},
        )
    ).scalar_one()

    await conn.execute(
        text(
            """
            update packing_records set batch_id = :batch_id
             where invoice_id = any(cast(:ids as uuid[]))
            """
        ),
        {"batch_id": str(batch_id), "ids": [str(i) for i in invoice_ids]},
    )

    return await get_batch(conn, batch_id)


async def get_batch(conn: AsyncConnection, batch_id: UUID) -> Dict[str, Any]:
    row = (
        await conn.execute(
            text(
                """
                select batch_id, batch_code, status, planned_carton_count,
                       assigned_cartons, scanned_cartons, remaining_cartons,
                       created_at, created_by_name, released_at, released_by_name, notes
                  from v_batch_status where batch_id = :id
                """
            ),
            {"id": str(batch_id)},
        )
    ).mappings().first()

    if row is None:
        raise AppError("Batch not found.", code="not_found", http_status=404)

    batch = dict(row)
    batch["cartons"] = await batch_cartons(conn, batch_id)
    batch["message"] = _batch_message(batch)
    return batch


def _batch_message(batch: Dict[str, Any]) -> str:
    if batch["status"] == "released":
        return f"Batch released for pickup by {batch['released_by_name']}."
    if batch["status"] == "complete":
        return (
            f"Batch complete — {batch['scanned_cartons']} cartons ready for pickup"
        )
    if batch["status"] == "cancelled":
        return "Batch cancelled."
    if batch["remaining_cartons"] == 0 and batch["assigned_cartons"] > 0:
        return "All cartons scanned — confirm to complete the batch"
    return (
        f"{batch['scanned_cartons']} of {batch['assigned_cartons']} cartons out-scanned"
    )


async def batch_cartons(conn: AsyncConnection, batch_id: UUID) -> List[Dict[str, Any]]:
    rows = await conn.execute(
        text(
            """
            select i.id as invoice_id, i.invoice_number, i.sku, i.units, i.customer_name,
                   pp.full_name as packed_by_name, pr.packed_at,
                   pr.out_scanned_at, op.full_name as out_scanned_by_name
              from packing_records pr
              join invoices i on i.id = pr.invoice_id
              left join profiles pp on pp.id = pr.packed_by
              left join profiles op on op.id = pr.out_scanned_by
             where pr.batch_id = :id
             order by i.invoice_number
            """
        ),
        {"id": str(batch_id)},
    )
    return [dict(r) for r in rows.mappings()]


async def list_batches(
    conn: AsyncConnection, status: Optional[str] = None
) -> List[Dict[str, Any]]:
    rows = await conn.execute(
        text(
            """
            select batch_id, batch_code, status, planned_carton_count,
                   assigned_cartons, scanned_cartons, remaining_cartons,
                   created_at, created_by_name, released_at, released_by_name, notes
              from v_batch_status
             where (cast(:status as text) is null or status = cast(:status as text))
             order by created_at desc
             limit 100
            """
        ),
        {"status": status},
    )
    return [dict(r) | {"cartons": [], "message": ""} for r in rows.mappings()]


async def complete_batch(conn: AsyncConnection, batch_id: UUID) -> Dict[str, Any]:
    """Close CONTROL POINT 6.

    Like the other control points, a failure here returns rather than raises: the
    caller gets a 409 with the two numbers so the operator can see which cartons
    are missing.
    """
    batch = await get_batch(conn, batch_id)

    if batch["status"] in ("complete", "released"):
        return {
            "batch": batch,
            "completed": True,
            "message": f"Batch {batch['batch_code']} is already {batch['status']}.",
        }

    if batch["remaining_cartons"] > 0:
        missing = [c["invoice_number"] for c in batch["cartons"] if not c["out_scanned_at"]]
        return {
            "batch": batch,
            "completed": False,
            "message": (
                f"Batch {batch['batch_code']}: {batch['scanned_cartons']} of "
                f"{batch['assigned_cartons']} cartons out-scanned. "
                f"Missing: {', '.join(missing[:5])}"
                + (" …" if len(missing) > 5 else "")
            ),
        }

    await conn.execute(
        text("update batches set status = 'complete' where id = :id"),
        {"id": str(batch_id)},
    )

    after = await get_batch(conn, batch_id)
    return {
        "batch": after,
        "completed": True,
        "message": f"Batch complete — {after['assigned_cartons']} cartons ready for pickup",
    }


async def release_batch(conn: AsyncConnection, batch_id: UUID) -> Dict[str, Any]:
    """Hand the batch to the pickup area. Only possible once it is complete."""
    batch = await get_batch(conn, batch_id)

    if batch["status"] == "released":
        raise AppError(
            f"Batch {batch['batch_code']} was already released by "
            f"{batch['released_by_name']}.",
            code="already_released",
            http_status=409,
        )

    if batch["status"] != "complete":
        raise AppError(
            f"Batch {batch['batch_code']} cannot be released until every carton is "
            "out-scanned (CONTROL POINT 6).",
            code="control_point_failed",
            http_status=409,
            hint=f"{batch['remaining_cartons']} carton(s) still to scan.",
        )

    await conn.execute(
        text(
            """
            update batches
               set status = 'released', released_by = auth.uid(), released_at = now()
             where id = :id
            """
        ),
        {"id": str(batch_id)},
    )

    # Invoices are closed on release: the goods have left the packing area and
    # the invoice is no longer something the floor should be able to act on.
    await conn.execute(
        text(
            """
            update invoices
               set is_open = false, closed_at = now(),
                   closed_reason = 'Released in batch ' || :code
             where id in (select invoice_id from packing_records where batch_id = :id)
            """
        ),
        {"id": str(batch_id), "code": batch["batch_code"]},
    )

    return await get_batch(conn, batch_id)


async def packing_productivity(
    conn: AsyncConnection, from_date: Optional[str] = None
) -> List[Dict[str, Any]]:
    """PRD §5.10 Packer Productivity — cartons packed, and errors alongside.

    Volume on its own would reward packing fast and wrong, so the count of
    exceptions raised against a packer's cartons is reported next to it.
    """
    rows = await conn.execute(
        text(
            """
            select p.full_name, p.employee_code,
                   count(pr.id)::int as cartons_packed,
                   min(pr.packed_at) as first_carton,
                   max(pr.packed_at) as last_carton,
                   sum(i.units)::int as units_packed,
                   round(
                     extract(epoch from (max(pr.packed_at) - min(pr.packed_at)))
                     / 60.0 / nullif(count(pr.id) - 1, 0), 1
                   ) as avg_minutes_per_carton
              from packing_records pr
              join profiles p on p.id = pr.packed_by
              join invoices i on i.id = pr.invoice_id
             where (cast(:from_date as date) is null
                    or pr.packed_at::date >= cast(:from_date as date))
             group by p.full_name, p.employee_code
             order by cartons_packed desc
            """
        ),
        {"from_date": from_date},
    )
    return [dict(r) for r in rows.mappings()]
