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
from app.schemas.warehouse import ScanIn
from app.services import scans

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
            hint="Ask an Admin to issue a replacement badge.",
        )

    if row["role"] not in expected_roles and row["role"] != "admin":
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


async def create_invoice_from_order_no(
    conn: AsyncConnection,
    *,
    order_no: str,
    purchase_order_line_id: UUID,
    units: int,
    customer_name: Optional[str],
    actor_id: str,
    source: str = "ocr",
    raw_text: Optional[str] = None,
    confidence: Optional[float] = None,
    was_corrected: bool = False,
) -> Dict[str, Any]:
    """A Packer books an invoice from the Order No she just OCR-scanned off the
    physical invoice (PRD §5.4). There is no separate typed invoice number —
    the scanned Order No fills both `invoice_number` and `order_no`, so the
    rest of the system (matching, packing, out-scan) keys off exactly the
    value the camera read, unchanged.

    `sku` is deliberately not a caller-supplied field — it is derived from the
    PO line, the same reasoning as CG3's sticker-knows-its-own-SKU check: a
    typed SKU can disagree with the line it is booked against, and a derived
    one cannot.

    `order_no` is not unique in the schema (a re-dispatch can legitimately
    share one — see 0015_order_no_ocr.sql), but `invoice_number` is
    required-unique, so scanning the same Order No twice surfaces the
    existing `duplicate_invoice` conflict rather than silently creating a
    second row.

    The `raw_text`/`confidence`/`was_corrected` provenance is logged into
    `order_no_scans` the same way `record_order_no` logs it for the
    attach-to-existing-invoice path — this is the first read of this Order
    No, not a later one, but the audit reasoning (0015: "the OCR misread it"
    needs to be a checkable claim) applies identically here.
    """
    order_no = order_no.strip().upper()
    if not ORDER_NO_RE.match(order_no):
        raise AppError(
            f"{order_no} is not a valid Order No.",
            code="bad_order_no",
            http_status=422,
            hint=ORDER_NO_HINT,
        )

    line = (
        await conn.execute(
            text("select id, sku from purchase_order_lines where id = :id"),
            {"id": str(purchase_order_line_id)},
        )
    ).mappings().first()

    if line is None:
        raise AppError(
            "That purchase order line does not exist.",
            code="unknown_po_line",
            http_status=404,
        )

    existing = (
        await conn.execute(
            text("select 1 from invoices where upper(invoice_number) = :num"),
            {"num": order_no},
        )
    ).first()

    if existing is not None:
        raise AppError(
            f"Invoice {order_no} already exists.",
            code="duplicate_invoice",
            http_status=409,
            hint="Look it up instead of creating it again.",
        )

    invoice_id = (
        await conn.execute(
            text(
                """
                insert into invoices
                  (invoice_number, order_no, purchase_order_line_id, sku, units, customer_name)
                values (:num, :num, :line_id, :sku, :units, :customer)
                returning id
                """
            ),
            {
                "num": order_no,
                "line_id": str(line["id"]),
                "sku": line["sku"],
                "units": units,
                "customer": (customer_name or "").strip() or None,
            },
        )
    ).scalar_one()

    await conn.execute(
        text(
            """
            insert into order_no_scans
                (invoice_id, raw_text, parsed_order_no, confidence, source, was_corrected,
                 scanned_by)
            values
                (:invoice_id, :raw_text, :parsed, :confidence, :source, :was_corrected, :who)
            """
        ),
        {
            "invoice_id": str(invoice_id),
            "raw_text": raw_text,
            "parsed": order_no,
            "confidence": confidence,
            "source": source,
            "was_corrected": was_corrected,
            "who": actor_id,
        },
    )

    return await get_invoice(conn, invoice_id)


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
    """Resolve the scanned code to an invoice.

    Tries a carton sticker first — the printed, unique QR admin issues per
    invoice (0020/0021) — and falls back to matching the raw invoice number
    directly, the same "QR first, human-readable text as fallback" shape every
    other sticker in this system has. A code that turns out to be a box or unit
    sticker is refused with a specific message rather than "not found".
    """
    code = invoice_number.strip().upper()

    sticker = (
        await conn.execute(
            text("select sticker_type::text as sticker_type, status::text as status, "
                 "invoice_id from stickers where code = :code"),
            {"code": code},
        )
    ).mappings().first()

    if sticker is not None and sticker["sticker_type"] != "carton":
        raise AppError(
            f"{code} is a {sticker['sticker_type']} sticker, not a carton sticker.",
            code="wrong_sticker_type",
            http_status=422,
            hint="Scan the carton sticker, or type the invoice number.",
        )

    if sticker is not None and sticker["status"] == "void":
        raise AppError(
            "That carton sticker has been voided.",
            code="sticker_void",
            http_status=409,
            hint="Ask Admin to reissue it.",
        )

    if sticker is not None:
        row = (
            await conn.execute(
                text(_INVOICE_SELECT + " where invoice_id = :id"),
                {"id": str(sticker["invoice_id"])},
            )
        ).mappings().first()
    else:
        row = (
            await conn.execute(
                text(_INVOICE_SELECT + " where upper(invoice_number) = :num"),
                {"num": code},
            )
        ).mappings().first()

    if row is None:
        raise AppError(
            f"No invoice with number {code}.",
            code="unknown_invoice",
            http_status=404,
            hint="Check the number on the invoice sheet, or scan its carton sticker.",
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


async def get_invoice_by_order_no(conn: AsyncConnection, order_no: str) -> Dict[str, Any]:
    """Find the invoice already booked against this Order No.

    The reverse of `record_order_no`, for the case where a matcher has the challan
    but not the invoice: OCR reads the Order No off the page and this says which
    invoice it belongs to.

    Two failure modes are reported separately on purpose, because the operator's
    next action differs. *No* invoice carries this Order No — the usual case for a
    challan whose number has not been captured yet — means "scan the invoice
    instead". *Several* do, which the schema permits deliberately (0015 declined to
    make order_no unique), means a human has to choose and the machine must not.
    """
    order_no = order_no.strip().upper()
    if not ORDER_NO_RE.match(order_no):
        raise AppError(
            f"{order_no} is not a valid Order No.",
            code="bad_order_no",
            http_status=422,
            hint=ORDER_NO_HINT,
        )

    rows = (
        await conn.execute(
            text(_INVOICE_SELECT + " where order_no = :order_no order by invoice_number"),
            {"order_no": order_no},
        )
    ).mappings().all()

    if not rows:
        raise AppError(
            f"No invoice is booked against order {order_no}.",
            code="unknown_order_no",
            http_status=404,
            hint="Scan or type the invoice number instead, then read the Order No.",
        )

    if len(rows) > 1:
        raise AppError(
            f"{len(rows)} invoices are booked against order {order_no}.",
            code="ambiguous_order_no",
            http_status=409,
            hint="Scan the invoice itself so the right one is picked.",
        )

    return dict(rows[0])


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
    """CONTROL POINT 5, first half: the matcher confirms product against invoice.

    Refused (by `fn_matching_units_complete`, 0024) unless every unit sticker
    for this invoice has already been scanned via `/invoices/{id}/match-scan` —
    the database raises a check_violation the global error handler turns into
    a 409, the same way `pack_invoice` below relies on `fn_packing_units_complete`
    for its own units-complete check.
    """
    invoice = await lookup_for_matching(conn, invoice_number)
    badge = await resolve_badge(conn, badge_code, ["invoice_matcher", "packer", "admin"])

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
            f"Invoice {invoice['invoice_number']} has not been matched "
            "(CONTROL POINT 5).",
            code="control_point_failed",
            http_status=409,
            hint="Admin must scan the invoice and their badge first.",
        )

    badge = await resolve_badge(conn, badge_code, ["packer"])

    if str(badge["profile_id"]) == str(invoice["verified_by"]):
        raise AppError(
            "The person who matched the invoice and the packer must be "
            "different people (CONTROL POINT 5).",
            code="control_point_failed",
            http_status=409,
            hint=f"{invoice['verified_by_name']} matched this invoice.",
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


# ---------------------------------------------------------------------------
# Packing assignment and product-box scanning (Phase 5)
# ---------------------------------------------------------------------------


async def assign_invoice(
    conn: AsyncConnection,
    invoice_number: str,
    badge_code: str,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """Assign a carton to a packer by scanning her badge card.

    Reads a card that is physically present, which is what `resolve_badge_holder`
    has always been for — so this needs no relaxation of the rule in
    DECISIONS.md §CC2 that nobody can *look up* a badge code. Physical custody of
    the card is the control, exactly as it is when a packer presents her own.

    Every refusal below is also enforced by `fn_packing_assignment_guard`. These
    checks exist to produce the sentence the lead needs to read, not to be the
    boundary — see DECISIONS.md §B3.
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
            f"Invoice {invoice['invoice_number']} has not been matched yet.",
            code="control_point_failed",
            http_status=409,
            hint="Admin must scan the invoice and their badge first.",
        )

    badge = await resolve_badge(conn, badge_code, ["packer"])

    if str(badge["profile_id"]) == str(invoice["verified_by"]):
        raise AppError(
            f"{badge['full_name']} matched this invoice and cannot also pack it "
            "(CONTROL POINT 5).",
            code="control_point_failed",
            http_status=409,
            hint="Assign it to someone else.",
        )

    await conn.execute(
        text(
            """
            insert into packing_assignments (invoice_id, assigned_to, assigned_by, note)
            values (:invoice_id, :to, auth.uid(), :note)
            """
        ),
        {"invoice_id": str(invoice["invoice_id"]), "to": str(badge["profile_id"]), "note": note},
    )

    state = await packing_state(conn, invoice["invoice_id"])
    return {
        "invoice": await get_invoice(conn, invoice["invoice_id"]),
        "packing": state,
        "assigned_to": badge,
        "message": (
            f"{invoice['invoice_number']} assigned to {badge['full_name']} — "
            f"{state['required_units']} product boxes to scan."
        ),
    }


async def packing_state(conn: AsyncConnection, invoice_id: UUID) -> Dict[str, Any]:
    """How far along this carton is: assigned to whom, how many boxes are in."""
    row = (
        await conn.execute(
            text(
                """
                select invoice_id, invoice_number, sku, required_units, packed_units,
                       remaining_units, ready_to_close, is_open,
                       verified_by, verified_by_name,
                       assigned_to, assigned_to_name,
                       packed_by, packed_by_name, packed_at
                  from v_invoice_packing where invoice_id = :id
                """
            ),
            {"id": str(invoice_id)},
        )
    ).mappings().first()

    if row is None:
        raise AppError("That invoice does not exist.", code="not_found", http_status=404)
    return dict(row)


async def scan_product_box(
    conn: AsyncConnection, invoice_id: UUID, scan: ScanIn
) -> Dict[str, Any]:
    """Scan one product box into a carton.

    The refusal path is deliberately the same as every other scanning step: the
    scan is *recorded* with a reason rather than raised, because a rejected scan
    is evidence and because a packing bench doing 200 of these an hour needs the
    reject to be a line on the screen, not an exception.
    """
    state = await packing_state(conn, invoice_id)

    if state["packed_at"] is not None:
        raise AppError(
            f"Invoice {state['invoice_number']} is already packed.",
            code="already_packed",
            http_status=409,
        )

    result = await scans.record_scan(conn, scan, "pack_unit", invoice_id=invoice_id)
    after = await packing_state(conn, invoice_id)

    result["packed_units"] = after["packed_units"]
    result["required_units"] = after["required_units"]
    result["remaining_units"] = after["remaining_units"]
    result["ready_to_close"] = after["ready_to_close"]
    return result


async def matching_state(conn: AsyncConnection, invoice_id: UUID) -> Dict[str, Any]:
    """How many units have been scanned to confirm product-in-hand at matching."""
    row = (
        await conn.execute(
            text(
                """
                select invoice_id, invoice_number, sku, required_units, matched_units,
                       remaining_units, ready_to_verify, is_open,
                       verified_by, verified_by_name
                  from v_invoice_matching where invoice_id = :id
                """
            ),
            {"id": str(invoice_id)},
        )
    ).mappings().first()

    if row is None:
        raise AppError("That invoice does not exist.", code="not_found", http_status=404)
    return dict(row)


async def scan_matching_unit(
    conn: AsyncConnection, invoice_id: UUID, scan: ScanIn
) -> Dict[str, Any]:
    """Scan one unit sticker to confirm product-in-hand before matching.

    Additive to `scan_product_box` above (CG3) — a second, independent check at
    an earlier step. Same refusal shape: a rejected scan is recorded with a
    reason rather than raised.
    """
    state = await matching_state(conn, invoice_id)

    if state["verified_by"] is not None:
        raise AppError(
            f"Invoice {state['invoice_number']} was already verified by "
            f"{state['verified_by_name']}.",
            code="already_verified",
            http_status=409,
        )

    result = await scans.record_scan(conn, scan, "match_unit", invoice_id=invoice_id)
    after = await matching_state(conn, invoice_id)

    result["matched_units"] = after["matched_units"]
    result["required_units"] = after["required_units"]
    result["remaining_units"] = after["remaining_units"]
    result["ready_to_verify"] = after["ready_to_verify"]
    return result


async def assigned_to_me(conn: AsyncConnection) -> List[Dict[str, Any]]:
    """The packer's own queue. Nobody else's work appears here."""
    rows = await conn.execute(
        text(
            """
            select invoice_id, invoice_number, sku, required_units, packed_units,
                   remaining_units, ready_to_close,
                   assigned_to, assigned_to_name, verified_by_name
              from v_invoice_packing
             where assigned_to = auth.uid()
               and packed_at is null
               and is_open
             order by invoice_number
            """
        )
    )
    return [dict(r) for r in rows.mappings()]
