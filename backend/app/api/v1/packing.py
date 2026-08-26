"""Invoice matching, packing, out-scan and batch release — PRD §5.4-5.6."""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.deps import CurrentUser, get_current_user, get_db, require_ops, require_roles
from app.core.errors import AppError
from app.schemas.warehouse import ScanIn, ScanResult
from app.services import packing as packing_service
from app.services import scans as scan_service
from app.services import stickers as sticker_service

router = APIRouter(tags=["packing"])

# Matching is an Admin-only action now that invoice_matcher has folded into it.
matcher_or_ops = require_ops
packer_or_ops = require_roles("packer")


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
    order_no: Optional[str] = None
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


class InvoiceCreate(BaseModel):
    invoice_number: str = Field(min_length=3, max_length=60)
    purchase_order_line_id: UUID
    units: int = Field(gt=0)
    customer_name: Optional[str] = Field(default=None, max_length=120)

    @field_validator("invoice_number")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class CartonStickerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    status: str
    invoice_id: UUID
    invoice_number: str
    sku: str
    units: int
    customer_name: Optional[str] = None


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


class OrderNoIn(BaseModel):
    """An attempt to read the Order No off a delivery challan.

    `order_no` is optional on purpose — the client posts a null when OCR ran and
    produced nothing usable, so the miss is recorded rather than discarded. It is
    the one endpoint here where "I failed" is a legitimate, recorded outcome.
    """

    invoice_number: str = Field(min_length=3, max_length=60)
    order_no: Optional[str] = Field(default=None, max_length=40)
    source: Literal["ocr", "manual"]

    # The engine's unedited output. Capped rather than unbounded: it is evidence,
    # not a document store, and the challan header block is a few hundred
    # characters at most.
    raw_text: Optional[str] = Field(default=None, max_length=2000)
    confidence: Optional[float] = Field(default=None, ge=0, le=100)
    was_corrected: bool = False

    @field_validator("invoice_number")
    @classmethod
    def _upper_invoice(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("order_no")
    @classmethod
    def _upper_order(cls, v: Optional[str]) -> Optional[str]:
        # An empty string from a cleared input field means "no read", not "the
        # order number is the empty string" — which would fail the regex and
        # report a validation error for what is really a miss.
        if v is None:
            return None
        cleaned = v.strip().upper()
        return cleaned or None


class OrderNoResult(BaseModel):
    invoice: InvoiceOut
    recorded: bool
    message: str


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
    expect: str = Query(default="any", pattern="^(any|matcher|packer)$"),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_roles("packer")),
):
    """Identify the holder of a scanned badge.

    Returns a name and role only. A badge is attribution, not a credential — it
    cannot be exchanged for a session, and this endpoint is the only thing that
    reads one.

    "matcher" is a station, not a role — matching is done by Admin.
    """
    expected = ["admin", "packer"] if expect == "any" else (["admin"] if expect == "matcher" else [expect])
    return await packing_service.resolve_badge(conn, payload.badge_code, expected)


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------


@router.post("/invoices", response_model=InvoiceOut, status_code=status.HTTP_201_CREATED)
async def create_invoice(
    payload: InvoiceCreate,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_ops),
):
    """Admin books an invoice against a received PO line, from the dashboard."""
    return await packing_service.create_invoice(
        conn,
        invoice_number=payload.invoice_number,
        purchase_order_line_id=payload.purchase_order_line_id,
        units=payload.units,
        customer_name=payload.customer_name,
    )


@router.post(
    "/invoices/{invoice_id}/sticker",
    response_model=CartonStickerOut,
    status_code=status.HTTP_201_CREATED,
)
async def issue_carton_sticker(
    invoice_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_ops),
):
    """Print the carton sticker for this invoice. Reissuing voids the old one."""
    return await sticker_service.issue_carton_sticker(conn, invoice_id)


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
    invoice_number: Optional[str] = Query(default=None, min_length=3, max_length=60),
    order_no: Optional[str] = Query(default=None, min_length=3, max_length=40),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(matcher_or_ops),
):
    """PRD §5.4: is this a valid open invoice, and where is its stock?

    Called on the invoice scan, before the matcher walks to a rack — so a trip to
    the wrong aisle is avoided rather than discovered.

    Two ways to identify it. `invoice_number` is the scanned or typed barcode
    value. `order_no` is for the case where the matcher has only the challan: OCR
    reads the Order No off the page and the invoice is found from that. Exactly
    one is required — accepting both would leave the server deciding which the
    caller meant, and the two can disagree.
    """
    if (invoice_number is None) == (order_no is None):
        raise AppError(
            "Provide either an invoice number or an Order No.",
            code="bad_request",
            http_status=422,
        )

    if order_no is not None:
        invoice = await packing_service.get_invoice_by_order_no(conn, order_no)
        # Routed back through the same gate as a scanned lookup so an invoice found
        # by Order No is held to identical rules — closed and already-verified are
        # refused the same way rather than only on the barcode path.
        return await packing_service.lookup_for_matching(conn, invoice["invoice_number"])

    return await packing_service.lookup_for_matching(conn, invoice_number)


@router.post("/invoices/order-no", response_model=OrderNoResult)
async def record_order_no(
    payload: OrderNoIn,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(matcher_or_ops),
):
    """PRD §5.4: attach the challan's Order No to the invoice.

    Not a control point. Nothing is gated on this value, so it takes the session
    user rather than a badge scan — the matcher's badge is spent on
    `/invoices/verify`, which is the step that actually attributes handling.
    Making them scan twice to record a field would buy no accountability.
    """
    return await packing_service.record_order_no(
        conn,
        invoice_number=payload.invoice_number,
        order_no=payload.order_no,
        source=payload.source,
        actor_id=user.id,
        raw_text=payload.raw_text,
        confidence=payload.confidence,
        was_corrected=payload.was_corrected,
    )


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


# ---------------------------------------------------------------------------
# Packing assignment and product-box scanning (Phase 5)
# ---------------------------------------------------------------------------


class PackingState(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    invoice_id: UUID
    invoice_number: str
    sku: Optional[str] = None
    required_units: int
    packed_units: int
    remaining_units: int
    ready_to_close: bool
    is_open: bool = True
    verified_by: Optional[UUID] = None
    verified_by_name: Optional[str] = None
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


class PackScanResult(ScanResult):
    """A product-box scan, plus where the carton now stands.

    Extends ScanResult rather than replacing it so the frontend's existing scan
    handling — including the offline queue's idempotent replay — applies
    unchanged.
    """

    packed_units: Optional[int] = None
    required_units: Optional[int] = None
    remaining_units: Optional[int] = None
    ready_to_close: Optional[bool] = None


@router.post("/invoices/assign", response_model=AssignResult)
async def assign_invoice(
    payload: AssignIn,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_roles("packer")),
):
    """Hand a carton to a named packer.

    Admin can do this as well as packers, since the person who matched the
    invoice is usually the one physically handing the box over.
    """
    return await packing_service.assign_invoice(
        conn, payload.invoice_number, payload.badge_code, payload.note
    )


@router.get("/invoices/{invoice_id}/packing", response_model=PackingState)
async def invoice_packing_state(
    invoice_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    return await packing_service.packing_state(conn, invoice_id)


@router.post("/invoices/{invoice_id}/pack-scan", response_model=PackScanResult)
async def scan_product_box(
    invoice_id: UUID,
    payload: ScanIn,
    response: Response,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(packer_or_ops),
):
    """Scan one product box into this carton.

    A rejected scan comes back 200 with `accepted: false`, like every other
    scanning endpoint — it is a result to render, not an error to catch, and the
    rejection is recorded either way.
    """
    result = await packing_service.scan_product_box(conn, invoice_id, payload)
    return result


@router.get("/packing/assigned-to-me", response_model=List[PackingState])
async def assigned_to_me(
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(packer_or_ops),
):
    """The packer's own queue. Nobody else's work appears here."""
    return await packing_service.assigned_to_me(conn)
