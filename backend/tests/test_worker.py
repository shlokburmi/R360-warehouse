"""The background worker's sweeps (DECISIONS.md §4).

These tests exist because the SLA escalation was broken from the start and
nothing said so. `escalate_overdue_approvals` bound its interval as a string
into `cast(:sla as interval)`, which asyncpg rejects — so the first statement of
every sweep raised, the worker logged "Sweep failed; retrying next cycle", and
did that once a minute forever. No approval was ever escalated, and because the
raise happened before `dispatch_emails()`, no email was ever sent either.

The whole suite was green throughout, because nothing called the sweep. So the
test that matters most here is the least interesting one: that the sweep runs at
all against a real connection.
"""

import pytest
from sqlalchemy import text

from app.services.gate import escalate_overdue_approvals
from app.worker import sweep
from tests.conftest import act_as

pytestmark = pytest.mark.asyncio


async def _overdue_entry(db, actors, *, minutes_ago: int):
    """A gate entry that has been waiting `minutes_ago` for a decision."""
    await act_as(db, actors["guard"])
    po = (
        await db.execute(
            text("select id, vendor_id from purchase_orders where po_number = 'PO-2026-0001'")
        )
    ).mappings().one()

    entry_id = (
        await db.execute(
            text(
                """
                insert into gate_entries
                  (status, vehicle_number, vendor_id, purchase_order_id,
                   requested_by, requested_at)
                values ('pending_approval', :vehicle, :vendor, :po, :guard,
                        now() - make_interval(mins => :mins))
                returning id
                """
            ),
            {
                "vehicle": f"KA01ZZ{minutes_ago:04d}",
                "vendor": po["vendor_id"],
                "po": po["id"],
                "guard": actors["guard"],
                "mins": minutes_ago,
            },
        )
    ).scalar_one()
    return entry_id


class TestApprovalEscalation:
    async def test_the_sweep_runs(self, db, actors):
        """The regression that matters. It is not about the numbers — it is
        that the statement executes at all against asyncpg."""
        result = await escalate_overdue_approvals(db)

        assert set(result) == {"escalated", "breached"}

    async def test_an_entry_waiting_past_the_sla_escalates_to_backup(self, db, actors):
        entry_id = await _overdue_entry(db, actors, minutes_ago=20)

        result = await escalate_overdue_approvals(db)

        row = (
            await db.execute(
                text("select escalated_at, sla_breached, status::text from gate_entries where id = :id"),
                {"id": entry_id},
            )
        ).mappings().one()

        assert result["escalated"] >= 1
        assert row["escalated_at"] is not None
        assert row["sla_breached"] is False
        # DECISIONS.md §4: the timer escalates the notification, never the
        # decision. A truck can wait; an unapproved truck entering cannot be
        # undone.
        assert row["status"] == "pending_approval"

    async def test_an_entry_past_the_hard_limit_is_flagged_for_admin(self, db, actors):
        entry_id = await _overdue_entry(db, actors, minutes_ago=45)

        result = await escalate_overdue_approvals(db)

        row = (
            await db.execute(
                text("select sla_breached, status::text from gate_entries where id = :id"),
                {"id": entry_id},
            )
        ).mappings().one()

        assert result["breached"] >= 1
        assert row["sla_breached"] is True
        assert row["status"] == "pending_approval", "still not auto-approved"

    async def test_a_fresh_entry_is_left_alone(self, db, actors):
        entry_id = await _overdue_entry(db, actors, minutes_ago=2)

        await escalate_overdue_approvals(db)

        row = (
            await db.execute(
                text("select escalated_at, sla_breached from gate_entries where id = :id"),
                {"id": entry_id},
            )
        ).mappings().one()

        assert row["escalated_at"] is None
        assert row["sla_breached"] is False

    async def test_escalating_twice_does_not_notify_twice(self, db, actors):
        """`escalated_at is null` is the guard. Without it an unattended truck
        would email Ops every minute until someone decided, which trains people
        to filter the alert."""
        await _overdue_entry(db, actors, minutes_ago=20)

        first = await escalate_overdue_approvals(db)
        second = await escalate_overdue_approvals(db)

        assert first["escalated"] >= 1
        assert second["escalated"] == 0

    async def test_it_notifies_ops_rather_than_deciding(self, db, actors):
        entry_id = await _overdue_entry(db, actors, minutes_ago=20)

        await escalate_overdue_approvals(db)

        notified = (
            await db.execute(
                text(
                    """
                    select count(*)::int from notifications
                     where gate_entry_id = :id and recipient_role = 'ops_manager'
                    """
                ),
                {"id": entry_id},
            )
        ).scalar_one()

        assert notified >= 1


class TestFullSweep:
    async def test_a_whole_sweep_completes(self, db, actors):
        """`sweep()` opens its own privileged transaction rather than using the
        test's, so this genuinely exercises the worker's path — including the
        email dispatcher that the escalation bug used to prevent reaching.

        It commits, unlike everything else in this suite. That is why it only
        creates notifications, which are additive and harmless.
        """
        await sweep()
