"""Admin: staff accounts and attribution badges (PRD §2, §8).

Every route here is Admin-only. The role model was later consolidated to four
roles (security_guard, offloading, packer, admin), folding the old ops_manager
and invoice_matcher roles into admin — so this is no longer a narrower
capability held back from a separate, weaker "Ops" tier. Provisioning and
badge issue are still gated to Admin alone, but Admin itself can now also
match invoices and approve gates, which is the tradeoff that consolidation
accepted. See DECISIONS.md §CE1.
"""

from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.deps import CurrentUser, get_db, require_admin
from app.core.config import Settings, get_settings
from app.schemas.admin import (
    AdminMeta,
    BadgeIssued,
    StaffCreate,
    StaffCreated,
    StaffOut,
    StaffUpdate,
)
from app.services import admin as admin_service
from app.services import retention as retention_service

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/meta", response_model=AdminMeta)
async def meta(user: CurrentUser = Depends(require_admin)):
    """The roles that can be assigned, and which of them carry a badge."""
    return admin_service.role_options()


@router.get("/staff", response_model=List[StaffOut])
async def list_staff(
    include_inactive: bool = Query(default=True),
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
):
    return await admin_service.list_staff(conn, include_inactive=include_inactive)


@router.post("/staff", response_model=StaffCreated, status_code=201)
async def create_staff(
    payload: StaffCreate,
    conn: AsyncConnection = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: CurrentUser = Depends(require_admin),
):
    """Provision an account. The temporary password comes back once.

    201 with a body rather than 204: the password is the only reason the Admin
    is on this screen, and it does not exist anywhere else afterwards.
    """
    return await admin_service.create_staff(conn, settings, payload)


@router.patch("/staff/{profile_id}", response_model=StaffOut)
async def update_staff(
    profile_id: UUID,
    payload: StaffUpdate,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
):
    return await admin_service.update_staff(conn, UUID(user.id), profile_id, payload)


@router.delete("/staff/{profile_id}", status_code=204)
async def delete_staff(
    profile_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: CurrentUser = Depends(require_admin),
):
    """Permanently remove an account. See admin_service.delete_staff for the
    activity-history guard that makes this fail safe rather than silently."""
    await admin_service.delete_staff(conn, settings, UUID(user.id), profile_id)


@router.post("/staff/{profile_id}/badge", response_model=BadgeIssued)
async def issue_badge(
    profile_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
):
    """Mint a badge and return it once, for printing.

    Calling this on someone who already has a badge is a *reissue*: the old
    code stops working immediately. There is no endpoint that reads an existing
    code, and there will not be one — see DECISIONS.md §CC2.
    """
    return await admin_service.issue_badge(conn, profile_id)


@router.post("/staff/{profile_id}/badge/revoke", response_model=StaffOut)
async def revoke_badge(
    profile_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
):
    return await admin_service.revoke_badge(conn, profile_id)


@router.get("/staff/{profile_id}/history")
async def account_history(
    profile_id: UUID,
    conn: AsyncConnection = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
):
    return await admin_service.account_history(conn, profile_id)


@router.get("/retention")
async def photo_retention(
    conn: AsyncConnection = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: CurrentUser = Depends(require_admin),
):
    """Identity photo retention posture (PRD §8, DPDP Act 2023).

    Read-only, deliberately. The purge itself runs in the worker, which holds
    the only connection privileged enough to delete from a bucket that grants
    DELETE to nobody. Exposing a "run it now" button here would mean handing a
    request handler that connection, which is the one thing app/db/session.py
    exists to prevent.
    """
    return await retention_service.retention_status(conn, settings)
