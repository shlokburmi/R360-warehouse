"""Request/response models for the gate flow (PRD §5.1, §5.2)."""

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

MOBILE_RE = r"^[6-9][0-9]{9}$"
VEHICLE_RE = r"^[A-Z0-9-]{4,15}$"


class PersonIn(BaseModel):
    """One human arriving on the vehicle."""

    full_name: str = Field(min_length=2, max_length=120)
    mobile: str = Field(pattern=MOBILE_RE, description="10 digits, Indian mobile")
    visitor_role: str = Field(pattern="^(driver|laborer|supervisor)$")
    id_photo_path: Optional[str] = Field(
        default=None,
        description="Path returned by the upload endpoint. Required only for "
        "first-time visitors or when a stored photo has gone stale.",
    )

    @field_validator("full_name")
    @classmethod
    def _clean_name(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("Name cannot be empty")
        return v

    @field_validator("mobile")
    @classmethod
    def _clean_mobile(cls, v: str) -> str:
        # Guards type these on a phone keypad in the sun. Accept the shapes
        # that a real person produces and normalise rather than rejecting.
        digits = "".join(ch for ch in v if ch.isdigit())
        if digits.startswith("91") and len(digits) == 12:
            digits = digits[2:]
        if digits.startswith("0") and len(digits) == 11:
            digits = digits[1:]
        return digits


class GateEntryCreate(BaseModel):
    vehicle_number: str = Field(pattern=VEHICLE_RE)
    vendor_id: UUID
    purchase_order_id: Optional[UUID] = None
    transporter_name: Optional[str] = Field(default=None, max_length=160)
    persons: List[PersonIn] = Field(min_length=1, max_length=12)

    @field_validator("vehicle_number")
    @classmethod
    def _clean_vehicle(cls, v: str) -> str:
        return "".join(ch for ch in v.upper() if ch.isalnum() or ch == "-")

    @field_validator("persons")
    @classmethod
    def _exactly_one_driver(cls, v: List[PersonIn]) -> List[PersonIn]:
        drivers = [p for p in v if p.visitor_role == "driver"]
        if len(drivers) != 1:
            raise ValueError("Exactly one person must be marked as the driver")

        mobiles = [p.mobile for p in v]
        if len(set(mobiles)) != len(mobiles):
            raise ValueError("Two people cannot share the same mobile number")
        return v


class GateDecision(BaseModel):
    """Ops approving or rejecting an entry (CONTROL POINT 1)."""

    approve: bool
    note: Optional[str] = Field(default=None, max_length=500)

    @field_validator("note")
    @classmethod
    def _strip(cls, v):
        return v.strip() if v else v

    def require_note_on_reject(self) -> None:
        if not self.approve and not self.note:
            raise ValueError("A rejection must say why")


class BoxCountDeclare(BaseModel):
    """Guard's physical count of boxes on the truck (PRD Step 2)."""

    box_count: int = Field(ge=1, le=2000)


class PersonOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    visitor_id: UUID
    full_name: str
    mobile: str
    visitor_role: str
    has_id_photo: bool
    is_returning_visitor: bool


class GateEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    entry_code: str
    status: str
    vehicle_number: str
    vendor_id: UUID
    vendor_name: Optional[str] = None
    purchase_order_id: Optional[UUID] = None
    po_number: Optional[str] = None
    transporter_name: Optional[str] = None

    requested_by: UUID
    requested_by_name: Optional[str] = None
    requested_at: Optional[datetime] = None

    decided_by: Optional[UUID] = None
    decided_by_name: Optional[str] = None
    decided_at: Optional[datetime] = None
    decision_note: Optional[str] = None

    sla_breached: bool = False
    escalated_at: Optional[datetime] = None

    time_in: Optional[datetime] = None
    time_out: Optional[datetime] = None

    declared_box_count: Optional[int] = None
    issued_box_sticker_count: int = 0
    scanned_box_count: int = 0

    persons: List[PersonOut] = Field(default_factory=list)
    created_at: datetime


class VerifyBoxesResult(BaseModel):
    """Outcome of CONTROL POINT 2.

    A failure is reported here with `verified: False` and a 409 status, not as a
    thrown error — the exception record written alongside it has to be committed.
    """

    verified: bool
    entry: GateEntryOut
    exception_code: Optional[str] = None
    message: str


class VisitorLookup(BaseModel):
    """Answers 'do I need to photograph this person?' before the form is filled."""

    found: bool
    visitor_id: Optional[UUID] = None
    full_name: Optional[str] = None
    photo_required: bool = True
    reason: str
    last_seen_at: Optional[datetime] = None
    is_blocked: bool = False
    blocked_reason: Optional[str] = None
