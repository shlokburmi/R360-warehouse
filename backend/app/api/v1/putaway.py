"""Putaway endpoints — PRD Step 6 (Phase 2)."""

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.deps import CurrentUser, get_current_user, get_db, require_roles
from app.services import putaway as putaway_service

router = APIRouter(tags=["putaway"])

# Warehouse Staff owns this step, carved back out of Offloading (which keeps
# inbound reconciliation and receiving). Admin can always act. Matched by the
# RLS policy in 0023_role_split.sql.
store_or_ops = require_roles("warehouse_staff")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class PutawayTask(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    box_id: UUID
    gate_entry_id: UUID
    box_number: int
    box_status: str
    stock_remaining: int
    quarantine_remaining: int
    entry_code: str
    vehicle_number: str
    vendor_name: str
    po_number: Optional[str] = None
    sku: Optional[str] = None
    description: Optional[str] = None


class LocationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    zone: str
    description: Optional[str] = None
    is_quarantine: bool


class BoxPutawayStatus(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    box_id: UUID
    box_number: int
    box_status: str
    scanned_units: int
    quarantined_units: int
    stock_units: int
    stock_placed: int
    quarantine_placed: int
    stock_remaining: int
    quarantine_remaining: int
    sku: Optional[str] = None
    description: Optional[str] = None
    entry_code: str
    entry_status: str


class PutawayIn(BaseModel):
    """Place units on a rack.

    `location_code` rather than an id: the operator scans the label on the rack,
    and the code is what the label carries. Resolving it server-side means a
    mistyped rack is refused rather than silently recorded against nothing.
    """

    location_code: str = Field(min_length=3, max_length=20)
    units: int = Field(ge=1, le=10_000)
    disposition: str = Field(default="stock", pattern="^(stock|quarantine)$")

    @field_validator("location_code")
    @classmethod
    def _normalise(cls, v: str) -> str:
        return v.strip().upper()


class PutawayResult(BaseModel):
    box: BoxPutawayStatus
    location: LocationOut
    units_placed: int
    complete: bool
    message: str


class PutawayRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    location_code: str
    is_quarantine: bool
    units: int
    disposition: str
    moved_at: datetime
    moved_by_name: Optional[str] = None


class StockRow(BaseModel):
    location_code: str
    zone: str
    is_quarantine: bool
    sku: str
    description: str
    units: int
    last_movement: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/putaway/queue", response_model=List[PutawayTask])
async def putaway_queue(
    entry_id: Optional[UUID] = Query(default=None),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Boxes ready to shelve.

    A box only appears once its gate entry has passed inbound reconciliation, so
    this list is also the answer to "what am I allowed to move?".
    """
    return await putaway_service.putaway_queue(conn, entry_id)


@router.get("/locations", response_model=List[LocationOut])
async def list_locations(
    q: Optional[str] = Query(default=None),
    quarantine_only: bool = Query(default=False),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    return await putaway_service.search_locations(conn, q, quarantine_only)


@router.get("/locations/resolve", response_model=LocationOut)
async def resolve_location(
    code: str = Query(min_length=3, max_length=20),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Turn a scanned rack label into a location. 404 if it is not a real rack."""
    return await putaway_service.resolve_location(conn, code)


@router.get("/boxes/{box_id}/putaway", response_model=BoxPutawayStatus)
async def box_putaway_status(
    box_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    return await putaway_service.box_putaway_status(conn, box_id)


@router.post(
    "/boxes/{box_id}/putaway",
    response_model=PutawayResult,
    status_code=status.HTTP_201_CREATED,
)
async def record_putaway(
    box_id: UUID,
    payload: PutawayIn,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(store_or_ops),
):
    """Place units from a box onto a rack.

    Refused if the entry has not been reconciled, if it would place more units
    than arrived, or if damaged units are aimed at a stock rack (or vice versa).
    """
    return await putaway_service.record_putaway(
        conn, box_id, payload.location_code, payload.units, payload.disposition
    )


@router.get("/boxes/{box_id}/putaway/history", response_model=List[PutawayRecord])
async def putaway_history(
    box_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    return await putaway_service.box_history(conn, box_id)


@router.get("/stock", response_model=List[StockRow])
async def stock_by_location(
    sku: Optional[str] = Query(default=None),
    zone: Optional[str] = Query(default=None),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Where each SKU is currently sitting."""
    return await putaway_service.stock_by_location(conn, sku, zone)
