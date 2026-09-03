"""Staff provisioning and attribution badges (PRD §2 "Admin", §8).

Everything here was a hand-run SQL job until now. Two of the operations have
real invariants attached and are worth reading carefully:

* **Issuing a badge** is the only path in the system that returns a badge code,
  and it returns one it has just minted. Reading an existing code is impossible
  by design (DECISIONS.md §CC2), so this module can reissue a lost badge but
  cannot tell anyone what is currently on one.

* **Deactivating an account** is refused if it would leave the warehouse with no
  active Admin, or if the Admin is deactivating themselves. Both are recoverable
  only with a psql prompt, which is exactly the thing this screen exists to
  remove the need for.

Creating an account is the one operation that reaches outside Postgres: the
account itself lives in GoTrue, and only the service-role key can create one.
See `_create_auth_user` for why that does not undermine DECISIONS.md §B1.
"""

import logging
import secrets
import string
from typing import Any, Dict, List, Optional
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.deps import ROLE_LABELS
from app.core.config import Settings
from app.core.errors import AppError
from app.services import qrcode_util
from app.schemas.admin import (
    ASSIGNABLE_ROLES,
    BADGE_ROLES,
    AdminMeta,
    BadgeIssued,
    PasswordReset,
    RoleOption,
    StaffCreate,
    StaffCreated,
    StaffOut,
    StaffUpdate,
)

log = logging.getLogger(__name__)

_STAFF_COLUMNS = """
    id, full_name, employee_code, role, is_active, is_backup_approver,
    has_badge, badge_active, badge_usable,
    invoices_verified, cartons_packed, last_attributed_at, created_at
"""


def _to_staff(row: Dict[str, Any]) -> StaffOut:
    return StaffOut(
        **row,
        role_label=ROLE_LABELS.get(row["role"], row["role"]),
        can_hold_badge=row["role"] in BADGE_ROLES,
    )


def role_options() -> AdminMeta:
    return AdminMeta(
        roles=[
            RoleOption(
                value=role,
                label=ROLE_LABELS.get(role, role),
                carries_badge=role in BADGE_ROLES,
            )
            for role in ASSIGNABLE_ROLES
        ]
    )


# ===========================================================================
# READ
# ===========================================================================


async def list_staff(conn: AsyncConnection, include_inactive: bool = True) -> List[StaffOut]:
    rows = await conn.execute(
        text(
            f"""
            select {_STAFF_COLUMNS}
              from v_staff_directory
             where cast(:include_inactive as boolean) or is_active
             order by is_active desc, role, full_name
            """
        ),
        {"include_inactive": include_inactive},
    )
    return [_to_staff(dict(r)) for r in rows.mappings()]


async def _get_staff(conn: AsyncConnection, profile_id: UUID) -> StaffOut:
    row = (
        await conn.execute(
            text(f"select {_STAFF_COLUMNS} from v_staff_directory where id = :id"),
            {"id": str(profile_id)},
        )
    ).mappings().first()

    if row is None:
        raise AppError(
            "No staff member with that id.",
            code="not_found",
            http_status=404,
        )
    return _to_staff(dict(row))


# ===========================================================================
# CREATE
# ===========================================================================


def _temporary_password() -> str:
    # Readable aloud and typeable on a phone at a gate: no l/1/O/0. Length
    # carries the strength instead of punctuation the user will mistype.
    alphabet = "".join(c for c in string.ascii_letters + string.digits if c not in "lI1O0")
    return "".join(secrets.choice(alphabet) for _ in range(14))


async def _create_auth_user(
    settings: Settings, payload: StaffCreate, password: str
) -> UUID:
    """Create the GoTrue account. The profile row appears via trigger.

    This uses the service-role key, which DECISIONS.md §B1 otherwise keeps away
    from the request path. The rule there is about the *database* connection —
    a service-role Postgres connection bypasses every RLS policy, which is why
    no route may hold one. This is a different thing: a scoped HTTP call to
    GoTrue's admin API, which can create an account and nothing else. There is
    no other way to create a user, and the route behind it is Admin-only and
    audited.

    `trg_auth_user_created` (0003) reads full_name, role, employee_code and
    mobile out of the metadata and writes the profile, so the role is correct
    from the first moment the account exists rather than being patched a
    statement later.
    """
    if not settings.supabase_service_role_key:
        raise AppError(
            "Account creation is not configured on this server.",
            code="auth_unconfigured",
            http_status=503,
            hint="Set SUPABASE_SERVICE_ROLE_KEY in the backend environment.",
        )

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            f"{settings.supabase_url}/auth/v1/admin/users",
            headers={
                "Authorization": f"Bearer {settings.supabase_service_role_key}",
                "apikey": settings.supabase_service_role_key,
                "Content-Type": "application/json",
            },
            json={
                "email": payload.email,
                "password": password,
                # There is no mailbox behind an employee address in this
                # deployment, so an unconfirmed account would simply never be
                # able to sign in. The Admin has verified the person face to
                # face, which is the thing email confirmation approximates.
                "email_confirm": True,
                "user_metadata": {
                    "full_name": payload.full_name,
                    "role": payload.role,
                    "employee_code": payload.employee_code,
                    "mobile": payload.mobile,
                },
            },
        )

    if response.status_code == 422 or response.status_code == 400:
        body = response.json() if response.content else {}
        message = str(body.get("msg") or body.get("message") or "").lower()
        if "already" in message or "registered" in message or "exists" in message:
            raise AppError(
                f"{payload.email} already has an account.",
                code="duplicate",
                http_status=409,
                hint="Change their role on the existing account instead of creating a second one.",
            )
        raise AppError(
            body.get("msg") or "That account could not be created.",
            code="auth_rejected",
            http_status=422,
        )

    if response.status_code >= 400:
        log.error("GoTrue admin create failed: %s %s", response.status_code, response.text[:400])
        raise AppError(
            "Could not create the account. Please retry.",
            code="auth_error",
            http_status=502,
        )

    user_id = response.json().get("id")
    if not user_id:
        raise AppError("The account was created but returned no id.", code="auth_error", http_status=502)
    return UUID(user_id)


async def create_staff(
    conn: AsyncConnection, settings: Settings, payload: StaffCreate
) -> StaffCreated:
    # Check the employee code before creating the account, not after. The auth
    # user is created outside this transaction and cannot be rolled back with
    # it, so a unique violation on the profile insert would leave an orphaned
    # login behind — an account with no profile, which get_current_user refuses
    # and no Admin screen would ever show.
    clash = (
        await conn.execute(
            text("select full_name from profiles where employee_code = :code"),
            {"code": payload.employee_code},
        )
    ).scalar()

    if clash is not None:
        raise AppError(
            f"Employee code {payload.employee_code} already belongs to {clash}.",
            code="duplicate",
            http_status=409,
        )

    password = _temporary_password()
    user_id = await _create_auth_user(settings, payload, password)

    # The trigger wrote the profile on GoTrue's own connection, which has
    # committed by the time the HTTP call returns. This transaction is READ
    # COMMITTED, so the row is visible to the statement below.
    staff = await _get_staff(conn, user_id)

    log.info("Admin provisioned %s (%s) as %s", staff.full_name, payload.email, staff.role)
    return StaffCreated(staff=staff, temporary_password=password)


async def _set_auth_password(settings: Settings, profile_id: UUID, password: str) -> None:
    """Overwrite the GoTrue password directly (0035) — the same admin API used
    to create the account in the first place, not a request the account holder
    approves. Unlike _delete_auth_user this is not best-effort: if it fails,
    the Admin has to know before handing over a password that doesn't work."""
    if not settings.supabase_service_role_key:
        raise AppError(
            "Password reset is not configured on this server.",
            code="auth_unconfigured",
            http_status=503,
            hint="Set SUPABASE_SERVICE_ROLE_KEY in the backend environment.",
        )

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.put(
                f"{settings.supabase_url}/auth/v1/admin/users/{profile_id}",
                headers={
                    "Authorization": f"Bearer {settings.supabase_service_role_key}",
                    "apikey": settings.supabase_service_role_key,
                    "Content-Type": "application/json",
                },
                json={"password": password},
            )
    except httpx.HTTPError as exc:
        log.error("GoTrue admin password reset errored for %s: %s", profile_id, exc)
        raise AppError(
            "Could not reach the auth service. Please retry.",
            code="auth_error",
            http_status=502,
        ) from exc

    if response.status_code in (400, 422):
        body = response.json() if response.content else {}
        # GoTrue's own password policy (min length beyond ours, breach check if
        # enabled, etc.) — surface its message rather than a generic failure,
        # same as _create_auth_user does for a duplicate email.
        raise AppError(
            body.get("msg") or body.get("message") or "That password was not accepted.",
            code="auth_rejected",
            http_status=422,
        )

    if response.status_code >= 400:
        log.error(
            "GoTrue admin password reset failed for %s: %s %s",
            profile_id,
            response.status_code,
            response.text[:400],
        )
        raise AppError(
            "Could not reset the password. Please retry.",
            code="auth_error",
            http_status=502,
        )


async def reset_password(
    conn: AsyncConnection,
    settings: Settings,
    profile_id: UUID,
    new_password: Optional[str] = None,
) -> PasswordReset:
    """Admin-only (DECISIONS.md §CE1's reasoning applies here too — this is a
    login credential, the same category as provisioning and badge issue, not
    a profile field). Ops Manager's staff CRUD access (0033) does not extend
    to this.

    `new_password` lets the Admin set a specific password instead of a random
    one. Either way it is returned once, the same as a freshly minted one —
    there is nowhere it is stored in plaintext, including when the Admin
    chose it themselves.
    """
    staff = await _get_staff(conn, profile_id)
    password = new_password or _temporary_password()
    await _set_auth_password(settings, profile_id, password)
    log.info("Admin reset the password for %s (%s)", staff.full_name, staff.employee_code)
    return PasswordReset(staff=staff, temporary_password=password)


# ===========================================================================
# UPDATE
# ===========================================================================


async def _active_admin_count(conn: AsyncConnection, excluding: UUID) -> int:
    return (
        await conn.execute(
            text(
                """
                select count(*)::int from profiles
                 where role = 'admin' and is_active and id <> :id
                """
            ),
            {"id": str(excluding)},
        )
    ).scalar_one()


async def update_staff(
    conn: AsyncConnection,
    actor_id: UUID,
    profile_id: UUID,
    payload: StaffUpdate,
) -> StaffOut:
    current = await _get_staff(conn, profile_id)

    fields: Dict[str, Any] = {}
    if payload.full_name is not None:
        fields["full_name"] = payload.full_name.strip()
    if payload.role is not None:
        fields["role"] = payload.role
    if payload.is_active is not None:
        fields["is_active"] = payload.is_active
    if payload.is_backup_approver is not None:
        fields["is_backup_approver"] = payload.is_backup_approver

    if not fields:
        return current

    role_after = fields.get("role", current.role)
    active_after = fields.get("is_active", current.is_active)
    backup_after = fields.get("is_backup_approver", current.is_backup_approver)

    # An Admin removing their own access has no way back in — only another
    # Admin can restore it. The block is here rather than in the database
    # because it is a usability rule, not an integrity one — a second Admin is
    # entirely allowed to do either thing. Scoped to current.role == "admin":
    # since 0033, a non-Admin (Ops Manager) can also reach this endpoint on
    # their own row, and losing Ops Manager access is recoverable by any Admin,
    # not just "a second one of the same kind" — that is not the lockout this
    # guards against.
    if str(profile_id) == str(actor_id) and current.role == "admin":
        if not active_after:
            raise AppError(
                "You cannot deactivate your own account.",
                code="self_lockout",
                http_status=409,
                hint="Ask another Admin to do it after they have taken over.",
            )
        if role_after != "admin":
            raise AppError(
                "You cannot change your own role away from Admin.",
                code="self_lockout",
                http_status=409,
                hint="Another Admin can do this once they are in place.",
            )

    # Last-Admin-standing. Losing this is only recoverable with a psql prompt,
    # which is the thing this screen exists to make unnecessary.
    if current.role == "admin" and current.is_active:
        if role_after != "admin" or not active_after:
            if await _active_admin_count(conn, profile_id) == 0:
                raise AppError(
                    f"{current.full_name} is the only active Admin.",
                    code="last_admin",
                    http_status=409,
                    hint="Promote someone else to Admin first.",
                )

    # DECISIONS.md §4: the T+15m escalation goes to an Admin flagged as backup.
    # Flagging anyone else produces an escalation path that silently notifies
    # nobody, which is worse than not having one.
    if backup_after and role_after != "admin":
        raise AppError(
            "Only an Admin can be a backup approver.",
            code="invalid_flag",
            http_status=422,
            hint="Gate approvals escalate to a backup Admin at T+15m (DECISIONS §4).",
        )

    # Someone who no longer holds a role that scans a badge should not keep a
    # live one. Doing this silently as part of the role change is deliberate:
    # the alternative is refusing the change and making the Admin perform two
    # steps in an order they have to know.
    if current.badge_usable and role_after not in BADGE_ROLES:
        fields["badge_active"] = False

    assignments = ", ".join(f"{k} = :{k}" for k in fields)
    params = {**fields, "id": str(profile_id)}

    result = await conn.execute(
        text(f"update profiles set {assignments} where id = :id"),
        params,
    )

    if result.rowcount == 0:
        # RLS refused the row rather than erroring. Never report success for an
        # update that did not happen — see docs/DECISIONS.md Part D.
        raise AppError(
            "You are not permitted to change this account.",
            code="not_permitted",
            http_status=403,
        )

    return await _get_staff(conn, profile_id)


# ===========================================================================
# DELETE
# ===========================================================================


async def _delete_auth_user(settings: Settings, profile_id: UUID) -> None:
    """Remove the GoTrue login (0032). Best-effort: the profile row is already
    gone by the time this runs, and get_current_user already refuses anyone
    without one, so a failure here leaves a harmless orphaned login rather
    than a usable account.

    "Best-effort" has to mean catching httpx's own exceptions too, not just a
    non-2xx status — a timeout or connection error here happens *after* the
    profile row is already deleted, so letting it propagate would roll back a
    delete that had already succeeded and hand the Admin a 500 for it.
    """
    if not settings.supabase_service_role_key:
        return

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.delete(
                f"{settings.supabase_url}/auth/v1/admin/users/{profile_id}",
                headers={
                    "Authorization": f"Bearer {settings.supabase_service_role_key}",
                    "apikey": settings.supabase_service_role_key,
                },
            )
    except httpx.HTTPError as exc:
        log.error("GoTrue admin delete errored for %s: %s", profile_id, exc)
        return

    if response.status_code >= 400 and response.status_code != 404:
        log.error(
            "GoTrue admin delete failed for %s: %s %s",
            profile_id,
            response.status_code,
            response.text[:400],
        )


async def delete_staff(
    conn: AsyncConnection, settings: Settings, actor_id: UUID, profile_id: UUID
) -> None:
    """Permanently remove an account (0032), as opposed to Deactivate.

    Every other table's FK to profiles(id) is left untouched, so this only
    succeeds for an account with no attributed activity — anything else
    surfaces as "deactivate instead" rather than a raw constraint error.
    """
    current = await _get_staff(conn, profile_id)

    if str(profile_id) == str(actor_id):
        raise AppError(
            "You cannot delete your own account.",
            code="self_lockout",
            http_status=409,
            hint="Ask another Admin to do it after they have taken over.",
        )

    if current.role == "admin" and current.is_active:
        if await _active_admin_count(conn, profile_id) == 0:
            raise AppError(
                f"{current.full_name} is the only active Admin.",
                code="last_admin",
                http_status=409,
                hint="Promote someone else to Admin first.",
            )

    try:
        async with conn.begin_nested():
            result = await conn.execute(
                text("delete from profiles where id = :id"),
                {"id": str(profile_id)},
            )
    except IntegrityError as exc:
        sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
        if sqlstate == "23503":
            raise AppError(
                f"{current.full_name} has activity history and cannot be permanently deleted.",
                code="has_history",
                http_status=409,
                hint="Deactivate the account instead — that removes their access without losing the record.",
            ) from exc
        raise

    if result.rowcount == 0:
        raise AppError(
            "You are not permitted to delete this account.",
            code="not_permitted",
            http_status=403,
        )

    await _delete_auth_user(settings, profile_id)
    log.info("Admin deleted %s (%s)", current.full_name, current.employee_code)


# ===========================================================================
# BADGES
# ===========================================================================


async def issue_badge(conn: AsyncConnection, profile_id: UUID) -> BadgeIssued:
    """Mint a fresh badge and return it exactly once.

    Every call invalidates whatever the person was carrying, because the code
    is replaced rather than read. That is what makes "lost badge" a safe
    operation: the found badge stops working the moment the replacement is
    printed.
    """
    code = (
        await conn.execute(
            text("select admin_issue_badge(cast(:id as uuid))"),
            {"id": str(profile_id)},
        )
    ).scalar_one()

    staff = await _get_staff(conn, profile_id)
    # The code is never logged. A badge code in a log file is a badge in a log
    # file.
    log.info("Admin issued a badge to %s (%s)", staff.full_name, staff.employee_code)
    return BadgeIssued(
        staff=staff,
        badge_code=code,
        badge_qr=qrcode_util.to_data_uri(code, scale=8),
    )


async def revoke_badge(conn: AsyncConnection, profile_id: UUID) -> StaffOut:
    await conn.execute(
        text("select admin_revoke_badge(cast(:id as uuid))"),
        {"id": str(profile_id)},
    )
    staff = await _get_staff(conn, profile_id)
    log.info("Admin revoked the badge of %s (%s)", staff.full_name, staff.employee_code)
    return staff


# ===========================================================================
# AUDIT
# ===========================================================================


async def account_history(
    conn: AsyncConnection, profile_id: UUID, limit: int = 50
) -> List[Dict[str, Any]]:
    """What has been done to this account, and by whom.

    Reads audit_log directly rather than a view because the interesting part is
    `changed_keys` — "badge_code" appearing there is a badge reissue, and that
    is the entry an Admin is looking for. The values themselves are redacted at
    write time (0013 §1); this is who and when, not what.
    """
    rows = await conn.execute(
        text(
            """
            select a.occurred_at, a.action, a.changed_keys, a.actor_source,
                   a.actor_role, p.full_name as actor_name
              from audit_log a
              left join profiles p on p.id = a.actor_id
             where a.table_name = 'profiles' and a.record_id = :id
             order by a.occurred_at desc
             limit :limit
            """
        ),
        {"id": str(profile_id), "limit": limit},
    )
    return [dict(r) for r in rows.mappings()]
