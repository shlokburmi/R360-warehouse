"""Gate endpoints — PRD §5.1, §5.2, §5.7."""

from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.deps import CurrentUser, get_current_user, get_db, require_ops, require_roles
from app.core.errors import AppError
from app.schemas.gate import (
    BoxCountDeclare,
    GateDecision,
    GateEntryCreate,
    GateEntryOut,
    VerifyBoxesResult,
    VisitorLookup,
)
from app.schemas.warehouse import BoxProgress
from app.services import gate as gate_service
from app.services import scans as scan_service

router = APIRouter(prefix="/gate", tags=["gate"])

guard_or_ops = require_roles("security_guard")
# Packers apply and scan both box and unit stickers at intake now, so they
# close out CP2 too (see the note on scans_insert in 0019).
packer_or_ops = require_roles("packer")


@router.get("/visitors/lookup", response_model=VisitorLookup)
async def lookup_visitor(
    mobile: str = Query(min_length=10, max_length=13),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(guard_or_ops),
):
    """Before typing the rest of the form: is this person known, and do we need
    a photo? Keeping this a separate call is what makes the <2 min target
    reachable — the guard finds out after ten digits, not after the whole form."""
    digits = "".join(ch for ch in mobile if ch.isdigit())[-10:]
    return await gate_service.lookup_visitor(conn, digits)


@router.post("/entries", response_model=GateEntryOut, status_code=status.HTTP_201_CREATED)
async def create_entry(
    payload: GateEntryCreate,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(guard_or_ops),
):
    """Register everyone on the truck and send the request to Admin (CP1).

    The gate stays locked from here until an Admin decides. There is no
    parameter on this endpoint, or any other, that skips that.
    """
    entry_id = await gate_service.create_entry(conn, user, payload)
    return await gate_service.get_entry(conn, entry_id)


@router.get("/entries", response_model=List[GateEntryOut])
async def list_entries(
    status_filter: Optional[List[str]] = Query(default=None, alias="status"),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    return await gate_service.list_entries(
        conn, status_filter=status_filter, limit=limit, offset=offset
    )


@router.get("/entries/pending", response_model=List[GateEntryOut])
async def pending_approvals(
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_ops),
):
    """The Admin approval queue (PRD §5.8), oldest first — the truck that has been
    waiting longest is the one to decide next."""
    entries = await gate_service.list_entries(conn, status_filter=["pending_approval"], limit=100)
    return sorted(entries, key=lambda e: e["requested_at"] or e["created_at"])


@router.get("/entries/{entry_id}", response_model=GateEntryOut)
async def get_entry(
    entry_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    return await gate_service.get_entry(conn, entry_id)


@router.post("/entries/{entry_id}/decision", response_model=GateEntryOut)
async def decide_entry(
    entry_id: UUID,
    payload: GateDecision,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_ops),
):
    """CONTROL POINT 1. Only Admin, never the requester."""
    try:
        payload.require_note_on_reject()
    except ValueError as exc:
        raise AppError(str(exc), code="note_required", http_status=422)

    return await gate_service.decide_entry(conn, user, entry_id, payload.approve, payload.note)


@router.post("/entries/{entry_id}/admit", response_model=GateEntryOut)
async def admit_vehicle(
    entry_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(guard_or_ops),
):
    """Guard opens the gate. Stamps time_in. Refused unless Admin approved."""
    return await gate_service.admit_vehicle(conn, entry_id)


@router.post("/entries/{entry_id}/box-count", response_model=GateEntryOut)
async def declare_box_count(
    entry_id: UUID,
    payload: BoxCountDeclare,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(guard_or_ops),
):
    """PRD Step 2: the guard's physical count of boxes on the truck."""
    return await gate_service.declare_box_count(conn, entry_id, payload.box_count)


@router.get("/entries/{entry_id}/box-progress", response_model=BoxProgress)
async def box_progress(
    entry_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    return await scan_service.box_scan_progress(conn, entry_id)


@router.post("/entries/{entry_id}/verify-boxes", response_model=VerifyBoxesResult)
async def verify_boxes(
    entry_id: UUID,
    response: Response,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(packer_or_ops),
):
    """CONTROL POINT 2.

    A mismatch answers 409 with `verified: false` and the code of the exception
    that was logged for Admin. The status code is set on the response rather than
    raised, so the exception record commits with the rest of the transaction.
    """
    result = await gate_service.verify_box_count(conn, entry_id)
    if not result["verified"]:
        response.status_code = status.HTTP_409_CONFLICT
    return result
