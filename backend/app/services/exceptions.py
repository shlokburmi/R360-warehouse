"""Exception management (PRD §5.9) and inbound reconciliation (CONTROL POINT 4)."""

import json
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import get_settings
from app.core.errors import AppError
from app.schemas.warehouse import ExceptionCreate, ReconcileIn
from app.services import notifications

_SELECT = """
    select e.id, e.exception_code, e.exception_type::text as exception_type,
           e.status::text as status, e.title, e.details,
           e.gate_entry_id, ge.entry_code,
           e.box_id, b.box_number,
           v.name as vendor_name, po.po_number,
           rp.full_name as reported_by_name, e.reported_at,
           e.escalated_at,
           e.resolution::text as resolution, e.resolution_note,
           sp.full_name as resolved_by_name, e.resolved_at
      from exceptions e
      left join gate_entries ge on ge.id = e.gate_entry_id
      left join boxes b on b.id = e.box_id
      left join vendors v on v.id = e.vendor_id
      left join purchase_orders po on po.id = e.purchase_order_id
      left join profiles rp on rp.id = e.reported_by
      left join profiles sp on sp.id = e.resolved_by
"""


async def list_exceptions(
    conn: AsyncConnection,
    *,
    status_filter: Optional[List[str]] = None,
    limit: int = 100,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    clause = ""
    params: Dict[str, Any] = {"limit": limit, "offset": offset}

    if status_filter:
        clause = " where e.status::text = any(cast(:statuses as text[])) "
        params["statuses"] = status_filter

    rows = await conn.execute(
        text(_SELECT + clause + " order by e.reported_at desc limit :limit offset :offset"),
        params,
    )
    return [dict(r) for r in rows.mappings()]


async def get_exception(conn: AsyncConnection, exception_id: UUID) -> Dict[str, Any]:
    row = (
        await conn.execute(text(_SELECT + " where e.id = :id"), {"id": str(exception_id)})
    ).mappings().first()

    if row is None:
        raise AppError("Exception not found.", code="not_found", http_status=404)
    return dict(row)


async def create_exception(conn: AsyncConnection, payload: ExceptionCreate) -> Dict[str, Any]:
    """Anyone on the floor can raise one. The person who finds the problem
    reports it — routing it through a supervisor first is how problems get
    quietly unfound."""
    row = (
        await conn.execute(
            text(
                """
                insert into exceptions
                  (exception_type, gate_entry_id, box_id, purchase_order_id, vendor_id,
                   title, details, reported_by)
                select cast(:etype as exception_type), :entry_id, :box_id,
                       ge.purchase_order_id, ge.vendor_id,
                       :title, cast(:details as jsonb), auth.uid()
                  from gate_entries ge
                 where ge.id = :entry_id
                returning id, exception_code
                """
            ),
            {
                "etype": payload.exception_type,
                "entry_id": str(payload.gate_entry_id) if payload.gate_entry_id else None,
                "box_id": str(payload.box_id) if payload.box_id else None,
                "title": payload.title,
                "details": json.dumps(payload.details),
            },
        )
    ).mappings().first()

    if row is None:
        raise AppError(
            "An exception must be attached to a gate entry.",
            code="entry_required",
            http_status=422,
        )

    await notifications.notify_ops(
        conn,
        title=payload.title,
        body="A new exception has been raised and needs your decision.",
        payload=payload.details,
        gate_entry_id=payload.gate_entry_id,
        exception_id=row["id"],
    )

    return await get_exception(conn, row["id"])


async def resolve_exception(
    conn: AsyncConnection, exception_id: UUID, resolution: str, note: str
) -> Dict[str, Any]:
    """Admin decides. The consequences for the box are applied by
    fn_apply_exception_resolution, inside this same transaction."""
    current = await get_exception(conn, exception_id)

    if current["status"] == "resolved":
        raise AppError(
            f"{current['exception_code']} was already resolved "
            f"by {current['resolved_by_name']}.",
            code="already_resolved",
            http_status=409,
        )

    box_resolutions = {"accept_short", "recount", "reject_box"}
    if current["box_id"] is None and resolution in box_resolutions:
        raise AppError(
            f"'{resolution}' only applies to an exception raised against a box.",
            code="wrong_resolution",
            http_status=422,
        )
    if current["box_id"] is not None and resolution not in box_resolutions:
        raise AppError(
            "Choose one of: accept short, recount, or reject box.",
            code="wrong_resolution",
            http_status=422,
            hint="A box-level exception needs a decision about the goods.",
        )

    await conn.execute(
        text(
            """
            update exceptions
               set status = 'resolved',
                   resolution = cast(:resolution as exception_resolution),
                   resolution_note = :note,
                   resolved_by = auth.uid(),
                   resolved_at = now()
             where id = :id
            """
        ),
        {"resolution": resolution, "note": note, "id": str(exception_id)},
    )

    resolved = await get_exception(conn, exception_id)

    if resolved["gate_entry_id"]:
        await notifications.notify(
            conn,
            title=f"{resolved['exception_code']} resolved: {resolution.replace('_', ' ')}",
            body=note,
            recipient_role="packer",
            gate_entry_id=resolved["gate_entry_id"],
            exception_id=exception_id,
        )

    return resolved


async def escalate_exception(
    conn: AsyncConnection, exception_id: UUID, email_superadmin: bool, note: Optional[str]
) -> Dict[str, Any]:
    """PRD §5.9 'EMAIL SUPERADMIN'. Escalating does not resolve anything — the
    goods stay held until someone decides."""
    exc = await get_exception(conn, exception_id)

    if exc["status"] == "resolved":
        raise AppError(
            "This exception is already resolved.", code="already_resolved", http_status=409
        )

    await conn.execute(
        text(
            """
            update exceptions
               set status = 'escalated', escalated_at = now(), escalated_to = auth.uid()
             where id = :id
            """
        ),
        {"id": str(exception_id)},
    )

    body = (
        f"Exception: {exc['exception_code']} ({exc['exception_type'].replace('_', ' ')})\n"
        f"Vendor: {exc['vendor_name'] or '—'}\n"
        f"PO: {exc['po_number'] or '—'}\n"
        f"Gate entry: {exc['entry_code'] or '—'}\n"
        f"Issue: {exc['title']}\n"
        f"Reported by: {exc['reported_by_name']} at {exc['reported_at']:%d %b %Y %H:%M}\n"
    )
    if note:
        body += f"\nOps note: {note}\n"

    await notifications.notify_admin(
        conn,
        title=f"Escalated: {exc['title']}",
        body=body,
        gate_entry_id=exc["gate_entry_id"],
        exception_id=exception_id,
    )

    if email_superadmin:
        settings = get_settings()
        if not settings.superadmin_email:
            raise AppError(
                "No superadmin email is configured.",
                code="no_superadmin_email",
                http_status=503,
                hint="Set SUPERADMIN_EMAIL in the backend environment.",
            )
        await notifications.notify(
            conn,
            title=f"[Warehouse] Escalated exception {exc['exception_code']}",
            body=body,
            recipient_role="admin",
            channel="email",
            payload={"to": settings.superadmin_email},
            exception_id=exception_id,
        )

    return await get_exception(conn, exception_id)


# ---------------------------------------------------------------------------
# CONTROL POINT 4 — inbound reconciliation
# ---------------------------------------------------------------------------


async def reconciliation_view(conn: AsyncConnection, entry_id: UUID) -> Dict[str, Any]:
    """What the inbound team compares against.

    The warehouse figure is derived from the scan ledger rather than stored, so
    there is nothing to drift and nothing to edit.
    """
    rows = await conn.execute(
        text(
            """
            select pol.id as purchase_order_line_id, pol.sku, pol.description,
                   pol.expected_units,
                   coalesce(wc.total_units, 0)::int as warehouse_count,
                   ir.inbound_count, ir.matched
              from gate_entries ge
              join purchase_order_lines pol on pol.purchase_order_id = ge.purchase_order_id
              left join (
                    select purchase_order_line_id, sum(total_units) as total_units
                      from v_warehouse_counts
                     where gate_entry_id = :id
                     group by purchase_order_line_id
              ) wc on wc.purchase_order_line_id = pol.id
              left join inbound_reconciliations ir
                     on ir.purchase_order_line_id = pol.id and ir.gate_entry_id = ge.id
             where ge.id = :id
             order by pol.line_no
            """
        ),
        {"id": str(entry_id)},
    )

    lines = [dict(r) for r in rows.mappings()]
    all_matched = bool(lines) and all(line["matched"] is True for line in lines)

    if not lines:
        message = "No purchase order lines to reconcile."
    elif all_matched:
        message = "Counts match — ready for putaway"
    elif any(line["inbound_count"] is None for line in lines):
        message = "Waiting for the inbound team's counts."
    else:
        message = "Counts do not match. Cannot proceed to putaway."

    return {
        "gate_entry_id": entry_id,
        "lines": lines,
        "all_matched": all_matched,
        "message": message,
    }


async def reconcile(
    conn: AsyncConnection, entry_id: UUID, payload: ReconcileIn
) -> Dict[str, Any]:
    """Inbound team enters their own count. A mismatch blocks putaway and raises
    an exception; it is never silently reconciled to the warehouse figure."""
    view = await reconciliation_view(conn, entry_id)
    warehouse_by_line = {str(l["purchase_order_line_id"]): l for l in view["lines"]}

    for line in payload.lines:
        key = str(line.purchase_order_line_id)
        if key not in warehouse_by_line:
            raise AppError(
                "That PO line does not belong to this gate entry.",
                code="invalid_reference",
                http_status=422,
            )

        await conn.execute(
            text(
                """
                insert into inbound_reconciliations
                  (gate_entry_id, purchase_order_line_id, warehouse_count, inbound_count, verified_by)
                values (:entry_id, :line_id, :warehouse, :inbound, auth.uid())
                on conflict (gate_entry_id, purchase_order_line_id) do update
                   set warehouse_count = excluded.warehouse_count,
                       inbound_count = excluded.inbound_count,
                       verified_by = excluded.verified_by,
                       verified_at = now()
                """
            ),
            {
                "entry_id": str(entry_id),
                "line_id": key,
                "warehouse": warehouse_by_line[key]["warehouse_count"],
                "inbound": line.inbound_count,
            },
        )

    result = await reconciliation_view(conn, entry_id)
    mismatched = [l for l in result["lines"] if l["matched"] is False]

    if mismatched:
        detail_lines = ", ".join(
            f"{l['sku']}: warehouse {l['warehouse_count']} vs inbound {l['inbound_count']}"
            for l in mismatched
        )
        row = (
            await conn.execute(
                text(
                    """
                    insert into exceptions
                      (exception_type, gate_entry_id, purchase_order_id, vendor_id,
                       title, details, reported_by)
                    select 'inbound_mismatch', ge.id, ge.purchase_order_id, ge.vendor_id,
                           :title, cast(:details as jsonb), auth.uid()
                      from gate_entries ge where ge.id = :entry_id
                    returning id, exception_code
                    """
                ),
                {
                    "title": f"Inbound count mismatch on {len(mismatched)} line(s)",
                    "details": json.dumps(
                        {
                            "lines": [
                                {
                                    "sku": l["sku"],
                                    "warehouse_count": l["warehouse_count"],
                                    "inbound_count": l["inbound_count"],
                                }
                                for l in mismatched
                            ]
                        }
                    ),
                    "entry_id": str(entry_id),
                },
            )
        ).mappings().one()

        await notifications.notify_ops(
            conn,
            title="Inbound count does not match",
            body=detail_lines,
            gate_entry_id=entry_id,
            exception_id=row["id"],
        )

        # Returned rather than raised: the route answers 409 and putaway stays
        # blocked either way, but the exception record must survive the request.
        result["message"] = f"Inbound count doesn't match. {detail_lines}"
        result["exception_code"] = row["exception_code"]
        return result

    if all(l["inbound_count"] is not None for l in result["lines"]):
        await conn.execute(
            text("update gate_entries set status = 'reconciled' where id = :id"),
            {"id": str(entry_id)},
        )
        result["message"] = "Counts match — ready for putaway"

    return result
