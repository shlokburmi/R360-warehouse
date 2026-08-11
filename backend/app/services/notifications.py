"""Notification fan-out (PRD §9).

Notifications are written inside the same transaction as the event that caused
them. That is deliberate: if the gate entry insert rolls back, the "new truck
arrived" alert must roll back with it, or Ops chases a truck that isn't there.

Email is not sent inline. Rows with channel='email' are picked up by the
dispatcher worker, so a slow SMTP server can never make a guard wait at the gate.
"""

from typing import Any, Dict, Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

# Deliberately no RETURNING clause. Postgres applies the SELECT policy to
# INSERT ... RETURNING, and a guard raising an alert addressed to Ops cannot
# read Ops' inbox — so asking for the id back would fail on a notification the
# guard is perfectly entitled to send. Nothing needs the id anyway.
_INSERT = text(
    """
    insert into notifications
      (recipient_id, recipient_role, channel, title, body, payload,
       gate_entry_id, exception_id)
    values
      (:recipient_id, :recipient_role, :channel, :title, :body, cast(:payload as jsonb),
       :gate_entry_id, :exception_id)
    """
)


async def notify(
    conn: AsyncConnection,
    *,
    title: str,
    body: str,
    recipient_id: Optional[UUID] = None,
    recipient_role: Optional[str] = None,
    channel: str = "in_app",
    payload: Optional[Dict[str, Any]] = None,
    gate_entry_id: Optional[UUID] = None,
    exception_id: Optional[UUID] = None,
) -> None:
    import json

    if recipient_id is None and recipient_role is None:
        raise ValueError("A notification needs a recipient or a role")

    await conn.execute(
        _INSERT,
        {
            "recipient_id": str(recipient_id) if recipient_id else None,
            "recipient_role": recipient_role,
            "channel": channel,
            "title": title,
            "body": body,
            "payload": json.dumps(payload or {}),
            "gate_entry_id": str(gate_entry_id) if gate_entry_id else None,
            "exception_id": str(exception_id) if exception_id else None,
        },
    )


async def notify_ops(conn: AsyncConnection, *, title: str, body: str, **kw) -> None:
    """In-app alert to every Ops Manager, plus a queued email.

    Both, not either: the in-app alert is what gets seen during a shift, the
    email is what gets seen when nobody is looking at the dashboard.
    """
    await notify(conn, title=title, body=body, recipient_role="ops_manager", **kw)
    await notify(
        conn, title=title, body=body, recipient_role="ops_manager", channel="email", **kw
    )


async def notify_admin(conn: AsyncConnection, *, title: str, body: str, **kw) -> None:
    await notify(conn, title=title, body=body, recipient_role="admin", **kw)
    await notify(
        conn, title=title, body=body, recipient_role="admin", channel="email", **kw
    )


async def unread_for_user(conn: AsyncConnection, limit: int = 50):
    rows = await conn.execute(
        text(
            """
            select id, title, body, payload, created_at, gate_entry_id, exception_id
              from notifications
             where channel = 'in_app'
               and read_at is null
               and (recipient_id = auth.uid() or recipient_role = auth_role())
             order by created_at desc
             limit :limit
            """
        ),
        {"limit": limit},
    )
    return [dict(r) for r in rows.mappings()]


async def mark_read(conn: AsyncConnection, notification_id: UUID) -> None:
    await conn.execute(
        text("update notifications set read_at = now() where id = :id and read_at is null"),
        {"id": str(notification_id)},
    )
