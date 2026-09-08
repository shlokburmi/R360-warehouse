"""What APPROVE & PROCEED and REJECT & RETURN actually do (PRD §5.9).

fn_apply_exception_resolution (0004) applies the three *box* outcomes inside
the database, and test_control_points.py covers those. It deliberately returns
early when `box_id is null`, which left the generic pair with no consequence at
all: the exception went to 'resolved' and the truck stayed stopped at the step
that raised it, so the next attempt at that step raised the same exception
again. These tests are the other half — the effect on the gate entry.

Unlike the control-point tests, these drive the service layer, because that is
where the decision is turned into an effect. The hard stops they must not be
able to talk their way past are still asserted in SQL, at the end.
"""

import pytest
from sqlalchemy import text

from app.core.errors import AppError
from app.schemas.warehouse import ExceptionCreate
from app.services import exceptions as exc_service
from app.services import gate as gate_service
from app.services import stickers as sticker_service
from tests.conftest import act_as

pytestmark = pytest.mark.asyncio


async def _po_boxes(db, po_id):
    """How many boxes the PO expands to — the number the count is compared to."""
    return (
        await db.execute(
            text(
                """
                select coalesce(sum(ceil(expected_units::numeric / units_per_box)), 0)::int
                  from purchase_order_lines where purchase_order_id = :po
                """
            ),
            {"po": po_id},
        )
    ).scalar_one()


async def _counting(db, entry, actors, declared):
    """Move the entry to the step where the guard has declared a box count."""
    await act_as(db, actors["guard"])
    await db.execute(
        text(
            """
            update gate_entries
               set status = 'counting', declared_box_count = :n,
                   declared_by = :guard, declared_at = now()
             where id = :id
            """
        ),
        {"n": declared, "guard": actors["guard"], "id": entry["id"]},
    )


async def _open_exception(db, entry_id):
    return [
        e
        for e in await exc_service.list_exceptions(db, status_filter=["open"])
        if str(e["gate_entry_id"]) == str(entry_id)
    ][0]


class TestApproveAndProceed:
    async def test_accepting_the_count_lets_the_stickers_be_issued(
        self, db, actors, gate_entry
    ):
        """The truck brought fewer boxes than the PO, and Ops accepts that.

        The sticker step is the one that refused, so it is the one that has to
        move: exactly as many stickers as boxes actually arrived, which is also
        what keeps CONTROL POINT 2's three numbers agreeing afterwards.
        """
        declared = await _po_boxes(db, gate_entry["po_id"]) - 2
        await _counting(db, gate_entry, actors, declared)

        await act_as(db, actors["admin"])
        refused = await sticker_service.generate_box_stickers(db, gate_entry["id"])
        assert refused["issued"] is False

        exception = await _open_exception(db, gate_entry["id"])
        resolved = await exc_service.resolve_exception(
            db, exception["id"], "accept", "two boxes short, agreed with the vendor"
        )
        assert resolved["status"] == "resolved"
        assert "Issue the box stickers" in resolved["outcome"]

        issued = await sticker_service.generate_box_stickers(db, gate_entry["id"])
        assert issued["issued"] is True

        counts = (
            await db.execute(
                text(
                    """
                    select ge.declared_box_count, ge.issued_box_sticker_count,
                           (select count(*) from boxes b where b.gate_entry_id = ge.id)::int
                             as boxes
                      from gate_entries ge where ge.id = :id
                    """
                ),
                {"id": gate_entry["id"]},
            )
        ).mappings().one()
        assert counts["declared_box_count"] == declared
        assert counts["issued_box_sticker_count"] == declared
        assert counts["boxes"] == declared

    async def test_acceptance_is_tied_to_the_number_that_was_accepted(
        self, db, actors, gate_entry
    ):
        """Re-declaring the count invalidates the acceptance.

        The override is read back off the resolved exception rather than stored
        as a flag, precisely so that a *different* count cannot inherit a
        decision nobody made about it.
        """
        po_boxes = await _po_boxes(db, gate_entry["po_id"])
        await _counting(db, gate_entry, actors, po_boxes - 2)

        await act_as(db, actors["admin"])
        await sticker_service.generate_box_stickers(db, gate_entry["id"])
        exception = await _open_exception(db, gate_entry["id"])
        await exc_service.resolve_exception(db, exception["id"], "accept", "two short")

        # The guard recounts and finds a different number.
        await _counting(db, gate_entry, actors, po_boxes - 3)
        await act_as(db, actors["admin"])
        again = await sticker_service.generate_box_stickers(db, gate_entry["id"])
        assert again["issued"] is False
        assert again["exception_code"] is not None

    async def test_more_boxes_than_the_po_covers_cannot_be_accepted(
        self, db, actors, gate_entry
    ):
        """There is no PO line for the surplus, so there is nothing to receive
        it against. The refusal names the fix (amend the PO) and leaves the
        exception open for it."""
        await _counting(db, gate_entry, actors, await _po_boxes(db, gate_entry["po_id"]) + 3)

        await act_as(db, actors["admin"])
        await sticker_service.generate_box_stickers(db, gate_entry["id"])
        exception = await _open_exception(db, gate_entry["id"])

        with pytest.raises(AppError) as err:
            await exc_service.resolve_exception(
                db, exception["id"], "accept", "extra boxes came along"
            )
        assert err.value.code == "cannot_accept"
        assert "PO" in err.value.hint

        still_open = (
            await db.execute(
                text("select status::text from exceptions where id = :id"),
                {"id": exception["id"]},
            )
        ).scalar_one()
        assert still_open == "open"

    async def test_control_point_2_cannot_be_approved_away(self, db, actors, gate_entry):
        """Stickers issued but not all scanned back. No decision recorded here
        can stand in for the missing scans, and the transition trigger would
        refuse the entry anyway — so the refusal happens where it can be read."""
        po_boxes = await _po_boxes(db, gate_entry["po_id"])
        await _counting(db, gate_entry, actors, po_boxes)

        await act_as(db, actors["admin"])
        assert (await sticker_service.generate_box_stickers(db, gate_entry["id"]))["issued"]

        verified = await gate_service.verify_box_count(db, gate_entry["id"])
        assert verified["verified"] is False

        exception = await _open_exception(db, gate_entry["id"])
        with pytest.raises(AppError) as err:
            await exc_service.resolve_exception(
                db, exception["id"], "accept", "let it through, we are late"
            )
        assert err.value.code == "cannot_accept"
        assert "CONTROL POINT 2" in err.value.message

    async def test_control_point_4_cannot_be_approved_away(self, db, actors, gate_entry):
        """The inbound team's count and the warehouse's still disagree.

        Putaway is blocked by the reconciliation itself (CONTROL POINT 4), so
        approving cannot release it. Re-entering the count can, and that is
        what the refusal says.
        """
        await act_as(db, actors["admin"])
        created = await exc_service.create_exception(
            db,
            ExceptionCreate(
                exception_type="inbound_mismatch",
                title="Inbound count mismatch on 1 line",
                gate_entry_id=gate_entry["id"],
                details={"lines": [{"sku": "SKU-1", "warehouse_count": 10, "inbound_count": 8}]},
            ),
        )

        with pytest.raises(AppError) as err:
            await exc_service.resolve_exception(
                db, created["id"], "accept", "close enough, book it"
            )
        assert err.value.code == "cannot_accept"
        assert "CONTROL POINT 4" in err.value.message


class TestRejectAndReturn:
    async def test_rejecting_cancels_the_gate_entry(self, db, actors, gate_entry):
        """"Reject & return" means the goods go back with the vehicle, which is
        the sideways exit 0028 added — not a control point being skipped."""
        await _counting(db, gate_entry, actors, await _po_boxes(db, gate_entry["po_id"]) - 1)

        await act_as(db, actors["admin"])
        await sticker_service.generate_box_stickers(db, gate_entry["id"])
        exception = await _open_exception(db, gate_entry["id"])

        resolved = await exc_service.resolve_exception(
            db, exception["id"], "reject", "sending the load back to the vendor"
        )
        assert "cancelled" in resolved["outcome"]

        entry = (
            await db.execute(
                text("select status::text as status, decision_note from gate_entries where id = :id"),
                {"id": gate_entry["id"]},
            )
        ).mappings().one()
        assert entry["status"] == "cancelled"
        assert exception["exception_code"] in entry["decision_note"]

    async def test_rejecting_an_entry_already_out_of_the_flow_records_only(
        self, db, actors, gate_entry
    ):
        """There is no truck left to send anything back with. The decision still
        goes on the record; nothing pretends to reverse what already happened."""
        await act_as(db, actors["admin"])
        created = await exc_service.create_exception(
            db,
            ExceptionCreate(
                exception_type="other",
                title="Vendor paperwork never arrived",
                gate_entry_id=gate_entry["id"],
                details={},
            ),
        )

        await db.execute(
            text("update gate_entries set status = 'counting', declared_box_count = 1 where id = :id"),
            {"id": gate_entry["id"]},
        )
        await db.execute(
            text("update gate_entries set status = 'cancelled' where id = :id"),
            {"id": gate_entry["id"]},
        )

        resolved = await exc_service.resolve_exception(
            db, created["id"], "reject", "logged against the vendor for the month"
        )
        assert resolved["status"] == "resolved"
        assert resolved["outcome"] is None


class TestUnchanged:
    async def test_a_manual_exception_is_still_just_a_record(self, db, actors, gate_entry):
        """Not everything an exception describes is something the software can
        act on. Approving one of those records the decision and says so, rather
        than claiming an effect it did not have."""
        await act_as(db, actors["admin"])
        created = await exc_service.create_exception(
            db,
            ExceptionCreate(
                exception_type="other",
                title="Driver arrived without a delivery note",
                gate_entry_id=gate_entry["id"],
                details={},
            ),
        )

        resolved = await exc_service.resolve_exception(
            db, created["id"], "accept", "vendor emailed the note through"
        )
        assert resolved["status"] == "resolved"
        assert resolved["outcome"] is None

        assert (
            await db.execute(
                text("select status::text from gate_entries where id = :id"),
                {"id": gate_entry["id"]},
            )
        ).scalar_one() == "inside"

    async def test_a_box_exception_still_takes_only_the_three_box_outcomes(
        self, db, actors, gate_entry
    ):
        """DECISIONS.md §3 is untouched by any of this."""
        await act_as(db, actors["admin"])
        created = await exc_service.create_exception(
            db,
            ExceptionCreate(
                exception_type="other",
                title="Unlabelled box on the truck",
                gate_entry_id=gate_entry["id"],
                details={},
            ),
        )

        with pytest.raises(AppError) as err:
            await exc_service.resolve_exception(
                db, created["id"], "accept_short", "eight of ten is fine"
            )
        assert err.value.code == "wrong_resolution"

