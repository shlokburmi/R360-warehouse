"""Stickers, scanning, boxes, exceptions and inbound reconciliation.

PRD §5.2, §5.3, §5.9 — Steps 2 through 5.
"""

from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.deps import CurrentUser, get_current_user, get_db, require_ops, require_roles
from app.schemas.gate import GateEntryOut
from app.schemas.warehouse import (
    BoxCloseResult,
    BoxOut,
    BoxProgress,
    DamageCheckIn,
    ExceptionCreate,
    ExceptionEscalate,
    ExceptionOut,
    ExceptionResolve,
    ReconcileIn,
    ReconcileOut,
    ScanBatchIn,
    ScanBatchResult,
    ScanIn,
    ScanResult,
    StickerIssueResult,
    StickerSheetOut,
    StickerSheetRequest,
)
from app.services import exceptions as exc_service
from app.services import scans as scan_service
from app.services import stickers as sticker_service

router = APIRouter(tags=["warehouse"])

offload_or_ops = require_roles("offloading")
# The inbound role folded into offloading, which now also reconciles.
inbound_or_ops = offload_or_ops
# Packers apply and scan both box and unit stickers at intake, in addition to
# their outbound packing job — see the note on scans_insert in 0019.
packer_or_ops = require_roles("packer")

# ===========================================================================
# STICKERS — Admin only (RLS enforces this independently)
# ===========================================================================


@router.post(
    "/entries/{entry_id}/stickers/box",
    response_model=StickerIssueResult,
    status_code=status.HTTP_201_CREATED,
)
async def generate_box_stickers(
    entry_id: UUID,
    response: Response,
    payload: StickerSheetRequest = Body(default=StickerSheetRequest()),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_ops),
):
    """Issue exactly as many box stickers as the guard counted (PRD Step 2).

    Answers 409 when that count disagrees with the PO — the discrepancy is
    logged against the vendor and no stickers are created.
    """
    result = await sticker_service.generate_box_stickers(
        conn, entry_id, payload.reprint_of_id
    )
    response.status_code = (
        status.HTTP_201_CREATED if result["issued"] else status.HTTP_409_CONFLICT
    )
    return result


@router.post(
    "/entries/{entry_id}/stickers/unit",
    response_model=StickerSheetOut,
    status_code=status.HTTP_201_CREATED,
)
async def generate_unit_stickers(
    entry_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_ops),
):
    """One sticker per unit, bound to its box at issue time (PRD Step 3)."""
    return await sticker_service.generate_unit_stickers(conn, entry_id)


@router.get("/entries/{entry_id}/sticker-sheets", response_model=List[StickerSheetOut])
async def list_sheets(
    entry_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    return await sticker_service.list_sheets(conn, entry_id)


@router.get("/sticker-sheets/{sheet_id}", response_model=StickerSheetOut)
async def get_sheet(
    sheet_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Full sheet including every sticker code — this is what the print view
    renders into a printable QR grid."""
    return await sticker_service.get_sheet(conn, sheet_id)


@router.post("/sticker-sheets/{sheet_id}/void")
async def void_sheet(
    sheet_id: UUID,
    reason: str = Body(embed=True, min_length=3, max_length=300),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_ops),
):
    """Void the unscanned stickers on a sheet. Scanned ones are left alone —
    a scan that happened is a fact, and a reprint must not erase it."""
    voided = await sticker_service.void_sheet(conn, sheet_id, reason)
    return {"voided": voided, "message": f"{voided} sticker(s) voided."}


# ===========================================================================
# SCANNING
# ===========================================================================


@router.post("/entries/{entry_id}/scan/box", response_model=ScanResult)
async def scan_box(
    entry_id: UUID,
    payload: ScanIn,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(packer_or_ops),
):
    """Packer scans a box sticker at intake. A rejection is a 200 with
    `accepted: false` — the operator needs to see and act on it, and pretending
    it is an HTTP error makes the offline queue much harder to reason about."""
    return await scan_service.record_scan(conn, payload, "box_verify")


@router.post("/entries/{entry_id}/scan/unit", response_model=ScanResult)
async def scan_unit(
    entry_id: UUID,
    payload: ScanIn,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(packer_or_ops),
):
    """Packer scans a unit sticker (PRD Step 3)."""
    return await scan_service.record_scan(conn, payload, "unit_verify")


@router.post("/scan/sync", response_model=ScanBatchResult)
async def sync_offline_scans(
    payload: ScanBatchIn,
    scan_type: str = Query(pattern="^(box_verify|unit_verify|pack_unit|out_scan|gate_exit)$"),
    invoice_id: Optional[UUID] = Query(
        default=None,
        description=(
            "Required for pack_unit: which carton the queued product boxes went into. "
            "A product sticker knows the box it arrived in, not the order it leaves on."
        ),
    ),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Drain a device's offline queue.

    Safe to call repeatedly with the same payload: every scan carries a
    client-minted id, so replays are absorbed. That is what lets the device
    retry blindly instead of trying to work out what landed.
    """
    return await scan_service.record_batch(conn, payload.scans, scan_type, invoice_id)


# ===========================================================================
# BOXES
# ===========================================================================


@router.get("/entries/{entry_id}/boxes", response_model=List[BoxOut])
async def list_boxes(
    entry_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    return await scan_service.list_boxes(conn, entry_id)


@router.get("/entries/{entry_id}/unit-progress", response_model=BoxProgress)
async def unit_progress(
    entry_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    return await scan_service.unit_scan_progress(conn, entry_id)


@router.post("/boxes/{box_id}/damage-check", response_model=BoxOut)
async def damage_check(
    box_id: UUID,
    payload: DamageCheckIn,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(packer_or_ops),
):
    """Mandatory damage answer before a box can close (DECISIONS.md §5)."""
    return await scan_service.record_damage_check(
        conn, box_id, payload.damage_level, payload.note, payload.photo_paths
    )


@router.post("/boxes/{box_id}/close", response_model=BoxCloseResult)
async def close_box(
    box_id: UUID,
    response: Response,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(packer_or_ops),
):
    """CONTROL POINT 3.

    On a count mismatch the box is held, an exception is logged against the
    vendor and PO, and this answers 409 with `closed: false`. The hold and the
    exception are both writes, so the refusal is a status code on a committed
    response rather than a raised error that would roll them back.
    """
    result = await scan_service.close_box(conn, box_id)
    if not result["closed"]:
        response.status_code = status.HTTP_409_CONFLICT
    return result


@router.post("/entries/{entry_id}/finish-offloading", response_model=GateEntryOut)
async def finish_offloading(
    entry_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(packer_or_ops),
):
    """Close Step 3 for the whole truck. Refused while any box is held."""
    return await scan_service.finish_offloading(conn, entry_id)


# ===========================================================================
# EXCEPTIONS
# ===========================================================================


@router.get("/exceptions", response_model=List[ExceptionOut])
async def list_exceptions(
    status_filter: Optional[List[str]] = Query(default=None, alias="status"),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    return await exc_service.list_exceptions(
        conn, status_filter=status_filter, limit=limit, offset=offset
    )


@router.post("/exceptions", response_model=ExceptionOut, status_code=status.HTTP_201_CREATED)
async def create_exception(
    payload: ExceptionCreate,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Anyone on the floor can raise one."""
    return await exc_service.create_exception(conn, payload)


@router.get("/exceptions/{exception_id}", response_model=ExceptionOut)
async def get_exception(
    exception_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    return await exc_service.get_exception(conn, exception_id)


@router.post("/exceptions/{exception_id}/resolve", response_model=ExceptionOut)
async def resolve_exception(
    exception_id: UUID,
    payload: ExceptionResolve,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_ops),
):
    """APPROVE & PROCEED / REJECT & RETURN (PRD §5.9).

    For a held box this is one of accept-short, recount, or reject-box — the
    three outcomes in DECISIONS.md §3. Each writes a named person against a
    stated outcome; none of them is a silent override.
    """
    return await exc_service.resolve_exception(conn, exception_id, payload.resolution, payload.note)


@router.post("/exceptions/{exception_id}/escalate", response_model=ExceptionOut)
async def escalate_exception(
    exception_id: UUID,
    payload: ExceptionEscalate,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_ops),
):
    """SEND EMAIL TO SUPERADMIN. Escalating does not release the goods."""
    return await exc_service.escalate_exception(
        conn, exception_id, payload.email_superadmin, payload.note
    )


# ===========================================================================
# INBOUND RECONCILIATION — CONTROL POINT 4
# ===========================================================================


@router.get("/entries/{entry_id}/reconciliation", response_model=ReconcileOut)
async def get_reconciliation(
    entry_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    return await exc_service.reconciliation_view(conn, entry_id)


@router.post("/entries/{entry_id}/reconciliation", response_model=ReconcileOut)
async def submit_reconciliation(
    entry_id: UUID,
    payload: ReconcileIn,
    response: Response,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(inbound_or_ops),
):
    """CONTROL POINT 4. A mismatch blocks putaway and logs an exception."""
    result = await exc_service.reconcile(conn, entry_id, payload)
    if not result["all_matched"]:
        response.status_code = status.HTTP_409_CONFLICT
    return result
