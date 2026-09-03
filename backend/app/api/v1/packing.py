"""Invoice matching, packing, out-scan and batch release — PRD §5.4-5.6."""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.deps import (
    CurrentUser,
    get_current_user,
    get_db,
    require_ops_manager,
    require_roles,
)
from app.core.errors import AppError
from app.schemas.warehouse import ScanIn, ScanResult
from app.services import packing as packing_service
from app.services import scans as scan_service

router = APIRouter(tags=["packing"])

packer_or_ops = require_roles("packer")
# Invoice creation and matching are done by whichever of these two roles is
# physically holding the invoice — a Packer creating it from her own OCR scan
# (this session's change), or an Invoice Matcher doing the same job under the
# older role split. Both still union with admin via require_roles.
matcher_or_ops = require_roles("packer", "invoice_matcher")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class BadgeHolder(BaseModel):
    profile_id: UUID
    full_name: str
    role: str
    employee_code: Optional[str] = None


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    invoice_id: UUID
    invoice_number: str
    order_no: Optional[str] = None
    customer_name: Optional[str] = None
    is_open: bool
    stage: str

    assigned_to: Optional[UUID] = None
    assigned_to_name: Optional[str] = None
    assigned_by: Optional[UUID] = None
    assigned_by_name: Optional[str] = None
    assigned_at: Optional[datetime] = None

    packed_by: Optional[UUID] = None
    packed_by_name: Optional[str] = None
    packed_at: Optional[datetime] = None

    batch_id: Optional[UUID] = None
    batch_code: Optional[str] = None
    batch_status: Optional[str] = None
    out_scanned_at: Optional[datetime] = None


class InvoiceFromOrderNo(BaseModel):
    """A Packer creating an invoice from the Order No she just OCR-scanned off
    the physical invoice. There is no typed invoice number and no PO/product/
    quantity — `order_no` is the whole invoice; what's inside the carton is
    Admin's separate ERP's concern, not this app's.

    The read-provenance fields mirror the OCR audit trail this system has
    always kept (0015_order_no_ocr.sql): the engine's raw output, confidence
    and whether the operator corrected it before confirming — "the OCR
    misread it" is only a usable answer six months from now if the evidence
    was recorded at the time.
    """

    order_no: str = Field(min_length=3, max_length=40)

    source: Literal["ocr", "manual"] = "ocr"
    raw_text: Optional[str] = Field(default=None, max_length=2000)
    confidence: Optional[float] = Field(default=None, ge=0, le=100)
    was_corrected: bool = False

    @field_validator("order_no")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class BadgeIn(BaseModel):
    """A badge scan. The code is opaque and carries no personal data."""

    badge_code: str = Field(min_length=4, max_length=40)

    @field_validator("badge_code")
    @classmethod
    def _clean(cls, v: str) -> str:
        return v.strip()


class PackIn(BadgeIn):
    invoice_number: str = Field(min_length=3, max_length=60)
    carton_code: Optional[str] = Field(default=None, max_length=60)

    @field_validator("invoice_number")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class AttributionResult(BaseModel):
    invoice: InvoiceOut
    who: BadgeHolder
    message: str


class Carton(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    invoice_id: UUID
    invoice_number: str
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
    first_carton: Optional[datetime] = None
    last_carton: Optional[datetime] = None
    avg_minutes_per_carton: Optional[float] = None


# ---------------------------------------------------------------------------
# Badges
# ---------------------------------------------------------------------------


@router.post("/badges/resolve", response_model=BadgeHolder)
async def resolve_badge(
    payload: BadgeIn,
    expect: str = Query(default="any", pattern="^(any|matcher|packer)$"),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_roles("packer", "invoice_matcher")),
):
    """Identify the holder of a scanned badge.

    Returns a name and role only. A badge is attribution, not a credential — it
    cannot be exchanged for a session, and this endpoint is the only thing that
    reads one.

    invoice_matcher as well as packer: this is what a matcher calls before
    /invoices/assign, to identify whose badge she is holding when she hands the
    carton to a packer.
    """
    if expect == "any":
        expected = ["invoice_matcher", "packer", "admin"]
    elif expect == "matcher":
        expected = ["invoice_matcher", "packer", "admin"]
    else:
        expected = [expect]
    return await packing_service.resolve_badge(conn, payload.badge_code, expected)


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------


@router.post(
    "/invoices/from-order-no", response_model=InvoiceOut, status_code=status.HTTP_201_CREATED
)
async def create_invoice_from_order_no(
    payload: InvoiceFromOrderNo,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(matcher_or_ops),
):
    """A Packer books an invoice from the Order No she just scanned off the
    physical invoice — there is no manual invoice-number entry, and no PO/
    product/quantity, anywhere in this dashboard."""
    return await packing_service.create_invoice_from_order_no(
        conn,
        order_no=payload.order_no,
        actor_id=user.id,
        source=payload.source,
        raw_text=payload.raw_text,
        confidence=payload.confidence,
        was_corrected=payload.was_corrected,
    )


@router.get("/invoices", response_model=List[InvoiceOut])
async def list_invoices(
    stage: Optional[str] = Query(
        default=None, pattern="^(open|assigned|packed|batched|out_scanned|closed)$"
    ),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    return await packing_service.list_invoices(conn, stage)


@router.get("/invoices/lookup", response_model=InvoiceOut)
async def lookup_invoice(
    invoice_number: Optional[str] = Query(default=None, min_length=3, max_length=60),
    order_no: Optional[str] = Query(default=None, min_length=3, max_length=40),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(matcher_or_ops),
):
    """Is this a valid, open invoice?

    Two ways to identify it. `invoice_number` is the scanned or typed value.
    `order_no` is the same thing by another name (0035: every invoice's
    number *is* its scanned Order No) — kept as a separate parameter because
    the two callers (a fresh OCR read vs. a typed/looked-up number) don't
    know that in advance. Exactly one is required — accepting both would
    leave the server deciding which the caller meant, and the two can
    disagree.
    """
    if (invoice_number is None) == (order_no is None):
        raise AppError(
            "Provide either an invoice number or an Order No.",
            code="bad_request",
            http_status=422,
        )

    if order_no is not None:
        invoice = await packing_service.get_invoice_by_order_no(conn, order_no)
    else:
        invoice = await packing_service.get_invoice_by_number(conn, invoice_number)

    if not invoice["is_open"]:
        raise AppError(
            f"Invoice {invoice['invoice_number']} is closed.",
            code="invoice_closed",
            http_status=409,
        )

    return invoice


@router.post("/invoices/pack", response_model=AttributionResult)
async def pack_invoice(
    payload: PackIn,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(packer_or_ops),
):
    """CONTROL POINT 5, second half — binds the packer's badge to the invoice.

    Refused if the invoice has not been assigned to anyone, or if the packer
    is the same person who assigned it (0036 — assigning now stands in for
    the old "verify" step).
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
    user: CurrentUser = Depends(require_ops_manager),
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
    user: CurrentUser = Depends(require_ops_manager),
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
    user: CurrentUser = Depends(require_ops_manager),
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
    user: CurrentUser = Depends(require_ops_manager),
):
    """Release to the pickup area, and close the invoices it contains."""
    return await packing_service.release_batch(conn, batch_id)


@router.get("/reports/packer-productivity", response_model=List[PackerProductivity])
async def packer_productivity(
    from_date: Optional[str] = Query(default=None),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_ops_manager),
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


# ---------------------------------------------------------------------------
# Packing assignment and product-box scanning (Phase 5)
# ---------------------------------------------------------------------------


class PackingState(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    invoice_id: UUID
    invoice_number: str
    is_open: bool = True
    assigned_to: Optional[UUID] = None
    assigned_to_name: Optional[str] = None
    packed_by: Optional[UUID] = None
    packed_by_name: Optional[str] = None
    packed_at: Optional[datetime] = None


class AssignIn(BaseModel):
    """Assign a carton by scanning the packer's badge card.

    The badge code is the assignee's, read off a card physically present at the
    bench. That is what `resolve_badge_holder` is for, and it is why this needs no
    relaxation of the badge rules in DECISIONS.md §CC2.
    """

    invoice_number: str = Field(min_length=3, max_length=64)
    badge_code: str = Field(min_length=4, max_length=64)
    note: Optional[str] = Field(default=None, max_length=280)

    @field_validator("invoice_number")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class AssignResult(BaseModel):
    invoice: InvoiceOut
    packing: PackingState
    assigned_to: BadgeHolder
    message: str


@router.post("/invoices/assign", response_model=AssignResult)
async def assign_invoice(
    payload: AssignIn,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_roles("packer", "invoice_matcher")),
):
    """Hand a carton to a named packer — this is the act that stands in for
    the old "verify" step (0036): there is no product/quantity confirmation
    left, so assigning is what establishes the second person CONTROL POINT 5
    requires before packing can happen.

    invoice_matcher as well as packer (and Admin, who covers both stations).
    """
    return await packing_service.assign_invoice(
        conn, payload.invoice_number, payload.badge_code, user.id, payload.note
    )


@router.get("/invoices/{invoice_id}/packing", response_model=PackingState)
async def invoice_packing_state(
    invoice_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    return await packing_service.packing_state(conn, invoice_id)


@router.get("/packing/assigned-to-me", response_model=List[PackingState])
async def assigned_to_me(
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(packer_or_ops),
):
    """The packer's own queue. Nobody else's work appears here."""
    return await packing_service.assigned_to_me(conn)
