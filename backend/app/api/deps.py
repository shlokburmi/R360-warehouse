"""Request dependencies: the current user, their database connection, and RBAC.

Note the ordering of guarantees. `require_roles` produces a friendly 403 before
any work happens, but it is *not* the security boundary — RLS is. If a handler
forgets to declare its roles, the database still refuses. The dependency exists
so that the refusal arrives as a clear message instead of an empty result set.
"""

from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import Settings, get_settings
from app.core.security import get_token_claims
from app.db.session import rls_transaction

ROLE_LABELS = {
    "security_guard": "Security Guard",
    "ops_manager": "Ops Manager",
    "offloading": "Offloading Team",
    "warehouse_staff": "Warehouse Staff",
    "invoice_matcher": "Invoice Matching",
    "packer": "Packing",
    "admin": "Admin",
}

# ops_manager and admin both count as "ops" for display purposes. Admin still
# passes every require_roles() check regardless of this tuple (see below).
OPS_ROLES = ("admin", "ops_manager")


@dataclass(frozen=True)
class CurrentUser:
    id: str
    email: Optional[str]
    full_name: str
    role: str
    employee_code: Optional[str]
    is_active: bool
    claims: Dict[str, Any]

    @property
    def is_ops(self) -> bool:
        return self.role in OPS_ROLES

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, self.role)


async def get_db(
    claims: Dict[str, Any] = Depends(get_token_claims),
    settings: Settings = Depends(get_settings),
) -> AsyncIterator[AsyncConnection]:
    """One transaction per request, scoped to the caller's identity.

    Because the whole request runs in a single transaction, a handler that
    raises halfway through leaves nothing behind — no orphaned gate entry with
    no people on it, no sticker sheet with no stickers.
    """
    async with rls_transaction(claims, settings) as conn:
        yield conn


async def get_current_user(
    request: Request,
    conn: AsyncConnection = Depends(get_db),
    claims: Dict[str, Any] = Depends(get_token_claims),
) -> CurrentUser:
    row = (
        await conn.execute(
            text(
                """
                select id, full_name, role::text as role, employee_code, is_active
                  from profiles
                 where id = auth.uid()
                """
            )
        )
    ).mappings().first()

    if row is None:
        # Authenticated against GoTrue but no profile row. Either provisioning
        # failed or the account predates the trigger. Either way it is not a
        # login problem, and telling them to retry the password would waste
        # everyone's time.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": {
                    "code": "no_profile",
                    "message": "Your account has no warehouse profile yet.",
                    "hint": "Ask an Admin to assign you a role.",
                }
            },
        )

    if not row["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": {
                    "code": "account_disabled",
                    "message": "This account has been deactivated.",
                }
            },
        )

    user = CurrentUser(
        id=str(row["id"]),
        email=claims.get("email"),
        full_name=row["full_name"],
        role=row["role"],
        employee_code=row["employee_code"],
        is_active=row["is_active"],
        claims=claims,
    )
    request.state.user = user
    return user


def require_roles(*roles: str):
    """Restrict a route to the given roles (admin always passes)."""

    allowed = set(roles) | {"admin"}

    async def _guard(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role not in allowed:
            readable = ", ".join(ROLE_LABELS.get(r, r) for r in sorted(roles))
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": {
                        "code": "wrong_role",
                        "message": f"This page is for {readable}.",
                        "hint": f"You are signed in as {user.role_label}.",
                    }
                },
            )
        return user

    return _guard


# Admin-exclusive: only provisioning and badge issuance (DECISIONS.md §CE1) —
# exception resolution/escalation and the audit log moved to Ops Manager too
# (see require_ops_manager below), per PRD §8's explicit grant: "Ops Manager
# can see everything, approve exceptions, view reports."
require_ops = require_roles("admin")

# Admin is not "Admin with more buttons". Provisioning accounts and issuing
# attribution badges are the two operations an Admin must not have, since
# either one would let them manufacture the second person CONTROL POINT 5
# requires. `require_roles("admin")` reads oddly given admin is added to every
# guard, so it is named here to make the intent explicit at the call site.
require_admin = require_roles("admin")

# Ops Manager, reintroduced as a distinct role: gate/exit decisions, sticker
# sheets, out-scan, batch release, packer productivity, ID-photo viewing.
# Admin still passes (require_roles always unions with "admin").
require_ops_manager = require_roles("ops_manager")

# Invoice Matcher, reintroduced as a distinct role: invoice lookup/order-no
# capture/verify (CONTROL POINT 5 first half) and the matching-stage unit scan.
require_invoice_matcher = require_roles("invoice_matcher")

# Warehouse Staff, carved out of Offloading: putaway only. Offloading keeps
# inbound reconciliation (CONTROL POINT 4) and receiving.
require_warehouse_staff = require_roles("warehouse_staff")
