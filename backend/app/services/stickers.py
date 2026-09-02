"""Sticker issue — PRD Steps 2 and 3.

Only Admin can issue stickers (enforced by RLS). That separation is what gives
CONTROL POINT 2 its meaning: the guard counts the boxes, Admin issues exactly that
many stickers, and the guard scans them back. Three numbers from two independent
parties. If the floor could print its own stickers, "scanned == issued" would be
a tautology.
"""

import secrets
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.errors import AppError
from app.services import gate, qrcode_util


def _code(prefix: str) -> str:
    """Short, unambiguous, non-sequential sticker code.

    Non-sequential matters: BOX-0001..BOX-0006 would let anyone hand-write a
    seventh valid-looking sticker. 8 hex chars is trivially printable at sticker
    size and not guessable at warehouse scale.
    """
    return f"{prefix}-{secrets.token_hex(4).upper()}"


async def _po_allocation(conn: AsyncConnection, po_id: UUID) -> List[Dict[str, Any]]:
    """Expand PO lines into the boxes they should arrive in.

    30 units at 10/box becomes three boxes of 10. A remainder becomes a partial
    final box: 25 at 10/box is 10 + 10 + 5, not three boxes of "about 8".
    """
    rows = await conn.execute(
        text(
            """
            select id, line_no, sku, description, expected_units, units_per_box
              from purchase_order_lines
             where purchase_order_id = :po_id
             order by line_no
            """
        ),
        {"po_id": str(po_id)},
    )

    allocation: List[Dict[str, Any]] = []
    for line in rows.mappings():
        remaining = line["expected_units"]
        while remaining > 0:
            units = min(remaining, line["units_per_box"])
            allocation.append(
                {
                    "purchase_order_line_id": line["id"],
                    "sku": line["sku"],
                    "description": line["description"],
                    "expected_units": units,
                }
            )
            remaining -= units

    return allocation


async def generate_box_stickers(
    conn: AsyncConnection, entry_id: UUID, reprint_of_id: Optional[UUID] = None
) -> Dict[str, Any]:
    """Issue exactly one sticker per physically counted box (PRD Step 2)."""
    entry = await gate.get_entry(conn, entry_id)

    if entry["status"] != "counting":
        raise AppError(
            "Boxes have not been counted yet.",
            code="wrong_status",
            http_status=409,
            hint="The guard must declare the box count first.",
        )

    declared = entry["declared_box_count"]
    if not declared:
        raise AppError("No box count has been declared.", code="no_box_count", http_status=409)

    if entry["issued_box_sticker_count"] > 0 and reprint_of_id is None:
        raise AppError(
            f"{entry['issued_box_sticker_count']} box stickers have already been issued "
            f"for {entry['entry_code']}.",
            code="already_issued",
            http_status=409,
            hint="Use reprint if the sheet was damaged or lost.",
        )

    if entry["purchase_order_id"] is None:
        raise AppError(
            "This entry has no purchase order, so expected quantities are unknown.",
            code="no_po",
            http_status=422,
            hint="Link a PO to the gate entry before issuing stickers.",
        )

    allocation = await _po_allocation(conn, entry["purchase_order_id"])

    # The guard counted the truck; the PO says what was ordered. A disagreement
    # here is a real discrepancy, not a rounding artefact — so it stops the line
    # and lands in the Admin queue rather than being silently absorbed.
    if len(allocation) != declared:
        # Returned, not raised: logging the discrepancy against the vendor is a
        # write, and raising here would roll it back along with everything else
        # in the request. The route answers 409 and no stickers are issued.
        code = await gate.raise_count_exception(
            conn,
            entry_id=entry_id,
            title=f"Box count does not match PO on {entry['entry_code']}",
            details={
                "declared_box_count": declared,
                "po_expected_boxes": len(allocation),
                "po_number": entry["po_number"],
            },
        )
        return {
            "issued": False,
            "sheet": None,
            "exception_code": code,
            "message": (
                f"Guard counted {declared} boxes but {entry['po_number']} expects "
                f"{len(allocation)}. Count mismatch — contact Admin."
            ),
        }

    sheet_id = (
        await conn.execute(
            text(
                """
                insert into sticker_sheets
                  (gate_entry_id, sticker_type, quantity, generated_by, reprint_of_id)
                values (:entry_id, 'box', :qty, auth.uid(), :reprint_of)
                returning id
                """
            ),
            {
                "entry_id": str(entry_id),
                "qty": declared,
                "reprint_of": str(reprint_of_id) if reprint_of_id else None,
            },
        )
    ).scalar_one()

    for index, alloc in enumerate(allocation, start=1):
        # stickers.box_id and boxes.sticker_id reference each other, so the
        # sticker goes in first with a null box, then the box, then the link.
        sticker_id = (
            await conn.execute(
                text(
                    """
                    insert into stickers
                      (code, sticker_type, sheet_id, gate_entry_id,
                       purchase_order_line_id, expected_units, sequence_no)
                    values (:code, 'box', :sheet_id, :entry_id, :line_id, :units, :seq)
                    returning id
                    """
                ),
                {
                    "code": _code("BOX"),
                    "sheet_id": str(sheet_id),
                    "entry_id": str(entry_id),
                    "line_id": str(alloc["purchase_order_line_id"]),
                    "units": alloc["expected_units"],
                    "seq": index,
                },
            )
        ).scalar_one()

        box_id = (
            await conn.execute(
                text(
                    """
                    insert into boxes
                      (gate_entry_id, sticker_id, box_number,
                       purchase_order_line_id, expected_units)
                    values (:entry_id, :sticker_id, :box_number, :line_id, :units)
                    returning id
                    """
                ),
                {
                    "entry_id": str(entry_id),
                    "sticker_id": str(sticker_id),
                    "box_number": index,
                    "line_id": str(alloc["purchase_order_line_id"]),
                    "units": alloc["expected_units"],
                },
            )
        ).scalar_one()

        await conn.execute(
            text("update stickers set box_id = :box_id, status = 'applied' where id = :id"),
            {"box_id": str(box_id), "id": str(sticker_id)},
        )

    await conn.execute(
        text(
            """
            update gate_entries
               set issued_box_sticker_count = issued_box_sticker_count + :qty
             where id = :id
            """
        ),
        {"qty": declared, "id": str(entry_id)},
    )

    return {
        "issued": True,
        "sheet": await get_sheet(conn, sheet_id),
        "exception_code": None,
        "message": f"{declared} box stickers issued.",
    }


async def generate_unit_stickers(conn: AsyncConnection, entry_id: UUID) -> Dict[str, Any]:
    """One sticker per individual unit, grouped by box (PRD Step 3).

    Each sticker is bound to its box at issue time. That is what lets the
    scanning page know, without the offloader telling it, that unit UNT-3F2A
    belongs to box 4 and counts against box 4's expected quantity.
    """
    entry = await gate.get_entry(conn, entry_id)

    if entry["status"] not in ("box_verified", "offloading"):
        raise AppError(
            "Box verification must be completed before unit stickers are issued.",
            code="wrong_status",
            http_status=409,
            hint=f"Current status: {entry['status'].replace('_', ' ')}.",
        )

    boxes = list(
        (
            await conn.execute(
                text(
                    """
                    select b.id, b.box_number, b.expected_units, b.purchase_order_line_id,
                           (select count(*) from stickers s
                             where s.box_id = b.id and s.sticker_type = 'unit'
                               and s.status <> 'void')::int as issued
                      from boxes b
                     where b.gate_entry_id = :id and b.status in ('verified', 'scanning')
                     order by b.box_number
                    """
                ),
                {"id": str(entry_id)},
            )
        ).mappings()
    )

    pending = [b for b in boxes if b["issued"] < b["expected_units"]]
    if not pending:
        raise AppError(
            "Unit stickers have already been issued for every open box.",
            code="already_issued",
            http_status=409,
        )

    total = sum(b["expected_units"] - b["issued"] for b in pending)

    sheet_id = (
        await conn.execute(
            text(
                """
                insert into sticker_sheets (gate_entry_id, sticker_type, quantity, generated_by)
                values (:entry_id, 'unit', :qty, auth.uid())
                returning id
                """
            ),
            {"entry_id": str(entry_id), "qty": total},
        )
    ).scalar_one()

    seq = 0
    for box in pending:
        for _ in range(box["expected_units"] - box["issued"]):
            seq += 1
            await conn.execute(
                text(
                    """
                    insert into stickers
                      (code, sticker_type, sheet_id, gate_entry_id, box_id,
                       purchase_order_line_id, sequence_no, status)
                    values (:code, 'unit', :sheet_id, :entry_id, :box_id, :line_id, :seq, 'applied')
                    """
                ),
                {
                    "code": _code("UNT"),
                    "sheet_id": str(sheet_id),
                    "entry_id": str(entry_id),
                    "box_id": str(box["id"]),
                    "line_id": str(box["purchase_order_line_id"]),
                    "seq": seq,
                },
            )

    await conn.execute(
        text(
            """
            update gate_entries set status = 'offloading'
             where id = :id and status = 'box_verified'
            """
        ),
        {"id": str(entry_id)},
    )

    return await get_sheet(conn, sheet_id)


async def void_sheet(conn: AsyncConnection, sheet_id: UUID, reason: str) -> int:
    """Void unscanned stickers on a sheet (misprint, jam, lost sheet).

    Scanned stickers are never voided here — a scan that happened is a fact, and
    unwinding it would let a bad count be papered over with a reprint.
    """
    if not reason or not reason.strip():
        raise AppError("A void needs a reason.", code="reason_required", http_status=422)

    result = await conn.execute(
        text(
            """
            update stickers
               set status = 'void', void_reason = :reason
             where sheet_id = :sheet_id and status in ('issued', 'applied')
            returning id
            """
        ),
        {"reason": reason.strip(), "sheet_id": str(sheet_id)},
    )
    return len(list(result))


async def get_sheet(conn: AsyncConnection, sheet_id: UUID) -> Dict[str, Any]:
    sheet = (
        await conn.execute(
            text(
                """
                select ss.id, ss.gate_entry_id, ss.sticker_type::text as sticker_type,
                       ss.quantity, ss.generated_at, p.full_name as generated_by_name
                  from sticker_sheets ss
                  left join profiles p on p.id = ss.generated_by
                 where ss.id = :id
                """
            ),
            {"id": str(sheet_id)},
        )
    ).mappings().first()

    if sheet is None:
        raise AppError("Sticker sheet not found.", code="not_found", http_status=404)

    rows = await conn.execute(
        text(
            """
            select s.id, s.code, s.sticker_type::text as sticker_type, s.status::text as status,
                   s.sequence_no, s.expected_units, s.box_id, b.box_number,
                   pol.sku, pol.description
              from stickers s
              left join boxes b on b.id = s.box_id
              left join purchase_order_lines pol on pol.id = s.purchase_order_line_id
             where s.sheet_id = :id
             order by s.sequence_no
            """
        ),
        {"id": str(sheet_id)},
    )

    out = dict(sheet)
    out["stickers"] = [
        dict(r) | {"qr": qrcode_util.to_data_uri(r["code"], scale=5)}
        for r in rows.mappings()
    ]
    return out


async def list_sheets(conn: AsyncConnection, entry_id: UUID) -> List[Dict[str, Any]]:
    rows = await conn.execute(
        text(
            """
            select ss.id, ss.gate_entry_id, ss.sticker_type::text as sticker_type,
                   ss.quantity, ss.generated_at, p.full_name as generated_by_name
              from sticker_sheets ss
              left join profiles p on p.id = ss.generated_by
             where ss.gate_entry_id = :id
             order by ss.generated_at desc
            """
        ),
        {"id": str(entry_id)},
    )
    return [dict(r) | {"stickers": []} for r in rows.mappings()]


# ---------------------------------------------------------------------------
# Carton stickers (0020/0021) — one per invoice, printed one at a time rather
# than issued as a sheet, since an invoice arrives and is booked one at a time.
# ---------------------------------------------------------------------------


async def issue_carton_sticker(conn: AsyncConnection, invoice_id: UUID) -> Dict[str, Any]:
    """Mint the carton sticker for one invoice.

    "Reissue" means replace, the same rule 0013 sets for badges: any existing
    live sticker for this invoice is voided first, so this never has to refuse
    a second print run — it can always just be pressed again after a smudge or
    a jam, and `stickers_one_live_carton_per_invoice` still only ever has one
    live row to enforce against.
    """
    invoice = (
        await conn.execute(
            text("select id, invoice_number from invoices where id = :id"),
            {"id": str(invoice_id)},
        )
    ).mappings().first()

    if invoice is None:
        raise AppError("Invoice not found.", code="not_found", http_status=404)

    await conn.execute(
        text(
            """
            update stickers
               set status = 'void', void_reason = 'Reissued'
             where invoice_id = :id and sticker_type = 'carton' and status <> 'void'
            """
        ),
        {"id": str(invoice_id)},
    )

    sticker_id = (
        await conn.execute(
            text(
                """
                insert into stickers (code, sticker_type, invoice_id, sequence_no, status)
                values (:code, 'carton', :invoice_id, 1, 'applied')
                returning id
                """
            ),
            {"code": _code("CTN"), "invoice_id": str(invoice_id)},
        )
    ).scalar_one()

    return await get_carton_sticker(conn, sticker_id)


async def get_carton_sticker(conn: AsyncConnection, sticker_id: UUID) -> Dict[str, Any]:
    row = (
        await conn.execute(
            text(
                """
                select s.id, s.code, s.status::text as status, s.invoice_id,
                       i.invoice_number, i.sku, i.units, i.customer_name
                  from stickers s
                  join invoices i on i.id = s.invoice_id
                 where s.id = :id
                """
            ),
            {"id": str(sticker_id)},
        )
    ).mappings().first()

    if row is None:
        raise AppError("Sticker not found.", code="not_found", http_status=404)
    return dict(row)
