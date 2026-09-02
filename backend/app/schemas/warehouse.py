"""Request/response models for stickers, boxes, scans, exceptions (PRD §5.2-5.3, §5.9)."""

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ===========================================================================
# STICKERS
# ===========================================================================


class StickerSheetRequest(BaseModel):
    """Admin generating stickers. Quantity is never chosen by Admin for box sheets —
    it is taken from the guard's declared count, which is what makes
    CONTROL POINT 2 a comparison of two independent numbers rather than one."""

    reprint_of_id: Optional[UUID] = None
    reprint_reason: Optional[str] = Field(default=None, max_length=300)


class StickerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    # Server-rendered QR for `code` (0034) — same data the frontend used to
    # draw a canvas QR from client-side.
    qr: str
    sticker_type: str
    status: str
    sequence_no: int
    expected_units: Optional[int] = None
    box_id: Optional[UUID] = None
    box_number: Optional[int] = None
    sku: Optional[str] = None
    description: Optional[str] = None


class StickerSheetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    gate_entry_id: UUID
    sticker_type: str
    quantity: int
    generated_at: datetime
    generated_by_name: Optional[str] = None
    stickers: List[StickerOut] = Field(default_factory=list)


class StickerIssueResult(BaseModel):
    """Outcome of issuing box stickers.

    `issued: False` with a 409 means the guard's count and the PO disagree — the
    discrepancy is logged against the vendor and no stickers exist, so nothing
    can be scanned into a count that was wrong before it started.
    """

    issued: bool
    sheet: Optional[StickerSheetOut] = None
    exception_code: Optional[str] = None
    message: str


# ===========================================================================
# SCANNING
# ===========================================================================


class ScanIn(BaseModel):
    """A single scan from a device.

    `client_event_id` is minted on the device before the scan leaves it. Sending
    the same one twice is a no-op, which is what makes the offline queue safe to
    replay without the device having to know whether the first attempt landed.
    """

    client_event_id: UUID
    raw_code: str = Field(min_length=3, max_length=64)
    scanned_at: datetime
    was_offline: bool = False
    device_label: Optional[str] = Field(default=None, max_length=80)
    disposition: Optional[str] = Field(default=None, pattern="^(stock|quarantine)$")

    @field_validator("raw_code")
    @classmethod
    def _normalise(cls, v: str) -> str:
        return v.strip().upper()


class ScanBatchIn(BaseModel):
    """Offline queue drain. Order matters — replayed in the sequence scanned."""

    scans: List[ScanIn] = Field(min_length=1, max_length=500)


class ScanResult(BaseModel):
    client_event_id: UUID
    accepted: bool
    duplicate: bool = False           # already recorded; treated as success
    reject_reason: Optional[str] = None
    message: str
    box_number: Optional[int] = None
    box_id: Optional[UUID] = None
    scanned_units: Optional[int] = None
    expected_units: Optional[int] = None


class ScanBatchResult(BaseModel):
    results: List[ScanResult]
    accepted_count: int
    rejected_count: int
    duplicate_count: int


class BoxProgress(BaseModel):
    """What the box-count / unit-count pages render."""

    total: int
    scanned: int
    remaining: int
    complete: bool
    message: str


# ===========================================================================
# BOXES
# ===========================================================================


class BoxOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    box_number: int
    status: str
    sticker_code: Optional[str] = None
    expected_units: int
    scanned_units: int
    quarantined_units: int
    sku: Optional[str] = None
    description: Optional[str] = None
    damage_level: Optional[str] = None
    damage_note: Optional[str] = None
    verified_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class DamageCheckIn(BaseModel):
    """Mandatory per-box damage answer (DECISIONS.md §5).

    Answering is required; answering 'none' is allowed. Anything else needs a
    note, and the API additionally insists on a photo — the note alone is not
    evidence when the vendor disputes the claim three weeks later.
    """

    damage_level: str = Field(pattern="^(none|packaging|product)$")
    note: Optional[str] = Field(default=None, max_length=500)
    photo_paths: List[str] = Field(default_factory=list, max_length=6)

    @field_validator("note")
    @classmethod
    def _strip(cls, v):
        return v.strip() if v else v


class BoxCloseResult(BaseModel):
    box: BoxOut
    closed: bool
    exception_code: Optional[str] = None
    message: str


# ===========================================================================
# EXCEPTIONS
# ===========================================================================


class ExceptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    exception_code: str
    exception_type: str
    status: str
    title: str
    details: Dict[str, Any] = Field(default_factory=dict)

    gate_entry_id: Optional[UUID] = None
    entry_code: Optional[str] = None
    box_id: Optional[UUID] = None
    box_number: Optional[int] = None
    vendor_name: Optional[str] = None
    po_number: Optional[str] = None

    reported_by_name: Optional[str] = None
    reported_at: datetime
    escalated_at: Optional[datetime] = None

    resolution: Optional[str] = None
    resolution_note: Optional[str] = None
    resolved_by_name: Optional[str] = None
    resolved_at: Optional[datetime] = None


class ExceptionResolve(BaseModel):
    """Admin deciding a held box (DECISIONS.md §3).

    There is no 'ignore' and no 'force through'. Every path leaves a named
    person attached to a stated outcome.
    """

    resolution: str = Field(pattern="^(accept_short|recount|reject_box|accept|reject)$")
    note: str = Field(min_length=3, max_length=1000)

    @field_validator("note")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 3:
            raise ValueError("Please say what was decided and why")
        return v


class ExceptionEscalate(BaseModel):
    email_superadmin: bool = True
    note: Optional[str] = Field(default=None, max_length=1000)


class ExceptionCreate(BaseModel):
    exception_type: str = Field(
        pattern="^(box_count_mismatch|unit_count_mismatch|inbound_mismatch|damage|other)$"
    )
    title: str = Field(min_length=3, max_length=200)
    gate_entry_id: Optional[UUID] = None
    box_id: Optional[UUID] = None
    details: Dict[str, Any] = Field(default_factory=dict)


# ===========================================================================
# INBOUND RECONCILIATION (CONTROL POINT 4)
# ===========================================================================


class ReconcileLineIn(BaseModel):
    purchase_order_line_id: UUID
    inbound_count: int = Field(ge=0)


class ReconcileIn(BaseModel):
    lines: List[ReconcileLineIn] = Field(min_length=1)


class ReconcileLineOut(BaseModel):
    purchase_order_line_id: UUID
    sku: str
    description: str
    expected_units: int
    warehouse_count: int
    inbound_count: Optional[int] = None
    matched: Optional[bool] = None


class ReconcileOut(BaseModel):
    gate_entry_id: UUID
    lines: List[ReconcileLineOut]
    all_matched: bool
    message: str
    exception_code: Optional[str] = None
