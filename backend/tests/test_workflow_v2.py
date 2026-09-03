"""Workflow corrections in migrations 0016-0018, 0036.

These cover the steps the process has always had on the floor but the software
did not: who is packing a given invoice, the guard's carton count before
anything is loaded, and Ops signing off on the truck leaving. (The fourth,
reconciling product stickers issued at the gate against product boxes packed,
was removed by 0036 — what is inside a carton is Admin's separate ERP's
concern, not this app's.)

Every one of them is a hard stop in the database rather than a check in a
service, for the reason in DECISIONS.md §B3. So these tests drive SQL directly —
if they pass, the guarantee holds regardless of what any handler does.
"""

import uuid

import pytest
from sqlalchemy import text

from app.core.errors import ControlPointError
from app.services import pickup as pickup_service
from tests.conftest import act_as, rejected

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def people(db):
    rows = await db.execute(
        text(
            """
            select employee_code, id, badge_code, role::text as role, full_name
              from profiles
             where employee_code in
                   ('EMP-M01','EMP-M02','EMP-P01','EMP-P02','EMP-O01','EMP-G01',
                    'EMP-A01','EMP-F01')
            """
        )
    )
    by_code = {r["employee_code"]: dict(r) for r in rows.mappings()}
    if "EMP-P01" not in by_code:
        pytest.skip("Seed data not loaded — run `supabase db reset`")
    return {
        "matcher": by_code["EMP-M01"],
        "matcher_b": by_code["EMP-M02"],
        "packer": by_code["EMP-P01"],
        "packer_b": by_code["EMP-P02"],
        "ops": by_code["EMP-O01"],
        "guard": by_code["EMP-G01"],
        "admin": by_code["EMP-A01"],
        # Aliases so the helpers imported from test_packing.py — which name the
        # same people matcher_a/packer_a — can be reused rather than duplicated.
        "matcher_a": by_code["EMP-M01"],
        "packer_a": by_code["EMP-P01"],
        "offloader_id": by_code["EMP-F01"]["id"],
    }


async def _invoice(db):
    # Must satisfy invoices_order_no_format (CP.........\_....): since 0036 an
    # invoice's number is the Order No that created it.
    number = f"CP{uuid.uuid4().int % 10**9:09d}_{uuid.uuid4().int % 10**4:04d}"
    invoice_id = (
        await db.execute(
            text(
                "insert into invoices (invoice_number, order_no) values (:n, :n) "
                "returning id"
            ),
            {"n": number},
        )
    ).scalar_one()
    return {"id": invoice_id, "invoice_number": number}


async def _assign(db, invoice_id, to_whom, by_whom):
    return (
        await db.execute(
            text(
                """
                insert into packing_assignments (invoice_id, assigned_to, assigned_by)
                values (:i, :t, :b) returning id
                """
            ),
            {"i": invoice_id, "t": to_whom, "b": by_whom},
        )
    ).scalar_one()


class TestPackingAssignment:
    async def test_a_lead_can_assign_by_scanning_a_packers_badge(self, db, people):
        """The floor step that was missing.

        The badge is resolved through the same one-way function a station tablet
        has always used, so this needs no relaxation of DECISIONS.md §CC2 —
        physical custody of the card is the control.
        """
        inv = await _invoice(db)
        await act_as(db, people["matcher"]["id"])

        holder = (
            await db.execute(
                text("select id, full_name from resolve_badge_holder(:c)"),
                {"c": people["packer"]["badge_code"]},
            )
        ).mappings().one()

        await _assign(db, inv["id"], holder["id"], people["matcher"]["id"])

        row = (
            await db.execute(
                text(
                    """
                    select assigned_to_name, assigned_to
                      from v_invoice_packing where invoice_id = :i
                    """
                ),
                {"i": inv["id"]},
            )
        ).mappings().one()

        assert str(row["assigned_to"]) == str(people["packer"]["id"])
        assert row["assigned_to_name"] == people["packer"]["full_name"]

    async def test_you_cannot_assign_to_yourself(self, db, people):
        """CONTROL POINT 5, caught while the lead is still holding the badge
        rather than after the carton is packed.

        Admin, not the matcher: assignment also requires the assignee to be a
        packer-eligible role (fn_packing_assignment_guard checks that before
        CONTROL POINT 5), and Admin is the one role that is always both —
        exactly the "same Admin can't self-deal" case DECISIONS.md §CC3
        describes.
        """
        inv = await _invoice(db)
        await act_as(db, people["admin"]["id"])

        async with rejected(db, containing="CONTROL POINT 5"):
            await _assign(db, inv["id"], people["admin"]["id"], people["admin"]["id"])

    async def test_a_guard_cannot_be_assigned_a_pack(self, db, people):
        inv = await _invoice(db)
        await act_as(db, people["matcher"]["id"])

        async with rejected(db, containing="not a packer"):
            await _assign(db, inv["id"], people["guard"]["id"], people["matcher"]["id"])

    async def test_a_withdrawn_badge_cannot_be_assigned(self, db, people):
        inv = await _invoice(db)
        await act_as(db, people["admin"]["id"])
        await db.execute(
            text("select admin_revoke_badge(cast(:id as uuid))"),
            {"id": people["packer"]["id"]},
        )

        async with rejected(db, containing="withdrawn"):
            await _assign(db, inv["id"], people["packer"]["id"], people["matcher"]["id"])

    async def test_a_packed_carton_cannot_be_reassigned(self, db, people, actors):
        """Otherwise "assigned to" and "packed by" can be made to disagree.

        The service refuses this, but the service is not the boundary (§B3), and
        invoices stay open until their batch is released — so `is_open` alone
        does not cover it. An assignment that contradicts the pack record would
        look like evidence while being the opposite.
        """
        from tests.test_packing import _pack

        inv = await _invoice(db)
        await act_as(db, people["matcher"]["id"])
        await _assign(db, inv["id"], people["packer"]["id"], people["matcher"]["id"])
        await _pack(db, inv["id"], people["packer"]["id"])

        async with rejected(db, containing="already been packed"):
            await _assign(db, inv["id"], people["packer_b"]["id"], people["matcher"]["id"])

    async def test_reassigning_supersedes_rather_than_edits(self, db, people):
        """PRD §7. "This box moved from Kavitha to Anitha at 14:20, by Lakshmi"
        has to remain answerable."""
        inv = await _invoice(db)
        await act_as(db, people["matcher"]["id"])

        first = await _assign(db, inv["id"], people["packer"]["id"], people["matcher"]["id"])
        second = await _assign(db, inv["id"], people["packer_b"]["id"], people["matcher"]["id"])

        rows = (
            await db.execute(
                text(
                    """
                    select id, assigned_to, is_current from packing_assignments
                     where invoice_id = :i order by assigned_at
                    """
                ),
                {"i": inv["id"]},
            )
        ).mappings().all()

        assert len(rows) == 2, "the first assignment is kept, not overwritten"
        by_id = {str(r["id"]): r for r in rows}
        assert by_id[str(first)]["is_current"] is False
        assert by_id[str(second)]["is_current"] is True


# ---------------------------------------------------------------------------
# 2. The guard's carton count, and Ops's decision on it
# ---------------------------------------------------------------------------


async def _complete_batch(db, people, count=2):
    """A batch out-scanned and complete, but not yet counted or released."""
    from tests.test_packing import _batch_of, _out_scan, _packed_invoices

    invoices = await _packed_invoices(db, people, count)
    batch_id = await _batch_of(db, invoices, people)
    for inv in invoices:
        await _out_scan(db, inv["invoice_number"], people["ops"]["id"])
    await db.execute(
        text("update batches set status = 'complete' where id = :id"), {"id": batch_id}
    )
    return batch_id, invoices


async def _count_cartons(db, batch_id, counted, who):
    return (
        await db.execute(
            text(
                """
                insert into batch_load_approvals
                  (batch_id, counted_cartons, counted_by, expected_cartons)
                values (:b, :n, :w, 0) returning id
                """
            ),
            {"b": batch_id, "n": counted, "w": who},
        )
    ).scalar_one()


async def _release(db, batch_id, who):
    await db.execute(
        text(
            """
            update batches set status = 'released', released_by = :w, released_at = now()
             where id = :id
            """
        ),
        {"w": who, "id": batch_id},
    )


class TestLoadApproval:
    async def test_nothing_is_released_before_the_guard_counts(self, db, people):
        """The new gate. Until now a batch went from out-scanned straight to the
        pickup area with no independent count of what was physically there."""
        batch_id, _ = await _complete_batch(db, people)

        async with rejected(db, containing="not been counted by a guard"):
            await _release(db, batch_id, people["ops"]["id"])

    async def test_nothing_is_released_while_the_count_is_undecided(self, db, people):
        batch_id, _ = await _complete_batch(db, people)
        await _count_cartons(db, batch_id, 2, people["guard"]["id"])

        async with rejected(db, containing="has not decided yet"):
            await _release(db, batch_id, people["ops"]["id"])

    async def test_a_rejected_count_blocks_release(self, db, people):
        batch_id, _ = await _complete_batch(db, people)
        approval = await _count_cartons(db, batch_id, 1, people["guard"]["id"])

        await db.execute(
            text(
                """
                update batch_load_approvals
                   set status = 'rejected', decided_by = :ops, note = 'One carton missing'
                 where id = :id
                """
            ),
            {"ops": people["ops"]["id"], "id": approval},
        )

        async with rejected(db, containing="rejected the carton count"):
            await _release(db, batch_id, people["ops"]["id"])

    async def test_an_approved_count_lets_the_batch_go(self, db, people):
        batch_id, _ = await _complete_batch(db, people)
        approval = await _count_cartons(db, batch_id, 2, people["guard"]["id"])
        await db.execute(
            text(
                "update batch_load_approvals set status = 'approved', decided_by = :o "
                "where id = :id"
            ),
            {"o": people["ops"]["id"], "id": approval},
        )

        await _release(db, batch_id, people["ops"]["id"])

        status = (
            await db.execute(
                text("select status::text from batches where id = :id"), {"id": batch_id}
            )
        ).scalar_one()
        assert status == "released"

    async def test_the_counter_cannot_approve_their_own_count(self, db, people):
        """The same separation as CONTROL POINT 1. A count that approves itself
        is not a check."""
        batch_id, _ = await _complete_batch(db, people)
        approval = await _count_cartons(db, batch_id, 2, people["guard"]["id"])

        async with rejected(db, containing="cannot approve their own count"):
            await db.execute(
                text(
                    "update batch_load_approvals set status = 'approved', decided_by = :g "
                    "where id = :id"
                ),
                {"g": people["guard"]["id"], "id": approval},
            )

    async def test_only_ops_may_decide_a_count(self, db, people):
        """Without this the rule above is only "name someone else", and a guard
        could nominate any colleague."""
        batch_id, _ = await _complete_batch(db, people)
        approval = await _count_cartons(db, batch_id, 2, people["guard"]["id"])

        async with rejected(db, containing="Only an Admin"):
            await db.execute(
                text(
                    "update batch_load_approvals set status = 'approved', decided_by = :p "
                    "where id = :id"
                ),
                {"p": people["packer"]["id"], "id": approval},
            )

    async def test_a_rejection_must_say_why(self, db, people):
        """"Rejected" with no reason is an argument waiting to happen on a
        loading bay at 7pm."""
        batch_id, _ = await _complete_batch(db, people)
        approval = await _count_cartons(db, batch_id, 1, people["guard"]["id"])

        async with rejected(db):
            await db.execute(
                text(
                    "update batch_load_approvals set status = 'rejected', decided_by = :o "
                    "where id = :id"
                ),
                {"o": people["ops"]["id"], "id": approval},
            )

    async def test_an_unfinished_batch_cannot_be_counted(self, db, people):
        """Counting a half-scanned batch produces a mismatch for an
        uninteresting reason, and trains everyone to approve mismatches."""
        from tests.test_packing import _batch_of, _packed_invoices

        invoices = await _packed_invoices(db, people, 2)
        batch_id = await _batch_of(db, invoices, people)

        async with rejected(db, containing="not ready to count"):
            await _count_cartons(db, batch_id, 2, people["guard"]["id"])

    async def test_the_expected_count_is_captured_not_recomputed(self, db, people):
        """The record's value is what the two numbers were when the human looked."""
        batch_id, _ = await _complete_batch(db, people, count=2)
        approval = await _count_cartons(db, batch_id, 5, people["guard"]["id"])

        row = (
            await db.execute(
                text(
                    "select counted_cartons, expected_cartons from batch_load_approvals "
                    "where id = :id"
                ),
                {"id": approval},
            )
        ).mappings().one()

        assert row["counted_cartons"] == 5
        assert row["expected_cartons"] == 2, "filled in by the trigger, not the caller"

    async def test_a_recount_supersedes_rather_than_edits(self, db, people):
        batch_id, _ = await _complete_batch(db, people)
        first = await _count_cartons(db, batch_id, 1, people["guard"]["id"])
        second = await _count_cartons(db, batch_id, 2, people["guard"]["id"])

        rows = (
            await db.execute(
                text(
                    "select id, is_current from batch_load_approvals "
                    "where batch_id = :b order by counted_at"
                ),
                {"b": batch_id},
            )
        ).mappings().all()

        assert len(rows) == 2
        by_id = {str(r["id"]): r["is_current"] for r in rows}
        assert by_id[str(first)] is False
        assert by_id[str(second)] is True


# ---------------------------------------------------------------------------
# 3. Ops approves the vehicle leaving
# ---------------------------------------------------------------------------


async def _loaded_pickup(db, people, actors, count=2):
    """A pickup with every carton scanned on and verified, ready to leave."""
    from tests.test_packing import _approve_load
    from tests.test_pickup import _exit_scan, _pickup

    batch_id, invoices = await _complete_batch(db, people, count)
    await _approve_load(db, batch_id, people)
    await _release(db, batch_id, people["ops"]["id"])

    pickup_id = await _pickup(db, batch_id, actors["guard"])
    for inv in invoices:
        await _exit_scan(db, inv["invoice_number"], actors["guard"])

    await db.execute(
        text("update pickups set status = 'verified', verified_by = :w where id = :id"),
        {"w": actors["guard"], "id": pickup_id},
    )
    return pickup_id


class TestExitApproval:
    async def test_a_verified_vehicle_cannot_simply_leave(self, db, people, actors):
        """CONTROL POINT 7 passing is no longer sufficient on its own."""
        pickup_id = await _loaded_pickup(db, people, actors)

        async with rejected(db, containing="not been approved to leave"):
            await db.execute(
                text(
                    "update pickups set status = 'departed', released_by = :w where id = :id"
                ),
                {"w": actors["guard"], "id": pickup_id},
            )

    async def test_requesting_exit_records_who_asked(self, db, people, actors):
        pickup_id = await _loaded_pickup(db, people, actors)

        await db.execute(
            text(
                "update pickups set status = 'exit_pending', exit_requested_by = :w "
                "where id = :id"
            ),
            {"w": actors["guard"], "id": pickup_id},
        )

        row = (
            await db.execute(
                text(
                    "select status::text, exit_requested_by, exit_requested_at "
                    "from pickups where id = :id"
                ),
                {"id": pickup_id},
            )
        ).mappings().one()

        assert row["status"] == "exit_pending"
        assert str(row["exit_requested_by"]) == str(actors["guard"])
        assert row["exit_requested_at"] is not None, "stamped by the trigger"

    async def test_the_gate_stays_shut_without_an_approval(self, db, people, actors):
        pickup_id = await _loaded_pickup(db, people, actors)
        await db.execute(
            text(
                "update pickups set status = 'exit_pending', exit_requested_by = :w "
                "where id = :id"
            ),
            {"w": actors["guard"], "id": pickup_id},
        )

        async with rejected(db, containing="without a recorded Ops approval"):
            await db.execute(
                text(
                    "update pickups set status = 'departed', released_by = :w where id = :id"
                ),
                {"w": actors["guard"], "id": pickup_id},
            )

    async def test_the_guard_cannot_approve_their_own_exit_request(self, db, people, actors):
        pickup_id = await _loaded_pickup(db, people, actors)
        await db.execute(
            text(
                "update pickups set status = 'exit_pending', exit_requested_by = :w "
                "where id = :id"
            ),
            {"w": actors["guard"], "id": pickup_id},
        )

        async with rejected(db, containing="cannot also approve"):
            await db.execute(
                text(
                    """
                    update pickups
                       set status = 'departed', released_by = :w,
                           exit_approved_by = :w, exit_approved_at = now()
                     where id = :id
                    """
                ),
                {"w": actors["guard"], "id": pickup_id},
            )

    async def test_only_ops_may_approve_an_exit(self, db, people, actors):
        pickup_id = await _loaded_pickup(db, people, actors)
        await db.execute(
            text(
                "update pickups set status = 'exit_pending', exit_requested_by = :w "
                "where id = :id"
            ),
            {"w": actors["guard"], "id": pickup_id},
        )

        async with rejected(db, containing="Only an Admin"):
            await db.execute(
                text(
                    """
                    update pickups
                       set status = 'departed', released_by = :g,
                           exit_approved_by = :p, exit_approved_at = now()
                     where id = :id
                    """
                ),
                {"g": actors["guard"], "p": people["packer"]["id"], "id": pickup_id},
            )

    async def test_an_approved_exit_opens_the_gate_and_stamps_time_out(
        self, db, people, actors
    ):
        pickup_id = await _loaded_pickup(db, people, actors)
        await db.execute(
            text(
                "update pickups set status = 'exit_pending', exit_requested_by = :w "
                "where id = :id"
            ),
            {"w": actors["guard"], "id": pickup_id},
        )
        await db.execute(
            text(
                "update pickups set exit_approved_by = :o, exit_approved_at = now() "
                "where id = :id"
            ),
            {"o": people["ops"]["id"], "id": pickup_id},
        )
        await db.execute(
            text("update pickups set status = 'departed', released_by = :w where id = :id"),
            {"w": actors["guard"], "id": pickup_id},
        )

        row = (
            await db.execute(
                text(
                    "select status::text, time_out, exit_approved_by from pickups where id = :id"
                ),
                {"id": pickup_id},
            )
        ).mappings().one()

        assert row["status"] == "departed"
        assert row["time_out"] is not None
        assert str(row["exit_approved_by"]) == str(people["ops"]["id"])

    async def test_a_rejected_exit_returns_the_vehicle_to_verified(self, db, people, actors):
        """So the guard can re-request once whatever Ops asked about is dealt
        with, rather than the pickup being stuck."""
        pickup_id = await _loaded_pickup(db, people, actors)
        await db.execute(
            text(
                "update pickups set status = 'exit_pending', exit_requested_by = :w "
                "where id = :id"
            ),
            {"w": actors["guard"], "id": pickup_id},
        )
        await db.execute(
            text(
                "update pickups set status = 'verified', exit_rejected_note = 'Seal missing' "
                "where id = :id"
            ),
            {"id": pickup_id},
        )

        row = (
            await db.execute(
                text("select status::text, exit_rejected_note from pickups where id = :id"),
                {"id": pickup_id},
            )
        ).mappings().one()
        assert row["status"] == "verified"
        assert row["exit_rejected_note"] == "Seal missing"

    async def test_a_rejection_revokes_an_approval_already_given(self, db, people, actors):
        """The sequence that used to open a gate with no live approval.

        Ops can approve and then change their mind while the vehicle is still on
        the pad — the status stays `exit_pending` after an approval, so a second
        decision is legitimate. The bug was that rejecting cleared the *request*
        and left the *approval* behind, so the guard could simply ask again and
        release against Ops's withdrawn consent.
        """
        pickup_id = await _loaded_pickup(db, people, actors)

        await db.execute(
            text(
                "update pickups set status = 'exit_pending', exit_requested_by = :w "
                "where id = :id"
            ),
            {"w": actors["guard"], "id": pickup_id},
        )
        await db.execute(
            text(
                "update pickups set exit_approved_by = :o, exit_approved_at = now() "
                "where id = :id"
            ),
            {"o": people["ops"]["id"], "id": pickup_id},
        )

        # Ops changes their mind. Both calls go through the service rather than
        # raw SQL, because the service is what the Ops screen calls — and it
        # reads auth.uid() for the actor, so the identity has to be assumed the
        # way a request does.
        await act_as(db, people["ops"]["id"])
        await pickup_service.decide_exit(db, pickup_id, approve=False, note="Seal missing")

        after_reject = (
            await db.execute(
                text(
                    "select status::text, exit_approved_by, exit_approved_at "
                    "from pickups where id = :id"
                ),
                {"id": pickup_id},
            )
        ).mappings().one()

        assert after_reject["status"] == "verified"
        assert after_reject["exit_approved_by"] is None, "a withdrawn approval must not survive"
        assert after_reject["exit_approved_at"] is None

        # The guard asks again. The gate must stay shut until Ops decides afresh.
        await act_as(db, actors["guard"])
        await pickup_service.request_exit(db, pickup_id)

        with pytest.raises(ControlPointError) as err:
            await pickup_service.release_vehicle(db, pickup_id)

        assert "not approved" in str(err.value) or "has not approved" in str(err.value)
