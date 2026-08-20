"""Pickup verification and gate exit — PRD §5.7, Step 10, CONTROL POINT 7."""

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.deps import (
    CurrentUser,
    get_current_user,
    get_db,
    require_ops,
    require_roles,
)
from app.schemas.gate import PersonOut, PersonIn, VEHICLE_RE
from app.schemas.warehouse import ScanIn, ScanResult
from app.services import pickup as pickup_service
from app.services import scans as scan_service

router = APIRouter(prefix="/pickups", tags=["pickup"])

guard_or_ops = require_roles("security_guard", "ops_manager")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AwaitingPickup(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    batch_id: UUID
    batch_code: str
    released_at: Optional[datetime] = None
    carton_count: int
    released_by_name: Optional[str] = None


class PickupCarton(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    invoice_id: UUID
    invoice_number: str
    sku: str
    units: int
    customer_name: Optional[str] = None
    packed_by_name: Optional[str] = None
    out_scanned_at: Optional[datetime] = None
    exit_scanned_at: Optional[datetime] = None
    exit_scanned_by_name: Optional[str] = None


class PickupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    pickup_id: UUID
    pickup_code: str
    status: str
    vehicle_number: str
    courier_name: Optional[str] = None
    transporter_name: Optional[str] = None

    batch_id: UUID
    batch_code: str

    released_cartons: int
    verified_cartons: int
    remaining_cartons: int

    registered_at: datetime
    registered_by_name: Optional[str] = None
    verified_at: Optional[datetime] = None
    verified_by_name: Optional[str] = None
    time_in: Optional[datetime] = None
    time_out: Optional[datetime] = None
    released_by_name: Optional[str] = None

    # Exit approval (Phase 5). A verified vehicle no longer leaves on the
    # guard's word alone.
    exit_requested_at: Optional[datetime] = None
    exit_requested_by_name: Optional[str] = None
    exit_approved_at: Optional[datetime] = None
    exit_approved_by_name: Optional[str] = None
    exit_rejected_note: Optional[str] = None
    exit_waiting_seconds: Optional[int] = None

    message: str = ""
    persons: List[PersonOut] = Field(default_factory=list)
    cartons: List[PickupCarton] = Field(default_factory=list)


class PickupCreate(BaseModel):
    """Register the collecting vehicle (PRD §5.7).

    Same person fields as an inbound entry, because the identity rules are the
    same and a driver who both delivers and collects should be one record.
    """

    batch_id: UUID
    vehicle_number: str = Field(pattern=VEHICLE_RE)
    courier_name: Optional[str] = Field(default=None, max_length=160)
    transporter_name: Optional[str] = Field(default=None, max_length=160)
    persons: List[PersonIn] = Field(min_length=1, max_length=8)

    @field_validator("vehicle_number")
    @classmethod
    def _clean_vehicle(cls, v: str) -> str:
        return "".join(ch for ch in v.upper() if ch.isalnum() or ch == "-")

    @field_validator("persons")
    @classmethod
    def _one_driver(cls, v: List[PersonIn]) -> List[PersonIn]:
        if len([p for p in v if p.visitor_role == "driver"]) != 1:
            raise ValueError("Exactly one person must be marked as the driver")
        if len({p.mobile for p in v}) != len(v):
            raise ValueError("Two people cannot share the same mobile number")
        return v


class PickupVerifyResult(BaseModel):
    pickup: PickupOut
    verified: bool
    message: str


class ExitRequestResult(BaseModel):
    pickup: PickupOut
    requested: bool
    message: str


class ExitDecision(BaseModel):
    approve: bool
    note: Optional[str] = Field(default=None, max_length=500)


class ExitDecisionResult(BaseModel):
    pickup: PickupOut
    approved: bool
    message: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/awaiting", response_model=List[AwaitingPickup])
async def awaiting_pickup(
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Released batches with no vehicle registered yet — the guard's worklist."""
    return await pickup_service.list_awaiting_pickup(conn)


@router.get("", response_model=List[PickupOut])
async def list_pickups(
    status_filter: Optional[List[str]] = Query(default=None, alias="status"),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    return await pickup_service.list_pickups(conn, status_filter)


@router.post("", response_model=PickupOut, status_code=status.HTTP_201_CREATED)
async def register_pickup(
    payload: PickupCreate,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(guard_or_ops),
):
    """Log the vehicle and its people. Refused unless the batch is released."""
    return await pickup_service.register_pickup(
        conn,
        payload.batch_id,
        payload.vehicle_number,
        payload.persons,
        payload.courier_name,
        payload.transporter_name,
    )


# Declared before /{pickup_id}: FastAPI matches in declaration order, so a
# literal path registered after the parameterised one is swallowed by it and
# arrives as an invalid UUID.
@router.get("/awaiting-exit", response_model=List[PickupOut])
async def awaiting_exit(
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Vehicles loaded, verified, and waiting on an Ops decision.

    Readable by the guard as well as Ops: the guard standing at the gate is the
    person being asked "how long?", and telling them to go and find out is not an
    answer.
    """
    return await pickup_service.awaiting_exit_approval(conn)


@router.get("/{pickup_id}", response_model=PickupOut)
async def get_pickup(
    pickup_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    return await pickup_service.get_pickup(conn, pickup_id)


@router.post("/{pickup_id}/scan", response_model=ScanResult)
async def scan_carton_onto_vehicle(
    pickup_id: UUID,
    payload: ScanIn,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(guard_or_ops),
):
    """Scan a carton as it is loaded. The label is the invoice number.

    Refused if the carton's batch was never released — which is what stops a
    carton being carried straight from the packing bench onto a truck.
    """
    return await scan_service.record_scan(conn, payload, "gate_exit")


@router.post("/{pickup_id}/verify", response_model=PickupVerifyResult)
async def verify_pickup(
    pickup_id: UUID,
    response: Response,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(guard_or_ops),
):
    """CONTROL POINT 7. 409 with the missing invoice numbers if any are absent."""
    result = await pickup_service.verify_pickup(conn, pickup_id)
    if not result["verified"]:
        response.status_code = status.HTTP_409_CONFLICT
    return result


@router.post("/{pickup_id}/release", response_model=PickupOut)
async def release_vehicle(
    pickup_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(guard_or_ops),
):
    """Open the gate and stamp time out. Only possible once verified."""
    return await pickup_service.release_vehicle(conn, pickup_id)


@router.post("/{pickup_id}/cancel", response_model=PickupOut)
async def cancel_pickup(
    pickup_id: UUID,
    reason: str = Body(embed=True, min_length=3, max_length=500),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(guard_or_ops),
):
    """Courier left without loading, wrong vehicle, and so on."""
    return await pickup_service.cancel_pickup(conn, pickup_id, reason)


# ---------------------------------------------------------------------------
# Exit approval (Phase 5)
# ---------------------------------------------------------------------------


@router.post("/{pickup_id}/request-exit", response_model=ExitRequestResult)
async def request_exit(
    pickup_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(guard_or_ops),
):
    """The guard asks Ops to open the gate.

    Deliberately separate from verification: CONTROL POINT 7 answers "is every
    carton on the truck", this answers "may it go" (DECISIONS.md §CD4).
    """
    return await pickup_service.request_exit(conn, pickup_id)


@router.post("/{pickup_id}/exit-decision", response_model=ExitDecisionResult)
async def decide_exit(
    pickup_id: UUID,
    payload: ExitDecision,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_ops),
):
    """Ops approves or holds the vehicle.

    Approving records the approval; it does not open the gate. The guard still
    performs the release, so the gate opening stays attached to the person
    standing at it.
    """
    return await pickup_service.decide_exit(conn, pickup_id, payload.approve, payload.note)
