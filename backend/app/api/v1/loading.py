"""The guard's carton count on a finished batch, and Admin's decision on it.

The outbound mirror of CONTROL POINT 1. Two roles, two endpoints, and the
database refuses to let one person be both.
"""

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.deps import CurrentUser, get_current_user, get_db, require_ops, require_roles
from app.services import loading as loading_service

router = APIRouter(tags=["loading"])

guard_or_ops = require_roles("security_guard")


class BatchAwaitingCount(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    batch_id: UUID
    batch_code: str
    batch_status: str
    planned_carton_count: int
    carton_count: int
    created_at: datetime


class LoadApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    batch_id: UUID
    batch_code: str
    batch_status: str

    counted_cartons: int
    expected_cartons: int
    counted_by: Optional[UUID] = None
    counted_by_name: Optional[str] = None
    counted_at: datetime

    status: str
    decided_by: Optional[UUID] = None
    decided_by_name: Optional[str] = None
    decided_at: Optional[datetime] = None
    note: Optional[str] = None
    is_current: bool = True

    #: Whether the guard's number agreed with the system's. The reason this is a
    #: field rather than something the UI computes: it is the one thing Admin needs
    #: to see before deciding, and it must not depend on the client getting the
    #: comparison right.
    matches: bool
    waiting_seconds: Optional[int] = None


class CountIn(BaseModel):
    """Only the number the guard physically counted.

    `expected_cartons` is filled in by the database from the batch. A count where
    the operator supplies both numbers is not a count.
    """

    counted_cartons: int = Field(ge=0, le=10_000)


class CountDecision(BaseModel):
    approve: bool
    note: Optional[str] = Field(default=None, max_length=500)


@router.get("/loading/awaiting-count", response_model=List[BatchAwaitingCount])
async def awaiting_count(
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Batches that are out-scanned and need a guard to count them."""
    return await loading_service.awaiting_count(conn)


@router.get("/loading/pending", response_model=List[LoadApprovalOut])
async def pending_decisions(
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Counts filed and waiting on Admin. Oldest first — the batch that has waited
    longest is the one holding up a bay."""
    return await loading_service.pending_decisions(conn)


@router.get("/loading/batches/{batch_id}", response_model=Optional[LoadApprovalOut])
async def batch_approval(
    batch_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    return await loading_service.get_approval(conn, batch_id)


@router.post("/loading/batches/{batch_id}/count", response_model=LoadApprovalOut)
async def count_cartons(
    batch_id: UUID,
    payload: CountIn,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(guard_or_ops),
):
    """File a physical carton count. Admin is alerted, and told if it mismatches."""
    return await loading_service.count_cartons(conn, batch_id, payload.counted_cartons)


@router.post("/loading/batches/{batch_id}/decision", response_model=LoadApprovalOut)
async def decide_count(
    batch_id: UUID,
    payload: CountDecision,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_ops),
):
    """Approve or reject the count. Nothing is released until this is recorded."""
    return await loading_service.decide_count(conn, batch_id, payload.approve, payload.note)
