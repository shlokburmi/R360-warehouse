"""Session, master data, notifications and photo uploads."""

import secrets
from datetime import date, datetime
from typing import Dict, List, Optional
from uuid import UUID

import httpx
from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.deps import (
    CurrentUser,
    get_current_user,
    get_db,
    require_ops_manager,
    require_roles,
)
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.services import notifications as notif_service

router = APIRouter(tags=["meta"])


# ===========================================================================
# SESSION
# ===========================================================================


class MeOut(BaseModel):
    id: UUID
    full_name: str
    role: str
    role_label: str
    employee_code: Optional[str] = None
    email: Optional[str] = None
    # What the frontend uses to decide which nav items exist at all. The API
    # still enforces access independently — this only shapes the UI.
    allowed_pages: List[str]


# Which pages each role can reach (PRD §8). A guard sees gate pages and nothing
# else; the point of listing it here is that the navigation is derived from one
# table rather than scattered across components.
PAGE_ACCESS: Dict[str, List[str]] = {
    # Guard still declares the box count on this page (Step 1); the scanning
    # steps on it now belong to packers.
    "security_guard": ["gate-entry", "box-counting", "pickup", "my-entries", "loading"],
    # Ops Manager, reintroduced: PRD §8 — "can see everything, approve
    # exceptions, view reports". Staff add/edit/delete on the admin screen was
    # Admin-only per DECISIONS.md §CE1/§CH1; 0033_ops_manager_staff_admin.sql
    # reverses that specifically, knowingly reopening the fraud vector those
    # sections describe. Badge issue/revoke and audit history stay Admin-only.
    "ops_manager": [
        "dashboard", "approvals", "stickers", "exceptions", "reports",
        "gate-entry", "pickup", "invoices", "batches", "loading",
        # Issuing box/unit sticker sheets is require_ops_manager-gated
        # (warehouse.py generate_box_stickers/generate_unit_stickers) — the
        # nav needs to actually reach the page that action lives on.
        "box-counting", "unit-scanning",
        "admin",
    ],
    # Offloading no longer scans goods in — packers do (see scans_insert in
    # 0019) — and no longer shelves goods either, now that warehouse_staff is
    # its own role again. Reconciliation (CONTROL POINT 4) and receiving stay.
    "offloading": ["exceptions", "reconciliation"],
    # Carved back out of offloading: putaway only.
    "warehouse_staff": ["exceptions", "putaway", "stock"],
    # Invoice Matching, reintroduced: matching, the exceptions anyone can
    # raise, and "packing" — the /invoices/assign handover step (PRD §7: "call
    # a packing lady while handing them over") lives on that page, and
    # packing_assignments_insert now names invoice_matcher explicitly.
    "invoice_matcher": ["exceptions", "invoice-matching", "packing"],
    # Packers apply and scan both box and unit stickers at intake, in addition
    # to their outbound packing job.
    "packer": ["box-counting", "unit-scanning", "exceptions", "packing"],
    # Admin's actual real-world use here is oversight — approve/decline and
    # track activity/logs — not hands-on station work. The backend still
    # accepts admin on every operational endpoint (require_roles always
    # unions with admin) as an emergency fallback if a role's own account is
    # unavailable; this list only controls what shows up in the nav day to
    # day, not what's actually permitted.
    "admin": ["dashboard", "approvals", "exceptions", "reports", "admin"],
}


@router.get("/me", response_model=MeOut)
async def me(user: CurrentUser = Depends(get_current_user)):
    return MeOut(
        id=user.id,
        full_name=user.full_name,
        role=user.role,
        role_label=user.role_label,
        employee_code=user.employee_code,
        email=user.email,
        allowed_pages=PAGE_ACCESS.get(user.role, []),
    )


# ===========================================================================
# MASTER DATA
# ===========================================================================


@router.get("/vendors")
async def list_vendors(
    q: Optional[str] = Query(default=None),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    rows = await conn.execute(
        text(
            """
            select id, code, name from vendors
             where is_active
               and (cast(:q as text) is null or name ilike '%' || cast(:q as text) || '%'
                     or code ilike '%' || cast(:q as text) || '%')
             order by name
             limit 100
            """
        ),
        {"q": q},
    )
    return [dict(r) for r in rows.mappings()]


@router.get("/purchase-orders")
async def list_purchase_orders(
    vendor_id: Optional[UUID] = Query(default=None),
    open_only: bool = Query(default=True),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    rows = await conn.execute(
        text(
            """
            select po.id, po.po_number, po.status::text as status, po.expected_on,
                   v.name as vendor_name, v.id as vendor_id,
                   coalesce(sum(pol.expected_units), 0)::int as expected_units,
                   count(pol.id)::int as line_count
              from purchase_orders po
              join vendors v on v.id = po.vendor_id
              left join purchase_order_lines pol on pol.purchase_order_id = po.id
             where (cast(:vendor_id as uuid) is null or po.vendor_id = cast(:vendor_id as uuid))
               and (not cast(:open_only as boolean) or po.status in ('open', 'partially_received'))
             group by po.id, po.po_number, po.status, po.expected_on, v.name, v.id
             order by po.expected_on desc nulls last, po.po_number
             limit 100
            """
        ),
        {"vendor_id": str(vendor_id) if vendor_id else None, "open_only": open_only},
    )
    return [dict(r) for r in rows.mappings()]


class POLineIn(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=200)
    expected_units: int = Field(gt=0)
    units_per_box: int = Field(gt=0)

    @field_validator("sku", "description")
    @classmethod
    def _strip(cls, v):
        return v.strip()


class POLineUpdate(BaseModel):
    """Partial update — only fields actually sent get changed."""

    sku: Optional[str] = Field(default=None, min_length=1, max_length=64)
    description: Optional[str] = Field(default=None, min_length=1, max_length=200)
    expected_units: Optional[int] = Field(default=None, gt=0)
    units_per_box: Optional[int] = Field(default=None, gt=0)

    @field_validator("sku", "description")
    @classmethod
    def _strip(cls, v):
        return v.strip() if v else v


class PurchaseOrderCreate(BaseModel):
    po_number: str = Field(min_length=1, max_length=64)
    vendor_id: UUID
    expected_on: Optional[date] = None
    lines: List[POLineIn] = Field(min_length=1)

    @field_validator("po_number")
    @classmethod
    def _strip_po(cls, v):
        return v.strip()


@router.post("/purchase-orders", status_code=201)
async def create_purchase_order(
    payload: PurchaseOrderCreate,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_ops_manager),
):
    """Ops enters a PO by hand — the counterpart to a guard's manual PO note
    (0025_vendor_proposal_and_po_note.sql): the note says a PO exists on paper
    but isn't in the system yet, and this is how it actually gets in."""
    po_id = (
        await conn.execute(
            text(
                """
                insert into purchase_orders (po_number, vendor_id, expected_on, created_by)
                values (:po_number, :vendor_id, :expected_on, auth.uid())
                returning id
                """
            ),
            {
                "po_number": payload.po_number,
                "vendor_id": str(payload.vendor_id),
                "expected_on": payload.expected_on,
            },
        )
    ).scalar_one()

    for line_no, line in enumerate(payload.lines, start=1):
        await conn.execute(
            text(
                """
                insert into purchase_order_lines
                  (purchase_order_id, line_no, sku, description, expected_units, units_per_box)
                values (:po_id, :line_no, :sku, :description, :expected_units, :units_per_box)
                """
            ),
            {
                "po_id": str(po_id),
                "line_no": line_no,
                "sku": line.sku,
                "description": line.description,
                "expected_units": line.expected_units,
                "units_per_box": line.units_per_box,
            },
        )

    return {"id": str(po_id), "po_number": payload.po_number}


@router.get("/purchase-orders/{po_id}/lines")
async def po_lines(
    po_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    rows = await conn.execute(
        text(
            """
            select id, line_no, sku, description, expected_units, units_per_box,
                   received_units, rejected_units,
                   ceil(expected_units::numeric / units_per_box)::int as expected_boxes
              from purchase_order_lines
             where purchase_order_id = :po_id
             order by line_no
            """
        ),
        {"po_id": str(po_id)},
    )
    return [dict(r) for r in rows.mappings()]


@router.post("/purchase-orders/{po_id}/lines", status_code=201)
async def add_purchase_order_line(
    po_id: UUID,
    payload: POLineIn,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_ops_manager),
):
    """A line Ops missed when first entering the PO by hand."""
    next_line_no = (
        await conn.execute(
            text(
                "select coalesce(max(line_no), 0) + 1"
                "  from purchase_order_lines where purchase_order_id = :po_id"
            ),
            {"po_id": str(po_id)},
        )
    ).scalar_one()

    line_id = (
        await conn.execute(
            text(
                """
                insert into purchase_order_lines
                  (purchase_order_id, line_no, sku, description, expected_units, units_per_box)
                values (:po_id, :line_no, :sku, :description, :expected_units, :units_per_box)
                returning id
                """
            ),
            {
                "po_id": str(po_id),
                "line_no": next_line_no,
                "sku": payload.sku,
                "description": payload.description,
                "expected_units": payload.expected_units,
                "units_per_box": payload.units_per_box,
            },
        )
    ).scalar_one()

    return {"id": str(line_id), "line_no": next_line_no}


@router.patch("/purchase-order-lines/{line_id}")
async def update_purchase_order_line(
    line_id: UUID,
    payload: POLineUpdate,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_ops_manager),
):
    """Ops corrects a mistyped line — e.g. units_per_box entered wrong when the
    PO was first keyed in by hand. Refused once any box already references
    this line: at that point boxes/stickers already exist against the old
    numbers, and silently changing what they meant would be worse than the
    original typo (0025/0028's manual-PO-entry gap made this a real need)."""
    in_use = (
        await conn.execute(
            text("select 1 from boxes where purchase_order_line_id = :id limit 1"),
            {"id": str(line_id)},
        )
    ).first()

    if in_use is not None:
        raise AppError(
            "This line already has boxes counted against it and can no longer be edited.",
            code="line_in_use",
            http_status=409,
            hint="Create a new PO line instead, or contact an Admin.",
        )

    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise AppError("Nothing to update.", code="no_fields", http_status=422)

    set_clause = ", ".join(f"{col} = :{col}" for col in fields)
    result = await conn.execute(
        text(f"update purchase_order_lines set {set_clause} where id = :id"),
        {**fields, "id": str(line_id)},
    )

    if result.rowcount == 0:
        raise AppError("Purchase order line not found.", code="not_found", http_status=404)

    row = (
        await conn.execute(
            text(
                """
                select id, line_no, sku, description, expected_units, units_per_box,
                       received_units, rejected_units,
                       ceil(expected_units::numeric / units_per_box)::int as expected_boxes
                  from purchase_order_lines where id = :id
                """
            ),
            {"id": str(line_id)},
        )
    ).mappings().one()

    return dict(row)


# ===========================================================================
# NOTIFICATIONS
# ===========================================================================


@router.get("/notifications")
async def list_notifications(
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    return await notif_service.unread_for_user(conn)


@router.post("/notifications/{notification_id}/read")
async def mark_notification_read(
    notification_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    await notif_service.mark_read(conn, notification_id)
    return {"ok": True}


# ===========================================================================
# PHOTO UPLOADS
#
# The backend never proxies image bytes — it hands out a short-lived signed URL
# and the device uploads straight to Supabase Storage. A guard on a weak mobile
# connection uploading a 4MB photo should not be occupying an API worker for
# thirty seconds.
# ===========================================================================


class UploadTicket(BaseModel):
    path: str
    upload_url: str
    token: str
    expires_in: int


async def _signed_upload(settings: Settings, bucket: str, path: str) -> UploadTicket:
    if not settings.supabase_service_role_key:
        raise AppError(
            "Photo upload is not configured on this server.",
            code="storage_unconfigured",
            http_status=503,
            hint="Set SUPABASE_SERVICE_ROLE_KEY in the backend environment.",
        )

    url = f"{settings.supabase_url}/storage/v1/object/upload/sign/{bucket}/{path}"

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {settings.supabase_service_role_key}",
                "Content-Type": "application/json",
            },
            json={"expiresIn": settings.signed_url_ttl_seconds},
        )

    if response.status_code >= 400:
        raise AppError(
            "Could not prepare the photo upload. Please retry.",
            code="storage_error",
            http_status=502,
            details={"status": response.status_code},
        )

    body = response.json()
    signed = body.get("url", "")
    token = signed.split("token=")[-1] if "token=" in signed else body.get("token", "")

    return UploadTicket(
        path=path,
        upload_url=f"{settings.supabase_url}/storage/v1{signed}",
        token=token,
        expires_in=settings.signed_url_ttl_seconds,
    )


@router.post("/uploads/identity-photo", response_model=UploadTicket)
async def identity_photo_ticket(
    mobile: str = Body(embed=True, min_length=10, max_length=13),
    settings: Settings = Depends(get_settings),
    user: CurrentUser = Depends(require_roles("security_guard")),
):
    """A one-shot upload slot for a visitor's ID photo.

    The path is scoped by mobile number and salted, so a guard cannot guess
    another visitor's object path — and even with the path, storage RLS denies
    them read access (see 0006_storage.sql).
    """
    digits = "".join(ch for ch in mobile if ch.isdigit())[-10:]
    stamp = datetime.utcnow().strftime("%Y%m%d")
    path = f"{digits}/{stamp}-{secrets.token_hex(6)}.jpg"
    return await _signed_upload(settings, "identity-photos", path)


@router.post("/uploads/damage-photo", response_model=UploadTicket)
async def damage_photo_ticket(
    box_id: UUID = Body(embed=True),
    settings: Settings = Depends(get_settings),
    user: CurrentUser = Depends(require_roles("packer")),
):
    path = f"{box_id}/{secrets.token_hex(6)}.jpg"
    return await _signed_upload(settings, "damage-photos", path)


@router.get("/uploads/identity-photo/view")
async def view_identity_photo(
    path: str = Query(min_length=3),
    settings: Settings = Depends(get_settings),
    user: CurrentUser = Depends(require_ops_manager),
):
    """Short-lived signed link to a stored ID photo. Ops and Admin only —
    PRD §8 Data Privacy / DPDP Act 2023."""
    if not settings.supabase_service_role_key:
        raise AppError("Storage is not configured.", code="storage_unconfigured", http_status=503)

    url = f"{settings.supabase_url}/storage/v1/object/sign/identity-photos/{path}"

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {settings.supabase_service_role_key}"},
            json={"expiresIn": settings.signed_url_ttl_seconds},
        )

    if response.status_code >= 400:
        raise AppError("Photo not found.", code="not_found", http_status=404)

    signed = response.json().get("signedURL", "")
    return {
        "url": f"{settings.supabase_url}/storage/v1{signed}",
        "expires_in": settings.signed_url_ttl_seconds,
    }
