"""Invoice matching, packing, out-scan and batch release — PRD §5.4-5.6."""

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.deps import CurrentUser, get_current_user, get_db, require_ops, require_roles
from app.schemas.warehouse import ScanIn, ScanResult
from app.services import packing as packing_service
from app.services import scans as scan_service

router = APIRouter(tags=["packing"])

matcher_or_ops = require_roles("invoice_matcher", "ops_manager")
packer_or_ops = require_roles("packer", "ops_manager")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class BadgeHolder(BaseModel):
    profile_id: UUID
    full_name: str
    role: str
    employee_code: Optional[str] = None


class StockHint(BaseModel):
    location_code: str
    units: int


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    invoice_id: UUID
    invoice_number: str
    sku: str
    units: int
    customer_name: Optional[str] = None
    description: Optional[str] = None
    is_open: bool
    stage: str

    verified_by: Optional[UUID] = None
    verified_by_name: Optional[str] = None
    verified_at: Optional[datetime] = None

    packed_by: Optional[UUID] = None
    packed_by_name: Optional[str] = None
    packed_at: Optional[datetime] = None

    batch_id: Optional[UUID] = None
    batch_code: Optional[str] = None
    batch_status: Optional[str] = None
    out_scanned_at: Optional[datetime] = None

    suggested_locations: List[StockHint] = Field(default_factory=list)


class BadgeIn(BaseModel):
    """A badge scan. The code is opaque and carries no personal data."""

    badge_code: str = Field(min_length=4, max_length=40)

    @field_validator("badge_code")
    @classmethod
    def _clean(cls, v: str) -> str:
        return v.strip()


class VerifyIn(BadgeIn):
    invoice_number: str = Field(min_length=3, max_length=60)

    @field_validator("invoice_number")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class PackIn(VerifyIn):
    carton_code: Optional[str] = Field(default=None, max_length=60)


class AttributionResult(BaseModel):
    invoice: InvoiceOut
    who: BadgeHolder
    message: str


class Carton(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    invoice_id: UUID
    invoice_number: str
    sku: str
    units: int
    customer_name: Optional[str] = None
    packed_by_name: Optional[str] = None
    packed_at: Optional[datetime] = None
    out_scanned_at: Optional[datetime] = None
    out_scanned_by_name: Optional[str] = None


class BatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    batch_id: UUID
    batch_code: str
    status: str
    planned_carton_count: int
    assigned_cartons: int
    scanned_cartons: int
    remaining_cartons: int
    created_at: datetime
    created_by_name: Optional[str] = None
    released_at: Optional[datetime] = None
    released_by_name: Optional[str] = None
    notes: Optional[str] = None
    cartons: List[Carton] = Field(default_factory=list)
    message: str = ""


class BatchCreate(BaseModel):
    invoice_ids: List[UUID] = Field(min_length=1, max_length=500)
    notes: Optional[str] = Field(default=None, max_length=500)


class BatchCompleteResult(BaseModel):
    batch: BatchOut
    completed: bool
    message: str


class PackerProductivity(BaseModel):
    full_name: str
    employee_code: Optional[str] = None
    cartons_packed: int
    units_packed: Optional[int] = None
    first_carton: Optional[datetime] = None
    last_carton: Optional[datetime] = None
    avg_minutes_per_carton: Optional[float] = None


# ---------------------------------------------------------------------------
# Badges
# ---------------------------------------------------------------------------


@router.post("/badges/resolve", response_model=BadgeHolder)
async def resolve_badge(
    payload: BadgeIn,
    expect: str = Query(default="any", pattern="^(any|invoice_matcher|packer)$"),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_roles("invoice_matcher", "packer", "ops_manager")),
):
    """Identify the holder of a scanned badge.

    Returns a name and role only. A badge is attribution, not a credential — it
    cannot be exchanged for a session, and this endpoint is the only thing that
    reads one.
    """
    expected = ["invoice_matcher", "packer"] if expect == "any" else [expect]
    return await packing_service.resolve_badge(conn, payload.badge_code, expected)


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------


@router.get("/invoices", response_model=List[InvoiceOut])
async def list_invoices(
    stage: Optional[str] = Query(
        default=None, pattern="^(open|verified|packed|batched|out_scanned|closed)$"
    ),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    return await packing_service.list_invoices(conn, stage)


@router.get("/invoices/lookup", response_model=InvoiceOut)
async def lookup_invoice(
    invoice_number: str = Query(min_length=3, max_length=60),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(matcher_or_ops),
):
    """PRD §5.4: is this a valid open invoice, and where is its stock?

    Called on the invoice scan, before the matcher walks to a rack — so a trip to
    the wrong aisle is avoided rather than discovered.
    """
    return await packing_service.lookup_for_matching(conn, invoice_number)


@router.post("/invoices/verify", response_model=AttributionResult)
async def verify_invoice(
    payload: VerifyIn,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(matcher_or_ops),
):
    """CONTROL POINT 5, first half — matcher confirms product against invoice."""
    result = await packing_service.verify_invoice(
        conn, payload.invoice_number, payload.badge_code
    )
    return {
        "invoice": result["invoice"],
        "who": result["verified_by"],
        "message": result["message"],
    }


@router.post("/invoices/pack", response_model=AttributionResult)
async def pack_invoice(
    payload: PackIn,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(packer_or_ops),
):
    """CONTROL POINT 5, second half — binds the packer's badge to the invoice.

    Refused if the invoice was never verified, or if the packer is the same
    person who verified it.
    """
    result = await packing_service.pack_invoice(
        conn, payload.invoice_number, payload.badge_code, payload.carton_code
    )
    return {
        "invoice": result["invoice"],
        "who": result["packed_by"],
        "message": result["message"],
    }


# ---------------------------------------------------------------------------
# Batches and out-scan (CONTROL POINT 6)
# ---------------------------------------------------------------------------


@router.get("/batches", response_model=List[BatchOut])
async def list_batches(
    batch_status: Optional[str] = Query(
        default=None, alias="status", pattern="^(open|scanning|complete|released|cancelled)$"
    ),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    return await packing_service.list_batches(conn, batch_status)


@router.post("/batches", response_model=BatchOut, status_code=status.HTTP_201_CREATED)
async def create_batch(
    payload: BatchCreate,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_ops),
):
    """Plan a batch from packed cartons.

    Planned before out-scanning on purpose: CONTROL POINT 6 compares cartons
    assigned against cartons scanned, and a batch assembled from whatever
    happened to get scanned could never fail.
    """
    return await packing_service.create_batch(conn, payload.invoice_ids, payload.notes)


@router.get("/batches/{batch_id}", response_model=BatchOut)
async def get_batch(
    batch_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    return await packing_service.get_batch(conn, batch_id)


@router.post("/batches/{batch_id}/scan", response_model=ScanResult)
async def out_scan(
    batch_id: UUID,
    payload: ScanIn,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_ops),
):
    """Out-scan one carton. The label is the invoice number (PRD §5.6).

    A rejection comes back as a 200 with `accepted: false` — same as the other
    scanning steps, because a refused scan is a normal outcome the operator acts
    on, not an error.
    """
    return await scan_service.record_scan(conn, payload, "out_scan")


@router.post("/batches/{batch_id}/complete", response_model=BatchCompleteResult)
async def complete_batch(
    batch_id: UUID,
    response: Response,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_ops),
):
    """CONTROL POINT 6. 409 with the two counts if any carton is unscanned."""
    result = await packing_service.complete_batch(conn, batch_id)
    if not result["completed"]:
        response.status_code = status.HTTP_409_CONFLICT
    return result


@router.post("/batches/{batch_id}/release", response_model=BatchOut)
async def release_batch(
    batch_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_ops),
):
    """Release to the pickup area, and close the invoices it contains."""
    return await packing_service.release_batch(conn, batch_id)


@router.get("/reports/packer-productivity", response_model=List[PackerProductivity])
async def packer_productivity(
    from_date: Optional[str] = Query(default=None),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_ops),
):
    """PRD §5.10 Packer Productivity."""
    return await packing_service.packing_productivity(conn, from_date)


# ---------------------------------------------------------------------------
# Ready-to-batch pool
# ---------------------------------------------------------------------------


@router.get("/packing/ready", response_model=List[InvoiceOut])
async def ready_to_batch(
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> List[Dict[str, Any]]:
    """Cartons packed but not yet assigned to a batch."""
    return await packing_service.list_invoices(conn, "packed")
