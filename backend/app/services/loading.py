"""The guard's carton count on a finished batch, and Ops's decision on it.

Added in Phase 5 because the process has always had this step and the software
did not: after packing, somebody physically counts the cartons on the bay, and
nothing is released to the pickup area until Ops has signed off on that number.

The shape is copied from CONTROL POINT 1 at the other end of the process, and
for the same reasons. The person who counts is not the person who approves. The
approver has to hold a role entitled to approve, not merely be a different name.
Nothing auto-approves on a timer, because a count that approves itself is not a
check — it is a delay.

`batch_load_approvals` and the release guard in 0018 are what actually enforce
all of that. The functions here exist to turn a refusal into a sentence an
operator can act on.
"""

import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.errors import AppError
from app.services import notifications

log = logging.getLogger(__name__)

_SELECT = """
    select la.id, la.batch_id, b.batch_code, b.status::text as batch_status,
           la.counted_cartons, la.expected_cartons,
           la.counted_by, cp.full_name as counted_by_name, la.counted_at,
           la.status::text as status,
           la.decided_by, dp.full_name as decided_by_name, la.decided_at,
           la.note, la.is_current,
           (la.counted_cartons = la.expected_cartons) as matches,
           extract(epoch from (now() - la.counted_at))::int as waiting_seconds
      from batch_load_approvals la
      join batches b on b.id = la.batch_id
      left join profiles cp on cp.id = la.counted_by
      left join profiles dp on dp.id = la.decided_by
"""


async def awaiting_count(conn: AsyncConnection) -> List[Dict[str, Any]]:
    """Batches that are out-scanned and waiting for a guard to count them."""
    rows = await conn.execute(
        text(
            """
            select b.id as batch_id, b.batch_code, b.status::text as batch_status,
                   b.planned_carton_count,
                   (select count(*)::int from packing_records pr where pr.batch_id = b.id)
                     as carton_count,
                   b.created_at
              from batches b
             where b.status = 'complete'
               and not exists (
                     select 1 from batch_load_approvals la
                      where la.batch_id = b.id and la.is_current
                        and la.status in ('pending', 'approved')
                   )
             order by b.created_at
            """
        )
    )
    return [dict(r) for r in rows.mappings()]


async def pending_decisions(conn: AsyncConnection) -> List[Dict[str, Any]]:
    """Counts filed by a guard and not yet decided. Oldest first — the batch
    that has waited longest is the one holding up a bay."""
    rows = await conn.execute(
        text(_SELECT + " where la.is_current and la.status = 'pending' order by la.counted_at")
    )
    return [dict(r) for r in rows.mappings()]


async def get_approval(conn: AsyncConnection, batch_id: UUID) -> Optional[Dict[str, Any]]:
    row = (
        await conn.execute(
            text(_SELECT + " where la.batch_id = :b and la.is_current"),
            {"b": str(batch_id)},
        )
    ).mappings().first()
    return dict(row) if row else None


async def count_cartons(
    conn: AsyncConnection, batch_id: UUID, counted_cartons: int
) -> Dict[str, Any]:
    """File a physical carton count against a finished batch.

    `expected_cartons` is deliberately not taken from the caller — the trigger
    fills it in from the batch. A count where the operator supplies both numbers
    is not a count.
    """
    batch = (
        await conn.execute(
            text(
                """
                select b.batch_code, b.status::text as status,
                       (select count(*)::int from packing_records pr where pr.batch_id = b.id)
                         as carton_count
                  from batches b where b.id = :id
                """
            ),
            {"id": str(batch_id)},
        )
    ).mappings().first()

    if batch is None:
        raise AppError("That batch does not exist.", code="not_found", http_status=404)

    if batch["status"] != "complete":
        raise AppError(
            f"Batch {batch['batch_code']} is not ready to count — it is {batch['status']}.",
            code="wrong_state",
            http_status=409,
            hint="Finish the out-scan first (CONTROL POINT 6).",
        )

    await conn.execute(
        text(
            """
            insert into batch_load_approvals
              (batch_id, counted_cartons, counted_by, expected_cartons)
            values (:b, :n, auth.uid(), 0)
            """
        ),
        {"b": str(batch_id), "n": counted_cartons},
    )

    approval = await get_approval(conn, batch_id)

    # Ops is told either way, but a mismatch says so in the subject line. The
    # difference between "please approve 25" and "counted 24, expected 25" is the
    # difference between a rubber stamp and a decision.
    if approval["matches"]:
        title = f"Carton count ready: {approval['batch_code']}"
        body = (
            f"{approval['counted_by_name']} counted {approval['counted_cartons']} cartons "
            f"on batch {approval['batch_code']}, matching the system. "
            "Approve to release it for loading."
        )
    else:
        title = f"Carton count MISMATCH: {approval['batch_code']}"
        body = (
            f"{approval['counted_by_name']} counted {approval['counted_cartons']} cartons "
            f"on batch {approval['batch_code']} but the system expects "
            f"{approval['expected_cartons']}. Nothing can be loaded until this is decided."
        )

    await notifications.notify(
        conn,
        title=title,
        body=body,
        recipient_role="ops_manager",
        channel="email",
    )

    log.info(
        "Carton count filed on %s: %s counted, %s expected",
        approval["batch_code"],
        approval["counted_cartons"],
        approval["expected_cartons"],
    )
    return approval


async def decide_count(
    conn: AsyncConnection, batch_id: UUID, approve: bool, note: Optional[str] = None
) -> Dict[str, Any]:
    """Ops approves or rejects the guard's count.

    A rejection must say why. "Rejected" with no reason is an argument waiting to
    happen on a loading bay at 7pm, and the database refuses it too.
    """
    approval = await get_approval(conn, batch_id)

    if approval is None:
        raise AppError(
            "Nobody has counted this batch yet.",
            code="not_found",
            http_status=404,
            hint="The guard counts the cartons first.",
        )

    if approval["status"] != "pending":
        raise AppError(
            f"This count was already {approval['status']} by {approval['decided_by_name']}.",
            code="already_decided",
            http_status=409,
        )

    if not approve and not (note or "").strip():
        raise AppError(
            "Say why the count is being rejected.",
            code="missing_field",
            http_status=422,
            hint="The guard needs to know what to recount.",
        )

    result = await conn.execute(
        text(
            """
            update batch_load_approvals
               set status = cast(:status as load_approval_status),
                   decided_by = auth.uid(),
                   decided_at = now(),
                   note = :note
             where id = :id and status = 'pending'
            """
        ),
        {"status": "approved" if approve else "rejected", "note": note, "id": str(approval["id"])},
    )

    if result.rowcount == 0:
        # RLS refused the row rather than erroring. Never report success for an
        # update that did not happen — see docs/DECISIONS.md Part D.
        raise AppError(
            "You are not permitted to decide carton counts.",
            code="not_permitted",
            http_status=403,
        )

    after = await get_approval(conn, batch_id)

    if not approve:
        await notifications.notify(
            conn,
            title=f"Carton count rejected: {after['batch_code']}",
            body=(
                f"Ops rejected the count on batch {after['batch_code']}: {note}. "
                "Recount the cartons and file again."
            ),
            recipient_role="security_guard",
            channel="in_app",
        )

    return after
