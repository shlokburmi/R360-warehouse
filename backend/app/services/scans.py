"""Scanning — PRD Steps 2 and 3, CONTROL POINTS 2 and 3.

The rules about what a scan means live in the database (fn_scan_resolve in
0004_control_points.sql). This module's job is the surrounding behaviour: batch
replay from an offline device, savepoints so one bad scan doesn't poison a
batch, and turning a `reject_reason` enum into a sentence someone can act on
while holding a scanner in one hand.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import get_settings
from app.core.errors import AppError
from app.schemas.warehouse import ScanIn
from app.services import gate, notifications

# What the person holding the scanner should do about it.
REJECT_MESSAGES = {
    "unknown_code": "Code not recognised. Check you scanned a warehouse label.",
    "wrong_sticker_type": "That is the wrong kind of sticker for this step.",
    "wrong_gate_entry": "That sticker belongs to a different truck.",
    "already_scanned": "Already scanned.",
    "box_not_open": "This box is not open for scanning right now.",
    "over_expected_quantity": "This box is already full. Extra unit — set it aside and call Admin.",
    "sticker_void": "This sticker was voided. Use the reissued sheet.",
    # Out-scan (Phase 3)
    "not_packed": "This carton has not been packed yet. It cannot be released.",
    "not_in_batch": "This carton is not in a batch. Add it to a batch first.",
    "batch_closed": "That batch is already closed. Nothing more can be scanned into it.",
    # Gate exit (Phase 4)
    "batch_not_released": "This carton has not been released for pickup. Do not load it.",
    "no_pickup_registered": "No vehicle has been registered for this batch yet.",
    "wrong_pickup": "That pickup is already closed.",
    # Packing a product box into a carton (Phase 5)
    "wrong_invoice": "This product does not belong to that invoice. Check the paperwork.",
    "unit_not_in_stock": "This product box was never counted in at offloading. Call Admin.",
    "invoice_already_full": "This carton is already full. Start the next one.",
    # Matching-stage unit scan (Phase 5, additive)
    "invoice_already_matched": "Every unit for this invoice is already confirmed. Scan your badge to verify.",
}

_INSERT_SCAN = text(
    """
    insert into scan_events
      (client_event_id, scan_type, raw_code, gate_entry_id, box_id, invoice_id,
       accepted, scanned_by, scanned_at, was_offline, device_label, disposition)
    values
      (:client_event_id, :scan_type, :raw_code, :gate_entry_id, :box_id,
       cast(:invoice_id as uuid),
       false, auth.uid(), :scanned_at, :was_offline, :device_label,
       cast(:disposition as unit_disposition))
    returning id, accepted, reject_reason::text as reject_reason,
              box_id, sticker_id, invoice_id
    """
)


def _clamp_scanned_at(scanned_at: datetime) -> datetime:
    """Trust the device clock, within reason.

    A phone that has been offline in a warehouse basement can be minutes out;
    one whose clock was never set can be years out. Anything beyond the
    tolerance is clamped to now rather than rejected — refusing the scan would
    lose real work over a wrong clock, and `recorded_at` preserves the truth.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)

    if scanned_at.tzinfo is None:
        scanned_at = scanned_at.replace(tzinfo=timezone.utc)

    tolerance = timedelta(hours=settings.scan_backdate_tolerance_hours)
    if scanned_at > now + timedelta(minutes=5) or scanned_at < now - tolerance:
        return now
    return scanned_at


async def _existing_scan(conn: AsyncConnection, client_event_id: UUID) -> Optional[Dict[str, Any]]:
    row = (
        await conn.execute(
            text(
                """
                select se.id, se.accepted, se.reject_reason::text as reject_reason,
                       se.box_id, b.box_number, b.scanned_units, b.expected_units
                  from scan_events se
                  left join boxes b on b.id = se.box_id
                 where se.client_event_id = :cid
                """
            ),
            {"cid": str(client_event_id)},
        )
    ).mappings().first()
    return dict(row) if row else None


async def record_scan(
    conn: AsyncConnection,
    scan: ScanIn,
    scan_type: str,
    invoice_id: Optional[UUID] = None,
) -> Dict[str, Any]:
    """Record one scan. Idempotent on `client_event_id`.

    Returns a result dict rather than raising on rejection: a rejected scan is a
    normal, expected outcome that the operator needs to see and act on, not an
    error condition. Rejections are recorded too — "the scanner didn't work" is
    then a checkable claim.

    `invoice_id` is only used by `pack_unit`, where the carton being filled
    cannot be derived from the scanned code — a product sticker knows which box
    it arrived in, not which order it is going out on. Every other scan type
    leaves it null and lets the resolver work it out.
    """
    prior = await _existing_scan(conn, scan.client_event_id)
    if prior is not None:
        # Offline replay of something already accepted. Report success so the
        # device clears it from the queue.
        return {
            "client_event_id": scan.client_event_id,
            "accepted": prior["accepted"],
            "duplicate": True,
            "reject_reason": prior["reject_reason"],
            "message": "Already recorded." if prior["accepted"]
            else REJECT_MESSAGES.get(prior["reject_reason"] or "", "Scan was rejected."),
            "box_id": prior["box_id"],
            "box_number": prior["box_number"],
            "scanned_units": prior["scanned_units"],
            "expected_units": prior["expected_units"],
        }

    params = {
        "client_event_id": str(scan.client_event_id),
        "scan_type": scan_type,
        "raw_code": scan.raw_code,
        "gate_entry_id": None,   # resolved from the sticker by the trigger
        "box_id": None,
        "invoice_id": str(invoice_id) if invoice_id else None,
        "scanned_at": _clamp_scanned_at(scan.scanned_at),
        "was_offline": scan.was_offline,
        "device_label": scan.device_label,
        "disposition": scan.disposition,
    }

    # Savepoint: a constraint violation on this one scan (two devices racing on
    # the same sticker) must not roll back the rest of a 200-scan batch.
    try:
        async with conn.begin_nested():
            row = (await conn.execute(_INSERT_SCAN, params)).mappings().one()
    except IntegrityError:
        return {
            "client_event_id": scan.client_event_id,
            "accepted": False,
            "duplicate": True,
            "reject_reason": "already_scanned",
            "message": REJECT_MESSAGES["already_scanned"],
        }

    box = None
    if row["box_id"]:
        box = (
            await conn.execute(
                text(
                    """
                    select box_number, scanned_units, expected_units, status::text as status
                      from boxes where id = :id
                    """
                ),
                {"id": str(row["box_id"])},
            )
        ).mappings().first()

    accepted = row["accepted"]
    reason = row["reject_reason"]

    if accepted:
        message = "Scanned."
        if box is not None and scan_type == "unit_verify":
            message = f"Box {box['box_number']}: {box['scanned_units']} of {box['expected_units']}"
        elif scan_type in ("out_scan", "gate_exit"):
            # Carton progress is per batch, so report the batch position — that is
            # the number the person releasing the truck actually needs. The two
            # steps count different columns on the same set of cartons.
            column = "out_scanned_at" if scan_type == "out_scan" else "exit_scanned_at"
            progress = (
                await conn.execute(
                    text(
                        f"""
                        select b.batch_code,
                               count(pr.id)::int as total,
                               count(pr.{column})::int as scanned
                          from packing_records pr
                          join batches b on b.id = pr.batch_id
                         where pr.batch_id = (
                                 select batch_id from packing_records
                                  where invoice_id = :invoice_id
                               )
                         group by b.batch_code
                        """
                    ),
                    {"invoice_id": str(row["invoice_id"])},
                )
            ).mappings().first()

            if progress is not None:
                message = (
                    f"{progress['batch_code']}: {progress['scanned']} of "
                    f"{progress['total']} cartons"
                )
    else:
        message = REJECT_MESSAGES.get(reason or "", "Scan was rejected.")

    return {
        "client_event_id": scan.client_event_id,
        "accepted": accepted,
        "duplicate": False,
        "reject_reason": reason,
        "message": message,
        "box_id": row["box_id"],
        "box_number": box["box_number"] if box else None,
        "scanned_units": box["scanned_units"] if box else None,
        "expected_units": box["expected_units"] if box else None,
    }


async def record_batch(
    conn: AsyncConnection,
    scans: List[ScanIn],
    scan_type: str,
    invoice_id: Optional[UUID] = None,
) -> Dict[str, Any]:
    """Drain an offline queue. Replayed in the order the scans happened.

    `invoice_id` applies to `pack_unit` only, and the whole group shares it —
    the device queues one carton's product boxes together because the carton is
    what the count is against.
    """
    ordered = sorted(scans, key=lambda s: s.scanned_at)
    results = [
        await record_scan(conn, s, scan_type, invoice_id=invoice_id) for s in ordered
    ]

    return {
        "results": results,
        "accepted_count": sum(1 for r in results if r["accepted"] and not r["duplicate"]),
        "rejected_count": sum(1 for r in results if not r["accepted"]),
        "duplicate_count": sum(1 for r in results if r["duplicate"]),
    }


# ---------------------------------------------------------------------------
# Progress readouts
# ---------------------------------------------------------------------------


async def box_scan_progress(conn: AsyncConnection, entry_id: UUID) -> Dict[str, Any]:
    row = (
        await conn.execute(
            text(
                """
                select count(*)::int as total,
                       count(*) filter (where status <> 'pending')::int as scanned
                  from boxes where gate_entry_id = :id
                """
            ),
            {"id": str(entry_id)},
        )
    ).mappings().one()

    total, scanned = row["total"], row["scanned"]
    remaining = total - scanned
    complete = total > 0 and remaining == 0

    if total == 0:
        message = "Waiting for Admin to issue the sticker sheet."
    elif complete:
        message = "All boxes verified — move to next step"
    else:
        message = f"{scanned} of {total} boxes scanned · {remaining} remaining"

    return {
        "total": total,
        "scanned": scanned,
        "remaining": remaining,
        "complete": complete,
        "message": message,
    }


async def unit_scan_progress(conn: AsyncConnection, entry_id: UUID) -> Dict[str, Any]:
    row = (
        await conn.execute(
            text(
                """
                select coalesce(sum(expected_units), 0)::int as total,
                       coalesce(sum(scanned_units), 0)::int as scanned,
                       count(*) filter (where status = 'held')::int as held,
                       count(*) filter (where status in ('complete','short_accepted','rejected','emptied'))::int as closed,
                       count(*)::int as boxes
                  from boxes
                 where gate_entry_id = :id and status <> 'rejected'
                """
            ),
            {"id": str(entry_id)},
        )
    ).mappings().one()

    total, scanned = row["total"], row["scanned"]
    remaining = total - scanned
    complete = row["boxes"] > 0 and row["closed"] == row["boxes"]

    if row["held"]:
        message = f"{row['held']} box(es) held — Admin decision needed"
    elif complete:
        message = "All units scanned and boxes closed"
    else:
        message = f"{scanned} of {total} units scanned · {remaining} remaining"

    return {
        "total": total,
        "scanned": scanned,
        "remaining": remaining,
        "complete": complete,
        "message": message,
    }


async def list_boxes(conn: AsyncConnection, entry_id: UUID) -> List[Dict[str, Any]]:
    rows = await conn.execute(
        text(
            """
            select b.id, b.box_number, b.status::text as status, s.code as sticker_code,
                   b.expected_units, b.scanned_units, b.quarantined_units,
                   pol.sku, pol.description,
                   b.damage_level::text as damage_level, b.damage_note,
                   b.verified_at, b.completed_at
              from boxes b
              join stickers s on s.id = b.sticker_id
              left join purchase_order_lines pol on pol.id = b.purchase_order_line_id
             where b.gate_entry_id = :id
             order by b.box_number
            """
        ),
        {"id": str(entry_id)},
    )
    return [dict(r) for r in rows.mappings()]


# ---------------------------------------------------------------------------
# CONTROL POINT 3 — closing a box
# ---------------------------------------------------------------------------


async def record_damage_check(
    conn: AsyncConnection,
    box_id: UUID,
    damage_level: str,
    note: Optional[str],
    photo_paths: List[str],
) -> Dict[str, Any]:
    """The mandatory damage answer (DECISIONS.md §5).

    Answering is compulsory; the answer is allowed to be 'none'. Anything else
    needs a note and at least one photo — without evidence the record is worth
    nothing when the vendor disputes it weeks later, and collecting it costs the
    packer about fifteen seconds.
    """
    if damage_level != "none":
        if not note:
            raise AppError(
                "Describe the damage.", code="note_required", http_status=422
            )
        if not photo_paths:
            raise AppError(
                "At least one photo is required for a damage report.",
                code="photo_required",
                http_status=422,
                hint="The photo is the evidence for the vendor claim.",
            )

    await conn.execute(
        text(
            """
            update boxes
               set damage_level = cast(:level as damage_level),
                   damage_note = :note,
                   damage_checked_by = auth.uid(),
                   damage_checked_at = now()
             where id = :id
            """
        ),
        {"level": damage_level, "note": note, "id": str(box_id)},
    )

    for path in photo_paths:
        await conn.execute(
            text(
                """
                insert into damage_photos (box_id, path, uploaded_by)
                values (:box_id, :path, auth.uid())
                """
            ),
            {"box_id": str(box_id), "path": path},
        )

    if damage_level != "none":
        await _raise_box_exception(
            conn,
            box_id=box_id,
            exception_type="damage",
            title=f"Damage reported ({damage_level})",
            details={"damage_level": damage_level, "note": note, "photos": len(photo_paths)},
        )

    return await _box_row(conn, box_id)


async def close_box(conn: AsyncConnection, box_id: UUID) -> Dict[str, Any]:
    """Attempt CONTROL POINT 3 for one box.

    Success closes the box. Failure holds it — the goods do not enter the
    warehouse, an exception is raised against the vendor and PO, and Admin is
    alerted. There is no third outcome and no override.

    A failure returns `closed: False` rather than raising. That is not leniency:
    the route still answers 409 and the goods are still held. It is because
    holding the box *writes* — the status change, the exception, the Admin alert —
    and raising out of the request would roll the whole transaction back,
    discarding the very record that makes the hold enforceable.
    """
    box = await _box_row(conn, box_id)

    if box["status"] in ("complete", "short_accepted", "rejected", "emptied"):
        return {
            "box": box,
            "closed": True,
            "exception_code": None,
            "message": f"Box {box['box_number']} is already closed.",
        }

    if box["damage_level"] is None:
        raise AppError(
            "Record the damage check before closing this box.",
            code="damage_check_required",
            http_status=422,
        )

    if box["scanned_units"] != box["expected_units"]:
        code = await _hold_box(conn, box_id, box)
        return {
            "box": await _box_row(conn, box_id),
            "closed": False,
            "exception_code": code,
            "message": (
                f"Box {box['box_number']}: {box['scanned_units']} of "
                f"{box['expected_units']} units. Goods held. Action needed."
            ),
        }

    await conn.execute(
        text(
            """
            update boxes
               set status = 'complete', completed_at = now(), completed_by = auth.uid()
             where id = :id
            """
        ),
        {"id": str(box_id)},
    )

    await conn.execute(
        text(
            """
            update purchase_order_lines
               set received_units = received_units + :units
             where id = :line_id
            """
        ),
        {"units": box["scanned_units"], "line_id": str(box["purchase_order_line_id"])},
    )

    return {
        "box": await _box_row(conn, box_id),
        "closed": True,
        "exception_code": None,
        "message": f"Box {box['box_number']} complete — {box['scanned_units']} units received.",
    }


async def _hold_box(conn: AsyncConnection, box_id: UUID, box: Dict[str, Any]) -> str:
    await conn.execute(
        text("update boxes set status = 'held' where id = :id"), {"id": str(box_id)}
    )

    return await _raise_box_exception(
        conn,
        box_id=box_id,
        exception_type="unit_count_mismatch",
        title=(
            f"Box {box['box_number']}: {box['scanned_units']} of "
            f"{box['expected_units']} units"
        ),
        details={
            "box_number": box["box_number"],
            "sku": box["sku"],
            "expected_units": box["expected_units"],
            "scanned_units": box["scanned_units"],
            "shortfall": box["expected_units"] - box["scanned_units"],
        },
    )


async def _raise_box_exception(
    conn: AsyncConnection,
    *,
    box_id: UUID,
    exception_type: str,
    title: str,
    details: Dict[str, Any],
) -> str:
    import json

    row = (
        await conn.execute(
            text(
                """
                insert into exceptions
                  (exception_type, gate_entry_id, box_id, purchase_order_id, vendor_id,
                   title, details, reported_by)
                select cast(:etype as exception_type), b.gate_entry_id, b.id,
                       ge.purchase_order_id, ge.vendor_id,
                       :title, cast(:details as jsonb), auth.uid()
                  from boxes b
                  join gate_entries ge on ge.id = b.gate_entry_id
                 where b.id = :box_id
                returning id, exception_code, gate_entry_id
                """
            ),
            {
                "etype": exception_type,
                "title": title,
                "details": json.dumps(details),
                "box_id": str(box_id),
            },
        )
    ).mappings().one()

    await notifications.notify_ops(
        conn,
        title=title,
        body="Goods are held. Action needed.",
        payload=details,
        gate_entry_id=row["gate_entry_id"],
        exception_id=row["id"],
    )

    return row["exception_code"]


async def _box_row(conn: AsyncConnection, box_id: UUID) -> Dict[str, Any]:
    row = (
        await conn.execute(
            text(
                """
                select b.id, b.gate_entry_id, b.box_number, b.status::text as status,
                       s.code as sticker_code, b.expected_units, b.scanned_units,
                       b.quarantined_units, b.purchase_order_line_id,
                       pol.sku, pol.description,
                       b.damage_level::text as damage_level, b.damage_note,
                       b.verified_at, b.completed_at
                  from boxes b
                  join stickers s on s.id = b.sticker_id
                  left join purchase_order_lines pol on pol.id = b.purchase_order_line_id
                 where b.id = :id
                """
            ),
            {"id": str(box_id)},
        )
    ).mappings().first()

    if row is None:
        raise AppError("Box not found.", code="not_found", http_status=404)
    return dict(row)


async def finish_offloading(conn: AsyncConnection, entry_id: UUID) -> Dict[str, Any]:
    """Close out Step 3 for the whole truck. Refused while any box is held."""
    await conn.execute(
        text("update gate_entries set status = 'offloaded' where id = :id"),
        {"id": str(entry_id)},
    )
    return await gate.get_entry(conn, entry_id)
