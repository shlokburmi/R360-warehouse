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
from app.services import qrcode_util

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


async def my_badge(conn: AsyncConnection) -> Dict[str, str]:
    """The caller's own current badge, rendered as a QR.

    `my_badge_code()` resolves the caller from auth.uid() inside the
    database (0037_self_badge_view.sql), so this can never be pointed at
    someone else's badge. The code itself never leaves this function — only
    the rendered image goes back, same as admin_service.issue_badge, and for
    the same reason: a badge code in a log file is a badge in a log file.
    """
    code = (await conn.execute(text("select my_badge_code()"))).scalar_one_or_none()

    if code is None:
        raise AppError(
            "You do not have an active attribution badge.",
            code="no_badge",
            http_status=404,
            hint="Ask an Admin to issue one.",
        )

    return {"badge_qr": qrcode_util.to_data_uri(code, scale=8)}


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------

_INVOICE_SELECT = """
    select invoice_id, invoice_number, order_no, customer_name,
           is_open, stage,
           assigned_to, assigned_to_name, assigned_by, assigned_by_name, assigned_at,
           packed_by, packed_by_name, packed_at,
           batch_id, batch_code, batch_status, out_scanned_at
      from v_invoice_status
"""


async def create_invoice_from_order_no(
    conn: AsyncConnection,
    *,
    order_no: str,
    actor_id: str,
    source: str = "ocr",
    raw_text: Optional[str] = None,
    confidence: Optional[float] = None,
    was_corrected: bool = False,
) -> Dict[str, Any]:
    """A Packer books an invoice from the Order No she just OCR-scanned off the
    physical invoice (PRD §5.4). There is no separate typed invoice number,
    and no PO/product/quantity — the scanned Order No is the whole invoice;
    what's actually inside the carton is Admin's separate ERP's concern, not
    this app's. The scanned Order No fills both `invoice_number` and
    `order_no`, so the rest of the system (assign, pack, out-scan) keys off
    exactly the value the camera read.

    `order_no` is not unique in the schema (a re-dispatch can legitimately
    share one — see 0015_order_no_ocr.sql), but `invoice_number` is
    required-unique, so scanning the same Order No twice surfaces the
    existing `duplicate_invoice` conflict rather than silently creating a
    second row.

    The `raw_text`/`confidence`/`was_corrected` provenance is logged into
    `order_no_scans` the same way this system has always logged an OCR read
    (0015: "the OCR misread it" needs to be a checkable claim).
    """
    order_no = order_no.strip().upper()
    if not ORDER_NO_RE.match(order_no):
        raise AppError(
            f"{order_no} is not a valid Order No.",
            code="bad_order_no",
            http_status=422,
            hint=ORDER_NO_HINT,
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
                insert into invoices (invoice_number, order_no)
                values (:num, :num)
                returning id
                """
            ),
            {"num": order_no},
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
    """Find the invoice whose Order No this is.

    Every invoice created via `create_invoice_from_order_no` has `order_no ==
    invoice_number`, so this is effectively a lookup by invoice number under
    a different name — kept separate because the caller (a fresh OCR read)
    doesn't know in advance whether the invoice already exists.

    Two failure modes are reported separately on purpose, because the operator's
    next action differs. *No* invoice carries this Order No means it hasn't been
    created yet — the caller should create it. *Several* do — only possible for
    a legacy invoice from before this Order-No-is-the-number design, since the
    schema itself does not enforce `order_no` uniqueness (0015) — means a human
    has to choose and the machine must not.
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


async def pack_invoice(
    conn: AsyncConnection,
    invoice_number: str,
    badge_code: str,
    carton_code: Optional[str] = None,
) -> Dict[str, Any]:
    """CONTROL POINT 5, second half: the packer's badge is bound to the invoice.

    The database refuses if the invoice has not been assigned to anyone, or
    if the packer is the same person who assigned it — assigning now stands
    in for the "verify" step this used to require (0036: there is no more
    product/quantity confirmation, so the assignment is the only remaining
    record of a second person having handled it before packing).
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

    if invoice["assigned_by"] is None:
        raise AppError(
            f"Invoice {invoice['invoice_number']} has not been assigned to "
            "anyone yet (CONTROL POINT 5).",
            code="control_point_failed",
            http_status=409,
            hint="Scan the invoice and assign it to a packer first.",
        )

    badge = await resolve_badge(conn, badge_code, ["packer"])

    if str(badge["profile_id"]) == str(invoice["assigned_by"]):
        raise AppError(
            "The person who assigned the invoice and the packer must be "
            "different people (CONTROL POINT 5).",
            code="control_point_failed",
            http_status=409,
            hint=f"{invoice['assigned_by_name']} assigned this invoice.",
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
            select i.id as invoice_id, i.invoice_number, i.customer_name,
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
                   round(
                     extract(epoch from (max(pr.packed_at) - min(pr.packed_at)))
                     / 60.0 / nullif(count(pr.id) - 1, 0), 1
                   ) as avg_minutes_per_carton
              from packing_records pr
              join profiles p on p.id = pr.packed_by
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
    actor_id: str,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """Assign a carton to a packer by scanning her badge card.

    Reads a card that is physically present, which is what `resolve_badge_holder`
    has always been for — so this needs no relaxation of the rule in
    DECISIONS.md §CC2 that nobody can *look up* a badge code. Physical custody of
    the card is the control, exactly as it is when a packer presents her own.

    Assigning now stands in for the old "verify" step (0036) — there is no
    product/quantity confirmation left, so this is the one act that
    establishes a second person before packing can happen.

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

    badge = await resolve_badge(conn, badge_code, ["packer"])

    if str(badge["profile_id"]) == str(actor_id):
        raise AppError(
            "You cannot assign this invoice to yourself (CONTROL POINT 5).",
            code="control_point_failed",
            http_status=409,
            hint="Packing must be a second person. Assign it to someone else.",
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
        "message": f"{invoice['invoice_number']} assigned to {badge['full_name']}.",
    }


async def packing_state(conn: AsyncConnection, invoice_id: UUID) -> Dict[str, Any]:
    """How far along this carton is: assigned to whom, packed or not."""
    row = (
        await conn.execute(
            text(
                """
                select invoice_id, invoice_number, is_open,
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


async def assigned_to_me(conn: AsyncConnection) -> List[Dict[str, Any]]:
    """The packer's own queue. Nobody else's work appears here."""
    rows = await conn.execute(
        text(
            """
            select invoice_id, invoice_number, is_open,
                   assigned_to, assigned_to_name, packed_by, packed_by_name, packed_at
              from v_invoice_packing
             where assigned_to = auth.uid()
               and packed_at is null
               and is_open
             order by invoice_number
            """
        )
    )
    return [dict(r) for r in rows.mappings()]
