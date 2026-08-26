"""The four workflow corrections in migrations 0016-0019.

These cover the steps the process has always had on the floor but the software
did not: who is packing a given invoice, the guard's carton count before
anything is loaded, Ops signing off on the truck leaving, and the reconciliation
between product stickers issued at the gate and product boxes actually packed.

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


async def _invoice(db, units=2):
    line = (
        await db.execute(
            text(
                """
                select pol.id, pol.sku from purchase_order_lines pol
                  join purchase_orders po on po.id = pol.purchase_order_id
                 where po.po_number = 'PO-2026-0001'
                 order by pol.line_no limit 1
                """
            )
        )
    ).mappings().one()

    number = f"INV-WF-{uuid.uuid4().hex[:10].upper()}"
    invoice_id = (
        await db.execute(
            text(
                """
                insert into invoices
                  (invoice_number, purchase_order_line_id, sku, units, customer_name)
                values (:n, :l, :s, :u, 'Test Customer')
                returning id
                """
            ),
            {"n": number, "l": line["id"], "s": line["sku"], "u": units},
        )
    ).scalar_one()
    return {"id": invoice_id, "invoice_number": number, "units": units}


async def _verify(db, invoice_id, who):
    await db.execute(
        text("insert into invoice_verifications (invoice_id, verified_by) values (:i, :w)"),
        {"i": invoice_id, "w": who},
    )


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
        await _verify(db, inv["id"], people["matcher"]["id"])

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

    async def test_an_unverified_invoice_cannot_be_assigned(self, db, people):
        """The small box only reaches a bench because a matcher put it with its
        invoice, so an unverified invoice has not physically been matched."""
        inv = await _invoice(db)
        await act_as(db, people["matcher"]["id"])

        async with rejected(db, containing="not been matched"):
            await _assign(db, inv["id"], people["packer"]["id"], people["matcher"]["id"])

    async def test_the_verifier_cannot_be_assigned_the_pack(self, db, people):
        """CONTROL POINT 5, caught while the lead is still holding the badge
        rather than after the carton is packed."""
        inv = await _invoice(db)
        await act_as(db, people["ops"]["id"])
        await _verify(db, inv["id"], people["ops"]["id"])

        async with rejected(db, containing="CONTROL POINT 5"):
            await _assign(db, inv["id"], people["ops"]["id"], people["ops"]["id"])

    async def test_a_guard_cannot_be_assigned_a_pack(self, db, people):
        inv = await _invoice(db)
        await act_as(db, people["matcher"]["id"])
        await _verify(db, inv["id"], people["matcher"]["id"])

        async with rejected(db, containing="not a packer"):
            await _assign(db, inv["id"], people["guard"]["id"], people["matcher"]["id"])

    async def test_a_withdrawn_badge_cannot_be_assigned(self, db, people):
        inv = await _invoice(db)
        await act_as(db, people["admin"]["id"])
        await db.execute(
            text("select admin_revoke_badge(cast(:id as uuid))"),
            {"id": people["packer"]["id"]},
        )
        await _verify(db, inv["id"], people["matcher"]["id"])

        async with rejected(db, containing="withdrawn"):
            await _assign(db, inv["id"], people["packer"]["id"], people["matcher"]["id"])

    async def test_a_packed_carton_cannot_be_reassigned(self, db, people, actors):
        """Otherwise "assigned to" and "packed by" can be made to disagree.

        The service refuses this, but the service is not the boundary (§B3), and
        invoices stay open until their batch is released — so `is_open` alone
        does not cover it. An assignment that contradicts the pack record would
        look like evidence while being the opposite.
        """
        from tests.test_packing import _pack, _stock_and_pack_scan

        inv = await _invoice(db, units=2)
        inv["units"] = 2
        await act_as(db, people["matcher"]["id"])
        await _verify(db, inv["id"], people["matcher"]["id"])
        await _stock_and_pack_scan(db, inv, people)
        await _pack(db, inv["id"], people["packer"]["id"])

        async with rejected(db, containing="already been packed"):
            await _assign(db, inv["id"], people["packer_b"]["id"], people["matcher"]["id"])

    async def test_reassigning_supersedes_rather_than_edits(self, db, people):
        """PRD §7. "This box moved from Kavitha to Anitha at 14:20, by Lakshmi"
        has to remain answerable."""
        inv = await _invoice(db)
        await act_as(db, people["matcher"]["id"])
        await _verify(db, inv["id"], people["matcher"]["id"])

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
# 2. Product stickers issued vs product boxes packed
# ---------------------------------------------------------------------------


async def _received_units(db, gate_entry, actors, count=2):
    """Product stickers that have genuinely been counted in at offloading.

    Built the long way round — box stickers, box scans, unit stickers, unit
    scans — because the point of the reconciliation is that a product box cannot
    be packed unless it really arrived, and a shortcut that inserted stickers
    without the receiving scans would test nothing.
    """
    from tests.test_control_points import _issue_boxes, _scan, _sticker_code

    boxes = await _issue_boxes(db, gate_entry["id"], actors)

    await act_as(db, actors["guard"])
    for box_id in boxes:
        await _scan(db, await _sticker_code(db, box_id), "box_verify", actors["guard"])

    await db.execute(
        text("update gate_entries set status = 'box_verified' where id = :id"),
        {"id": gate_entry["id"]},
    )
    await db.execute(
        text("update gate_entries set status = 'offloading' where id = :id"),
        {"id": gate_entry["id"]},
    )

    await act_as(db, actors["ops"])
    box_id = boxes[0]
    line = (
        await db.execute(
            text(
                """
                select pol.id, pol.sku from purchase_order_lines pol
                  join purchase_orders po on po.id = pol.purchase_order_id
                 where po.po_number = 'PO-2026-0001'
                 order by pol.line_no limit 1
                """
            )
        )
    ).mappings().one()

    sheet_id = (
        await db.execute(
            text(
                """
                insert into sticker_sheets (gate_entry_id, sticker_type, quantity, generated_by)
                values (:e, 'unit', :n, :ops) returning id
                """
            ),
            {"e": gate_entry["id"], "n": count, "ops": actors["ops"]},
        )
    ).scalar_one()

    codes = []
    for seq in range(1, count + 1):
        code = f"UNT-{uuid.uuid4().hex[:8].upper()}"
        await db.execute(
            text(
                """
                insert into stickers
                  (code, sticker_type, sheet_id, gate_entry_id, box_id,
                   purchase_order_line_id, sequence_no, status)
                values (:c, 'unit', :sh, :e, :b, :pol, :seq, 'applied')
                """
            ),
            {
                "c": code,
                "sh": sheet_id,
                "e": gate_entry["id"],
                "b": box_id,
                "pol": line["id"],
                "seq": seq,
            },
        )
        codes.append(code)

    await act_as(db, actors["offloader"])
    for code in codes:
        result = await _scan(db, code, "unit_verify", actors["offloader"])
        assert result["accepted"], f"setup scan refused: {result['reject_reason']}"

    return {"codes": codes, "sku": line["sku"], "box_id": box_id}


async def _pack_scan(db, code, invoice_id, actor):
    return (
        await db.execute(
            text(
                """
                insert into scan_events
                  (client_event_id, scan_type, raw_code, invoice_id,
                   accepted, scanned_by, scanned_at)
                values (:cid, 'pack_unit', :code, :inv, false, :actor, now())
                returning accepted, reject_reason::text as reject_reason
                """
            ),
            {
                "cid": str(uuid.uuid4()),
                "code": code,
                "inv": invoice_id,
                "actor": actor,
            },
        )
    ).mappings().one()


class TestPackUnitReconciliation:
    async def test_a_carton_cannot_close_until_every_product_box_is_in_it(
        self, db, gate_entry, actors, people
    ):
        """The gap between CONTROL POINT 3 and CONTROL POINT 6: a product box
        counted into the warehouse that never appears in any carton."""
        stock = await _received_units(db, gate_entry, actors, count=2)

        await act_as(db, people["matcher"]["id"])
        inv = await _invoice(db, units=2)
        await db.execute(
            text("update invoices set sku = :s where id = :i"),
            {"s": stock["sku"], "i": inv["id"]},
        )
        await _verify(db, inv["id"], people["matcher"]["id"])

        await act_as(db, people["packer"]["id"])
        first = await _pack_scan(db, stock["codes"][0], inv["id"], people["packer"]["id"])
        assert first["accepted"], first["reject_reason"]

        # One of two scanned. The carton must not be closeable.
        async with rejected(db, containing="1 of 2 product boxes"):
            await db.execute(
                text("insert into packing_records (invoice_id, packed_by) values (:i, :p)"),
                {"i": inv["id"], "p": people["packer"]["id"]},
            )

        second = await _pack_scan(db, stock["codes"][1], inv["id"], people["packer"]["id"])
        assert second["accepted"], second["reject_reason"]

        await db.execute(
            text("insert into packing_records (invoice_id, packed_by) values (:i, :p)"),
            {"i": inv["id"], "p": people["packer"]["id"]},
        )

        row = (
            await db.execute(
                text(
                    "select packed_units, required_units, ready_to_close "
                    "from v_invoice_packing where invoice_id = :i"
                ),
                {"i": inv["id"]},
            )
        ).mappings().one()
        assert row["packed_units"] == 2
        assert row["ready_to_close"] is True

    async def test_the_same_product_box_cannot_be_packed_twice(
        self, db, gate_entry, actors, people
    ):
        """Holding the scanner still over one sticker must not fill a carton."""
        stock = await _received_units(db, gate_entry, actors, count=2)

        await act_as(db, people["matcher"]["id"])
        inv = await _invoice(db, units=2)
        await db.execute(
            text("update invoices set sku = :s where id = :i"),
            {"s": stock["sku"], "i": inv["id"]},
        )
        await _verify(db, inv["id"], people["matcher"]["id"])

        await act_as(db, people["packer"]["id"])
        assert (await _pack_scan(db, stock["codes"][0], inv["id"], people["packer"]["id"]))["accepted"]
        again = await _pack_scan(db, stock["codes"][0], inv["id"], people["packer"]["id"])

        assert again["accepted"] is False
        assert again["reject_reason"] == "already_scanned"

    async def test_over_packing_is_refused(self, db, gate_entry, actors, people):
        """An eleventh item in a ten-item carton would make it complete and
        wrong."""
        stock = await _received_units(db, gate_entry, actors, count=2)

        await act_as(db, people["matcher"]["id"])
        inv = await _invoice(db, units=1)
        await db.execute(
            text("update invoices set sku = :s where id = :i"),
            {"s": stock["sku"], "i": inv["id"]},
        )
        await _verify(db, inv["id"], people["matcher"]["id"])

        await act_as(db, people["packer"]["id"])
        assert (await _pack_scan(db, stock["codes"][0], inv["id"], people["packer"]["id"]))["accepted"]
        extra = await _pack_scan(db, stock["codes"][1], inv["id"], people["packer"]["id"])

        assert extra["accepted"] is False
        assert extra["reject_reason"] == "invoice_already_full"

    async def test_a_product_box_that_never_arrived_cannot_be_packed(
        self, db, gate_entry, actors, people
    ):
        """Otherwise packing is a second, unaudited route to creating stock —
        the hole DECISIONS.md §C4 closes for putaway."""
        stock = await _received_units(db, gate_entry, actors, count=2)

        await act_as(db, actors["ops"])
        ghost = f"UNT-{uuid.uuid4().hex[:8].upper()}"
        sheet_id = (
            await db.execute(
                text(
                    "select id from sticker_sheets "
                    "where gate_entry_id = :e and sticker_type = 'unit' limit 1"
                ),
                {"e": gate_entry["id"]},
            )
        ).scalar_one()
        await db.execute(
            text(
                """
                insert into stickers
                  (code, sticker_type, sheet_id, gate_entry_id, box_id, sequence_no, status)
                values (:c, 'unit', :sh, :e, :b, 99, 'issued')
                """
            ),
            {"c": ghost, "sh": sheet_id, "e": gate_entry["id"], "b": stock["box_id"]},
        )

        await act_as(db, people["matcher"]["id"])
        inv = await _invoice(db, units=1)
        await _verify(db, inv["id"], people["matcher"]["id"])

        await act_as(db, people["packer"]["id"])
        result = await _pack_scan(db, ghost, inv["id"], people["packer"]["id"])

        assert result["accepted"] is False
        assert result["reject_reason"] == "unit_not_in_stock"

    async def test_a_big_box_sticker_is_refused_at_the_bench(
        self, db, gate_entry, actors, people
    ):
        """The likeliest mistake at a packing bench: the big box the product came
        out of is sitting right there."""
        from tests.test_control_points import _sticker_code

        stock = await _received_units(db, gate_entry, actors, count=2)
        big_box_code = await _sticker_code(db, stock["box_id"])

        await act_as(db, people["matcher"]["id"])
        inv = await _invoice(db, units=1)
        await _verify(db, inv["id"], people["matcher"]["id"])

        await act_as(db, people["packer"]["id"])
        result = await _pack_scan(db, big_box_code, inv["id"], people["packer"]["id"])

        assert result["accepted"] is False
        assert result["reject_reason"] == "wrong_sticker_type"

    async def test_an_unverified_invoice_takes_no_product_boxes(
        self, db, gate_entry, actors, people
    ):
        stock = await _received_units(db, gate_entry, actors, count=2)

        await act_as(db, people["matcher"]["id"])
        inv = await _invoice(db, units=1)

        await act_as(db, people["packer"]["id"])
        result = await _pack_scan(db, stock["codes"][0], inv["id"], people["packer"]["id"])

        assert result["accepted"] is False
        assert result["reject_reason"] == "wrong_invoice"

    async def test_reconciliation_view_shows_the_gap(self, db, gate_entry, actors, people):
        """What an auditor asks: issued, received, packed — and where are the
        rest."""
        stock = await _received_units(db, gate_entry, actors, count=2)

        await act_as(db, people["matcher"]["id"])
        inv = await _invoice(db, units=1)
        await db.execute(
            text("update invoices set sku = :s where id = :i"),
            {"s": stock["sku"], "i": inv["id"]},
        )
        await _verify(db, inv["id"], people["matcher"]["id"])

        await act_as(db, people["packer"]["id"])
        await _pack_scan(db, stock["codes"][0], inv["id"], people["packer"]["id"])

        row = (
            await db.execute(
                text(
                    """
                    select unit_stickers_issued, received_at_offloading,
                           packed_into_cartons, received_not_packed
                      from v_sticker_reconciliation where gate_entry_id = :e
                    """
                ),
                {"e": gate_entry["id"]},
            )
        ).mappings().one()

        assert row["unit_stickers_issued"] == 2
        assert row["received_at_offloading"] == 2
        assert row["packed_into_cartons"] == 1
        assert row["received_not_packed"] == 1


# ---------------------------------------------------------------------------
# 3. The guard's carton count, and Ops's decision on it
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
# 4. Ops approves the vehicle leaving
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
