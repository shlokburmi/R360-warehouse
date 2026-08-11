"""Background worker: approval SLA escalation, email dispatch, photo retention.

Run alongside the API:  python -m app.worker

Deliberately a separate process. Email delivery and SLA sweeps must never share
a request's latency budget — a guard at the gate should not wait on an SMTP
handshake, and an SMTP outage must not take the API down with it.

This worker holds the only privileged database connection in the system. It can
therefore escalate notifications on entries it does not "own", and destroy
identity photos that no user is permitted to delete — but note what it still
cannot do: there is no code path here, or anywhere, that approves a gate entry.
An unattended truck waits (docs/DECISIONS.md §4).
"""

import asyncio
import logging
import smtplib
import time
from email.message import EmailMessage
from typing import Any, Dict, List

from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import admin_transaction, dispose_engines
from app.services.gate import escalate_overdue_approvals
from app.services.retention import purge_expired_photos

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s worker: %(message)s")
log = logging.getLogger("worker")

SWEEP_SECONDS = 60

# Retention runs on its own clock. A gate approval is late after 15 minutes; a
# photo is late after 180 days. Sweeping storage every minute would be several
# thousand pointless round trips a day for a deadline measured in months.
RETENTION_SECONDS = 6 * 60 * 60


async def _recipients_for(conn, notification: Dict[str, Any]) -> List[str]:
    if notification["payload"] and notification["payload"].get("to"):
        return [notification["payload"]["to"]]

    if notification["recipient_id"]:
        rows = await conn.execute(
            text(
                """
                select u.email from auth.users u
                 join profiles p on p.id = u.id
                where p.id = :id and p.is_active
                """
            ),
            {"id": str(notification["recipient_id"])},
        )
    else:
        rows = await conn.execute(
            text(
                """
                select u.email from auth.users u
                 join profiles p on p.id = u.id
                where p.role = cast(:role as user_role) and p.is_active
                """
            ),
            {"role": notification["recipient_role"]},
        )

    return [r[0] for r in rows if r[0]]


def _send(to: List[str], subject: str, body: str) -> None:
    settings = get_settings()

    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = ", ".join(to)
    message["Subject"] = subject
    message.set_content(body)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
        if settings.smtp_user:
            smtp.starttls()
            smtp.login(settings.smtp_user, settings.smtp_password or "")
        smtp.send_message(message)


async def dispatch_emails() -> int:
    settings = get_settings()
    if not settings.smtp_host:
        return 0

    sent = 0
    async with admin_transaction("worker") as conn:
        rows = await conn.execute(
            text(
                """
                select id, recipient_id, recipient_role, title, body, payload
                  from notifications
                 where channel = 'email' and sent_at is null and send_error is null
                 order by created_at
                 limit 50
                 for update skip locked
                """
            )
        )

        for row in rows.mappings():
            recipients = await _recipients_for(conn, dict(row))

            if not recipients:
                # No mailbox to send to. Record it rather than retrying forever.
                await conn.execute(
                    text("update notifications set send_error = :err where id = :id"),
                    {"err": "no recipient email", "id": str(row["id"])},
                )
                continue

            try:
                await asyncio.to_thread(
                    _send, recipients, f"[R360 Warehouse] {row['title']}", row["body"]
                )
                await conn.execute(
                    text("update notifications set sent_at = now() where id = :id"),
                    {"id": str(row["id"])},
                )
                sent += 1
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                log.warning("Email send failed for %s: %s", row["id"], exc)
                await conn.execute(
                    text("update notifications set send_error = :err where id = :id"),
                    {"err": str(exc)[:500], "id": str(row["id"])},
                )

    return sent


async def sweep() -> None:
    async with admin_transaction("worker") as conn:
        result = await escalate_overdue_approvals(conn)
        if result["escalated"] or result["breached"]:
            log.info(
                "Gate approvals: %s escalated to backup, %s breached SLA",
                result["escalated"],
                result["breached"],
            )

    sent = await dispatch_emails()
    if sent:
        log.info("Dispatched %s email(s)", sent)


async def retention_sweep() -> None:
    """Destroy identity photos past the retention window (PRD §8)."""
    async with admin_transaction("worker") as conn:
        await purge_expired_photos(conn)


async def main() -> None:
    log.info(
        "Worker started (sweep every %ss, photo retention every %sh)",
        SWEEP_SECONDS,
        RETENTION_SECONDS // 3600,
    )

    # Run retention immediately on boot rather than waiting six hours. A process
    # that restarts more often than its own interval would otherwise never run
    # the job at all — which on a platform that recycles instances is a
    # retention policy that quietly never executes.
    last_retention = 0.0

    try:
        while True:
            try:
                await sweep()
            except Exception:  # noqa: BLE001 - a bad sweep must not kill the loop
                log.exception("Sweep failed; retrying next cycle")

            if time.monotonic() - last_retention >= RETENTION_SECONDS:
                last_retention = time.monotonic()
                try:
                    await retention_sweep()
                except Exception:  # noqa: BLE001 - same reasoning
                    log.exception("Photo retention sweep failed; retrying next cycle")

            await asyncio.sleep(SWEEP_SECONDS)
    finally:
        await dispose_engines()


if __name__ == "__main__":
    asyncio.run(main())
