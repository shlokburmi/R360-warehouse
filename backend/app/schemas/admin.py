"""Request and response shapes for the Admin screen."""

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

# The roles an Admin may assign. Deliberately the full enum: there is no
# "Admin cannot create another Admin" rule, because the alternative is a
# warehouse with exactly one person who can fix anything, on leave.
ASSIGNABLE_ROLES = (
    "security_guard",
    "ops_manager",
    "offloading",
    "warehouse_staff",
    "invoice_matcher",
    "packer",
    "admin",
)

# Kept in step with fn_badge_holder_guard() in 0009/0023. The database is the
# authority; this copy exists only so the form can grey the button out instead
# of letting the Admin discover the rule by hitting it.
BADGE_ROLES = ("packer", "invoice_matcher", "admin")


class StaffOut(BaseModel):
    id: UUID
    full_name: str
    employee_code: Optional[str] = None
    role: str
    role_label: str
    is_active: bool
    is_backup_approver: bool
    # Never the badge code itself — see DECISIONS.md §CC2. Only whether one
    # exists, and whether it currently opens anything.
    has_badge: bool
    badge_active: bool
    badge_usable: bool
    can_hold_badge: bool
    invoices_verified: int
    cartons_packed: int
    last_attributed_at: Optional[datetime] = None
    created_at: datetime


class StaffCreate(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    full_name: str = Field(min_length=2, max_length=120)
    role: str
    employee_code: str = Field(min_length=2, max_length=20)
    mobile: Optional[str] = None

    @field_validator("email")
    @classmethod
    def _email_shape(cls, value: str) -> str:
        # Not a full RFC 5322 check — GoTrue is the authority on what it will
        # accept, and duplicating its rules here would only produce two
        # different answers to the same question. This catches the typo that
        # would otherwise cost a round trip.
        email = value.strip().lower()
        local, _, domain = email.partition("@")
        if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
            raise ValueError("That does not look like an email address.")
        return email

    @field_validator("role")
    @classmethod
    def _known_role(cls, value: str) -> str:
        if value not in ASSIGNABLE_ROLES:
            raise ValueError(f"Unknown role: {value}")
        return value

    @field_validator("employee_code")
    @classmethod
    def _code_shape(cls, value: str) -> str:
        code = value.strip().upper()
        if not code.replace("-", "").isalnum():
            raise ValueError("Employee code may contain letters, digits and hyphens only.")
        return code

    @field_validator("mobile")
    @classmethod
    def _mobile_shape(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        digits = "".join(ch for ch in value if ch.isdigit())
        # Matches the CHECK on profiles.mobile. Rejecting here produces a
        # readable field error instead of a constraint violation after the
        # auth user has already been created.
        if len(digits) != 10 or digits[0] not in "6789":
            raise ValueError("Mobile must be 10 digits starting 6-9.")
        return digits


class StaffCreated(BaseModel):
    staff: StaffOut
    # Shown once, then unrecoverable — the Admin hands it over in person and the
    # user changes it. Storing it anywhere would make this a password database.
    temporary_password: str


class StaffUpdate(BaseModel):
    """Every field optional; only what is sent is changed."""

    full_name: Optional[str] = Field(default=None, min_length=2, max_length=120)
    role: Optional[str] = None
    is_active: Optional[bool] = None
    is_backup_approver: Optional[bool] = None

    @field_validator("role")
    @classmethod
    def _known_role(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in ASSIGNABLE_ROLES:
            raise ValueError(f"Unknown role: {value}")
        return value


class BadgeIssued(BaseModel):
    staff: StaffOut
    # The one place in the system a badge code is ever returned, and only for a
    # badge minted in this request. See 0013_admin_provisioning.sql §2.
    badge_code: str


class RoleOption(BaseModel):
    value: str
    label: str
    carries_badge: bool


class AdminMeta(BaseModel):
    roles: List[RoleOption]
