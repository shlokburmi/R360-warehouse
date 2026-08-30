"""Request/response models for the gate flow (PRD §5.1, §5.2)."""

import re
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

MOBILE_RE = r"^[6-9][0-9]{9}$"
# Exactly the shape of an Indian vehicle registration plate as this PRD wants
# it entered — state code, RTO code, series, number — e.g. KA01AB1234. Real
# plates occasionally vary (a 1-letter series, a 1-letter UT code), but the
# user asked for one fixed, compulsory format rather than accommodating every
# real-world variant, so this is deliberately exact: 2 letters, 2 digits, 2
# letters, 4 digits, nothing more.
VEHICLE_RE = r"^[A-Z]{2}[0-9]{2}[A-Z]{2}[0-9]{4}$"
# The guard's free-text PO reference (gate_entries.po_reference_note) is kept
# in the same shape real purchase_orders.po_number values already use (see
# supabase/seed.sql, e.g. PO-2026-0001) — not because the column is
# constrained to it, but so a reference typed by a guard is easy to search
# for and attach to the real PO once Ops enters it.
PO_REFERENCE_RE = r"^PO-[0-9]{4}-[0-9]{4}$"


def clean_and_validate_vehicle(v: str) -> str:
    """Normalise, then check the strict plate shape.

    Deliberately *not* a `Field(pattern=...)` constraint: Pydantic applies a
    Field-level pattern before any `@field_validator` on the same field runs
    (confirmed empirically — a validator meant to strip spaces/lowercase
    never got the chance to, because the pattern check had already rejected
    the raw input). Cleaning and validating in one place, here, is what
    actually lets "ka 01 ab 1234" become the accepted "KA01AB1234" instead of
    being rejected before it could be normalised.
    """
    cleaned = "".join(ch for ch in v.upper() if ch.isalnum())
    if not re.match(VEHICLE_RE, cleaned):
        raise ValueError(
            "Vehicle number must be exactly 2 letters, 2 digits, 2 letters, "
            "4 digits — e.g. KA01AB1234."
        )
    return cleaned


def clean_and_validate_po_reference(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    cleaned = v.strip().upper()
    if not cleaned:
        return None
    if not re.match(PO_REFERENCE_RE, cleaned):
        raise ValueError("PO reference must look like PO-2026-0001.")
    return cleaned


class PersonIn(BaseModel):
    """One human arriving on the vehicle."""

    full_name: str = Field(min_length=2, max_length=120)
    # Not `Field(pattern=MOBILE_RE)` — same reason as clean_and_validate_vehicle
    # above: a Field-level pattern is checked before _clean_mobile below ever
    # runs, so "+91 98765 43210" would be rejected before it could be
    # normalised. Cleaned and validated together in the validator instead.
    mobile: str = Field(description="10 digits, Indian mobile")
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
        if not re.match(MOBILE_RE, digits):
            raise ValueError("Mobile must be 10 digits starting 6-9.")
        return digits


class GateEntryCreate(BaseModel):
    vehicle_number: str
    vendor_id: UUID
    purchase_order_id: Optional[UUID] = None
    po_reference_note: Optional[str] = Field(default=None, max_length=120)
    transporter_name: Optional[str] = Field(default=None, max_length=160)
    persons: List[PersonIn] = Field(min_length=1, max_length=12)

    @field_validator("vehicle_number")
    @classmethod
    def _clean_vehicle(cls, v: str) -> str:
        return clean_and_validate_vehicle(v)

    @field_validator("po_reference_note")
    @classmethod
    def _clean_po_reference(cls, v: Optional[str]) -> Optional[str]:
        return clean_and_validate_po_reference(v)

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


class VendorProposeIn(BaseModel):
    """A guard registering a vendor that isn't in the system yet.

    Not a general vendor-creation form — see guard_propose_vendor() in
    0025_vendor_proposal_and_po_note.sql. The vendor is created immediately
    but unconfirmed (is_active = false); Ops/Admin confirms it by approving
    the gate entry that names it.
    """

    name: str = Field(min_length=2, max_length=160)
    mobile: Optional[str] = Field(default=None, pattern=MOBILE_RE)

    @field_validator("name")
    @classmethod
    def _clean_name(cls, v: str) -> str:
        return " ".join(v.split())


class VendorProposeOut(BaseModel):
    id: UUID
    name: str


class GateDecision(BaseModel):
    """Admin approving or rejecting an entry (CONTROL POINT 1)."""

    approve: bool
    note: Optional[str] = Field(default=None, max_length=500)

    @field_validator("note")
    @classmethod
    def _strip(cls, v):
        return v.strip() if v else v

    def require_note_on_reject(self) -> None:
        if not self.approve and not self.note:
            raise ValueError("A rejection must say why")


class GateEntryCancel(BaseModel):
    """Ops abandoning a truck's process mid-flow (0028)."""

    reason: str = Field(min_length=3, max_length=500)

    @field_validator("reason")
    @classmethod
    def _strip(cls, v):
        return v.strip()


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
    vendor_is_active: bool = True
    purchase_order_id: Optional[UUID] = None
    po_number: Optional[str] = None
    po_reference_note: Optional[str] = None
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
