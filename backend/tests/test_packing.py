"""Phase 3 — CONTROL POINTS 5 and 6, at the database level.

The failures these prevent:
  * a carton packed against an invoice nobody checked against the goods
  * one person doing both the check and the pack, making the check a formality
  * a carton leaving without any record of who packed it
  * a batch released with a carton missing
"""

import uuid

import pytest
from sqlalchemy import text

from tests.conftest import rejected

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def people(db):
    """The seeded matchers and packers, with their badges."""
    rows = await db.execute(
        text(
            """
            select employee_code, id, badge_code, role::text as role
              from profiles
             where employee_code in ('EMP-M01', 'EMP-M02', 'EMP-P01', 'EMP-P02', 'EMP-O01',
                                   'EMP-G01', 'EMP-F01')
            """
        )
    )
    by_code = {r["employee_code"]: dict(r) for r in rows.mappings()}

    if "EMP-M01" not in by_code:
        pytest.skip("Seed data not loaded")

    return {
        "matcher_a": by_code["EMP-M01"],
        "matcher_b": by_code["EMP-M02"],
        "packer_a": by_code["EMP-P01"],
        "packer_b": by_code["EMP-P02"],
        "ops": by_code["EMP-O01"],
        # Needed since 0019: producing a packable carton means receiving the
        # goods first, which is a guard and an offloader's work.
        "guard": by_code["EMP-G01"],
        "offloader_id": by_code["EMP-F01"]["id"],
    }


async def _new_invoice(db, units=2):
    """Create a fresh invoice inside the test's transaction.

    Deliberately not reusing the seeded invoices: the end-to-end walkthrough
    consumes those, and a test suite that depends on their state fails for
    reasons that have nothing to do with the code under test. Everything here
    rolls back with the fixture.
    """
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

    number = f"INV-TEST-{uuid.uuid4().hex[:10].upper()}"
    invoice_id = (
        await db.execute(
            text(
                """
                insert into invoices
                  (invoice_number, purchase_order_line_id, sku, units, customer_name)
                values (:num, :line, :sku, :units, 'Test Customer')
                returning id
                """
            ),
            {"num": number, "line": line["id"], "sku": line["sku"], "units": units},
        )
    ).scalar_one()

    return {"id": invoice_id, "invoice_number": number}


@pytest.fixture
async def invoice(db):
    return await _new_invoice(db)


async def _verify(db, invoice_id, who):
    """Insert the verification row directly, same as the floor's badge scan.

    Since 0024_matching_unit_scan.sql this is refused unless every unit for the
    invoice has already been match-scanned (mirroring 0019's identical
    requirement for pack_unit) — so, exactly like `_stock_and_pack_scan` below
    has always had to produce real received goods before a carton can close,
    this now has to produce real received goods and match-scan them before an
    invoice can verify. Self-contained (looks up its own guard/offloader
    rather than taking them as arguments) so the ~20 existing call sites across
    the test suite did not all need to start threading `people`/`invoice`
    dicts through a helper that, from the caller's side, is still just "mark
    this invoice verified".
    """
    await _confirm_all_units(db, invoice_id)
    await db.execute(
        text(
            "insert into invoice_verifications (invoice_id, verified_by) values (:i, :w)"
        ),
        {"i": invoice_id, "w": who},
    )


async def _confirm_all_units(db, invoice_id):
    already = (
        await db.execute(
            text("select invoice_matched_units(:i) as n"), {"i": invoice_id}
        )
    ).scalar_one()

    row = (
        await db.execute(
            text("select units from invoices where id = :i"), {"i": invoice_id}
        )
    ).mappings().one()

    if already >= row["units"]:
        return

    actors = (
        await db.execute(
            text(
                "select employee_code, id from profiles "
                "where employee_code in ('EMP-G01', 'EMP-F01', 'EMP-O01')"
            )
        )
    ).mappings().all()
    by_code = {r["employee_code"]: r["id"] for r in actors}

    invoice = {"id": invoice_id, "units": row["units"]}
    people = {
        "guard": {"id": by_code["EMP-G01"]},
        "ops": {"id": by_code["EMP-O01"]},
        "offloader_id": by_code["EMP-F01"],
    }
    codes = await _receive_goods(db, invoice, people)
    await _match_scan(db, invoice_id, codes)


async def _pack(db, invoice_id, who):
    await db.execute(
        text("insert into packing_records (invoice_id, packed_by) values (:i, :w)"),
        {"i": invoice_id, "w": who},
    )


class TestControlPoint5:
    async def test_cannot_pack_an_unverified_invoice(self, db, invoice, people):
        """The check exists to catch the wrong product in the box. Skipping it
        would mean nobody ever compared goods to paperwork."""
        async with rejected(db, containing="CONTROL POINT 5"):
            await _pack(db, invoice["id"], people["packer_a"]["id"])

    async def test_matcher_and_packer_must_differ(self, db, invoice, people):
        """One person doing both halves makes the match a formality."""
        await _verify(db, invoice["id"], people["matcher_a"]["id"])

        async with rejected(db, containing="must be different people"):
            await _pack(db, invoice["id"], people["matcher_a"]["id"])

    async def test_verified_then_packed_by_someone_else_succeeds(self, db, invoice, people):
        await _verify(db, invoice["id"], people["matcher_a"]["id"])
        await _stock_and_pack_scan(db, invoice, people)
        await _pack(db, invoice["id"], people["packer_a"]["id"])

        row = (
            await db.execute(
                text(
                    """
                    select stage, verified_by_name, packed_by_name, packed_at
                      from v_invoice_status where invoice_id = :id
                    """
                ),
                {"id": invoice["id"]},
            )
        ).mappings().one()

        assert row["stage"] == "packed"
        assert row["verified_by_name"] == "Lakshmi Devi"
        assert row["packed_by_name"] == "Kavitha S"
        assert row["packed_at"] is not None, "attribution carries a timestamp"

    async def test_an_invoice_cannot_be_packed_twice(self, db, invoice, people):
        await _verify(db, invoice["id"], people["matcher_a"]["id"])
        await _stock_and_pack_scan(db, invoice, people)
        await _pack(db, invoice["id"], people["packer_a"]["id"])

        async with rejected(db):
            await _pack(db, invoice["id"], people["packer_b"]["id"])

    async def test_a_packer_badge_cannot_verify_an_invoice(self, db, invoice, people):
        """Badges are role-scoped, so a packer cannot stand in as the checker."""
        async with rejected(db, containing="not permitted"):
            await _verify(db, invoice["id"], people["packer_a"]["id"])

    async def test_a_guard_badge_cannot_pack(self, db, invoice, people):
        """Badges are role-scoped: only packers (and Admin, who also matches
        invoices and may cover the bench) may pack."""
        await _verify(db, invoice["id"], people["matcher_a"]["id"])

        async with rejected(db, containing="not permitted"):
            await _pack(db, invoice["id"], people["guard"]["id"])

    async def test_a_deactivated_badge_stops_working(self, db, invoice, people):
        """A lost badge is killed without disabling the person's account."""
        await db.execute(
            text("update profiles set badge_active = false where id = :id"),
            {"id": people["matcher_a"]["id"]},
        )

        async with rejected(db, containing="deactivated"):
            await _verify(db, invoice["id"], people["matcher_a"]["id"])

    async def test_cannot_pack_a_closed_invoice(self, db, invoice, people):
        await _verify(db, invoice["id"], people["matcher_a"]["id"])
        await db.execute(
            text("update invoices set is_open = false, closed_at = now() where id = :id"),
            {"id": invoice["id"]},
        )

        async with rejected(db, containing="closed"):
            await _pack(db, invoice["id"], people["packer_a"]["id"])


class TestMatchingUnitScan:
    """0024_matching_unit_scan.sql — the matching-stage hard stop.

    Additive to CONTROL POINT 5's badge check: a matcher's badge cannot be
    attached to an invoice (invoice_verifications) until every unit sticker
    for it has been match-scanned, mirroring the equivalent requirement 0019
    already puts on packing_records (CG3).

    These drive the trigger directly rather than through `_verify` (which
    produces and match-scans the goods for every other test in this file) so
    the partial and zero cases are actually reachable.
    """

    async def test_verify_is_refused_with_no_units_scanned(self, db, people):
        inv = await _new_invoice(db, units=2)

        async with rejected(db, containing="0 of 2 units scanned at matching"):
            await db.execute(
                text(
                    "insert into invoice_verifications (invoice_id, verified_by) "
                    "values (:i, :w)"
                ),
                {"i": inv["id"], "w": people["matcher_a"]["id"]},
            )

    async def test_verify_is_refused_with_units_still_remaining(self, db, people):
        inv = await _new_invoice(db, units=2)
        inv["units"] = 2
        codes = await _receive_goods(db, inv, people)
        await _match_scan(db, inv["id"], codes[:1])  # only one of two

        async with rejected(db, containing="1 of 2 units scanned at matching"):
            await db.execute(
                text(
                    "insert into invoice_verifications (invoice_id, verified_by) "
                    "values (:i, :w)"
                ),
                {"i": inv["id"], "w": people["matcher_a"]["id"]},
            )

    async def test_verify_succeeds_once_every_unit_is_matched(self, db, people):
        inv = await _new_invoice(db, units=2)
        inv["units"] = 2
        codes = await _receive_goods(db, inv, people)
        await _match_scan(db, inv["id"], codes)

        await db.execute(
            text(
                "insert into invoice_verifications (invoice_id, verified_by) "
                "values (:i, :w)"
            ),
            {"i": inv["id"], "w": people["matcher_a"]["id"]},
        )

        row = (
            await db.execute(
                text("select matched_units, ready_to_verify from v_invoice_matching where invoice_id = :i"),
                {"i": inv["id"]},
            )
        ).mappings().one()
        assert row["matched_units"] == 2
        assert row["ready_to_verify"] is True

    async def test_a_unit_cannot_be_matched_twice(self, db, people):
        """Scanned once accepted, scanned again (same code) rejected.

        Checked by aggregate rather than "the latest row": `now()` is the
        transaction's start time in Postgres, so two scans in one test
        transaction share one `scanned_at` and cannot be told apart by
        ordering on it.
        """
        inv = await _new_invoice(db, units=2)
        inv["units"] = 2
        codes = await _receive_goods(db, inv, people)
        await _match_scan(db, inv["id"], codes[:1])
        await _match_scan(db, inv["id"], codes[:1])

        counts = (
            await db.execute(
                text(
                    "select count(*)::int as total, "
                    "count(*) filter (where accepted)::int as accepted, "
                    "count(*) filter (where reject_reason = 'already_scanned')::int as rejected "
                    "from scan_events where raw_code = :c and scan_type = 'match_unit'"
                ),
                {"c": codes[0]},
            )
        ).mappings().one()
        assert counts["total"] == 2
        assert counts["accepted"] == 1
        assert counts["rejected"] == 1

    async def test_a_unit_not_yet_received_cannot_be_matched(self, db, people):
        """A matcher cannot confirm a product that never arrived — the same
        `unit_not_in_stock` requirement pack_unit makes later.

        A unit sticker applied but never scanned at offloading (unit_verify):
        stickers_family_shape requires a gate entry and sheet, so one exists,
        but nothing in this test ever calls _raw_scan(..., 'unit_verify', ...)
        against it.
        """
        inv = await _new_invoice(db, units=1)

        line_id = (
            await db.execute(
                text("select purchase_order_line_id from invoices where id = :i"),
                {"i": inv["id"]},
            )
        ).scalar_one()

        po = (
            await db.execute(
                text(
                    """
                    select po.id, po.vendor_id from purchase_orders po
                      join purchase_order_lines pol on pol.purchase_order_id = po.id
                     where pol.id = :l
                    """
                ),
                {"l": line_id},
            )
        ).mappings().one()

        entry_id = (
            await db.execute(
                text(
                    """
                    insert into gate_entries
                      (status, vehicle_number, vendor_id, purchase_order_id,
                       requested_by, requested_at)
                    values ('pending_approval', :veh, :vendor, :po, :guard, now())
                    returning id
                    """
                ),
                {
                    "veh": f"KA01UN{uuid.uuid4().hex[:4].upper()}",
                    "vendor": po["vendor_id"],
                    "po": po["id"],
                    "guard": people["guard"]["id"],
                },
            )
        ).scalar_one()

        sheet_id = (
            await db.execute(
                text(
                    "insert into sticker_sheets (gate_entry_id, sticker_type, quantity, generated_by) "
                    "values (:e, 'unit', 1, :o) returning id"
                ),
                {"e": entry_id, "o": people["ops"]["id"]},
            )
        ).scalar_one()
        code = f"UNT-{uuid.uuid4().hex[:8].upper()}"
        await db.execute(
            text(
                """
                insert into stickers
                  (code, sticker_type, sheet_id, gate_entry_id, purchase_order_line_id,
                   sequence_no, status)
                values (:c, 'unit', :sh, :e, :l, 1, 'applied')
                """
            ),
            {"c": code, "sh": sheet_id, "e": entry_id, "l": line_id},
        )

        await _match_scan(db, inv["id"], [code])
        row = (
            await db.execute(
                text(
                    "select accepted, reject_reason::text as reject_reason "
                    "from scan_events where raw_code = :c order by scanned_at desc limit 1"
                ),
                {"c": code},
            )
        ).mappings().one()
        assert row["accepted"] is False
        assert row["reject_reason"] == "unit_not_in_stock"

    async def test_matching_and_packing_scans_do_not_collide(self, db, people):
        """The same unit sticker is scanned once at matching and, independently,
        once again at packing — scan_events_one_accept_per_sticker is keyed
        (sticker_id, scan_type), so this is not a double-scan."""
        inv = await _new_invoice(db, units=1)
        inv["units"] = 1
        codes = await _receive_goods(db, inv, people)
        await _match_scan(db, inv["id"], codes)
        # _verify's internal goods production is a no-op here: every unit for
        # this invoice is already match-scanned, so _confirm_all_units sees
        # `already >= units` and produces nothing further.
        await _verify(db, inv["id"], people["matcher_a"]["id"])

        for code in codes:
            await db.execute(
                text(
                    """
                    insert into scan_events
                      (client_event_id, scan_type, raw_code, invoice_id,
                       accepted, scanned_by, scanned_at)
                    values (:cid, 'pack_unit', :code, :inv, false, :who, now())
                    """
                ),
                {
                    "cid": str(uuid.uuid4()),
                    "code": code,
                    "inv": inv["id"],
                    "who": people["packer_a"]["id"],
                },
            )

        row = (
            await db.execute(
                text(
                    "select accepted from scan_events "
                    "where raw_code = :c and scan_type = 'pack_unit' "
                    "order by scanned_at desc limit 1"
                ),
                {"c": codes[0]},
            )
        ).mappings().one()
        assert row["accepted"] is True


async def _stock_and_pack_scan(db, invoice, people):
    """Give an invoice real product boxes, received and scanned into the carton.

    Migration 0019 makes packing a scanning step: a carton cannot close until the
    number of product boxes scanned into it equals the number the invoice
    promises. So a test that wants a packed carton has to produce the goods,
    exactly as the floor does — receive them at offloading first, then scan them
    into the carton.

    Written out in full rather than shortcut because the shortcut is the thing
    under test: if product boxes could be packed without having arrived, packing
    would be a second, unaudited route to creating inventory.
    """
    codes = await _receive_goods(db, invoice, people)

    for code in codes:
        await db.execute(
            text(
                """
                insert into scan_events
                  (client_event_id, scan_type, raw_code, invoice_id,
                   accepted, scanned_by, scanned_at)
                values (:cid, 'pack_unit', :code, :inv, false, :who, now())
                """
            ),
            {
                "cid": str(uuid.uuid4()),
                "code": code,
                "inv": invoice["id"],
                "who": people["packer_a"]["id"],
            },
        )

    return codes


async def _match_scan(db, invoice_id, codes):
    """Confirm every unit sticker code at matching, before the badge scan.

    Mirrors the pack_unit inserts in `_stock_and_pack_scan` — same shape, a
    different scan_type and hard stop (0024_matching_unit_scan.sql).
    """
    matcher = (
        await db.execute(
            text("select id from profiles where employee_code = 'EMP-M01'")
        )
    ).scalar_one()

    for code in codes:
        await db.execute(
            text(
                """
                insert into scan_events
                  (client_event_id, scan_type, raw_code, invoice_id,
                   accepted, scanned_by, scanned_at)
                values (:cid, 'match_unit', :code, :inv, false, :who, now())
                """
            ),
            {
                "cid": str(uuid.uuid4()),
                "code": code,
                "inv": invoice_id,
                "who": matcher,
            },
        )


async def _receive_goods(db, invoice, people):
    """Receive real product boxes at the gate — box count, box sticker, unit
    stickers — exactly as far as CONTROL POINT 3, without packing or matching
    them into anything. Returns the unit sticker codes.
    """
    line_id = (
        await db.execute(
            text("select purchase_order_line_id from invoices where id = :i"),
            {"i": invoice["id"]},
        )
    ).scalar_one()

    po = (
        await db.execute(
            text(
                """
                select po.id, po.vendor_id from purchase_orders po
                  join purchase_order_lines pol on pol.purchase_order_id = po.id
                 where pol.id = :l
                """
            ),
            {"l": line_id},
        )
    ).mappings().one()

    units = invoice.get("units", 2)

    entry_id = (
        await db.execute(
            text(
                """
                insert into gate_entries
                  (status, vehicle_number, vendor_id, purchase_order_id,
                   requested_by, requested_at)
                values ('pending_approval', :veh, :vendor, :po, :guard, now())
                returning id
                """
            ),
            {
                "veh": f"KA01PK{uuid.uuid4().hex[:4].upper()}",
                "vendor": po["vendor_id"],
                "po": po["id"],
                "guard": people["guard"]["id"],
            },
        )
    ).scalar_one()

    for sql_stmt, params in [
        ("update gate_entries set status = 'approved', decided_by = :o, decided_at = now() "
         "where id = :e", {"o": people["ops"]["id"], "e": entry_id}),
        ("update gate_entries set status = 'inside' where id = :e", {"e": entry_id}),
        ("update gate_entries set declared_box_count = 1, declared_by = :g, "
         "declared_at = now(), status = 'counting' where id = :e",
         {"g": people["guard"]["id"], "e": entry_id}),
    ]:
        await db.execute(text(sql_stmt), params)

    box_sheet = (
        await db.execute(
            text(
                "insert into sticker_sheets (gate_entry_id, sticker_type, quantity, generated_by) "
                "values (:e, 'box', 1, :o) returning id"
            ),
            {"e": entry_id, "o": people["ops"]["id"]},
        )
    ).scalar_one()

    box_code = f"BOX-{uuid.uuid4().hex[:8].upper()}"
    sticker_id = (
        await db.execute(
            text(
                """
                insert into stickers
                  (code, sticker_type, sheet_id, gate_entry_id, purchase_order_line_id,
                   expected_units, sequence_no, status)
                values (:c, 'box', :sh, :e, :l, :u, 1, 'applied') returning id
                """
            ),
            {"c": box_code, "sh": box_sheet, "e": entry_id, "l": line_id, "u": units},
        )
    ).scalar_one()

    box_id = (
        await db.execute(
            text(
                """
                insert into boxes
                  (gate_entry_id, sticker_id, box_number, purchase_order_line_id, expected_units)
                values (:e, :s, 1, :l, :u) returning id
                """
            ),
            {"e": entry_id, "s": sticker_id, "l": line_id, "u": units},
        )
    ).scalar_one()

    await db.execute(
        text("update stickers set box_id = :b where id = :s"), {"b": box_id, "s": sticker_id}
    )
    await db.execute(
        text("update gate_entries set issued_box_sticker_count = 1 where id = :e"),
        {"e": entry_id},
    )
    await _raw_scan(db, box_code, "box_verify", people["guard"]["id"])

    await db.execute(
        text("update gate_entries set status = 'box_verified' where id = :e"), {"e": entry_id}
    )
    await db.execute(
        text("update gate_entries set status = 'offloading' where id = :e"), {"e": entry_id}
    )

    unit_sheet = (
        await db.execute(
            text(
                "insert into sticker_sheets (gate_entry_id, sticker_type, quantity, generated_by) "
                "values (:e, 'unit', :n, :o) returning id"
            ),
            {"e": entry_id, "n": units, "o": people["ops"]["id"]},
        )
    ).scalar_one()

    codes = []
    for seq in range(1, units + 1):
        code = f"UNT-{uuid.uuid4().hex[:8].upper()}"
        await db.execute(
            text(
                """
                insert into stickers
                  (code, sticker_type, sheet_id, gate_entry_id, box_id,
                   purchase_order_line_id, sequence_no, status)
                values (:c, 'unit', :sh, :e, :b, :l, :seq, 'applied')
                """
            ),
            {"c": code, "sh": unit_sheet, "e": entry_id, "b": box_id, "l": line_id, "seq": seq},
        )
        await _raw_scan(db, code, "unit_verify", people["offloader_id"])
        codes.append(code)

    return codes


async def _raw_scan(db, code, scan_type, actor):
    await db.execute(
        text(
            """
            insert into scan_events
              (client_event_id, scan_type, raw_code, accepted, scanned_by, scanned_at)
            values (:cid, cast(:st as scan_type), :code, false, :actor, now())
            """
        ),
        {"cid": str(uuid.uuid4()), "st": scan_type, "code": code, "actor": actor},
    )


async def _packed_invoices(db, people, count=3):
    """Fresh invoices, verified and packed, ready to be batched.

    Since 0019 this has to produce the goods too — a carton will not close until
    every product box it promises has been scanned into it.
    """
    invoices = []
    for _ in range(count):
        inv = await _new_invoice(db)
        inv["units"] = 2
        await _verify(db, inv["id"], people["matcher_a"]["id"])
        await _stock_and_pack_scan(db, inv, people)
        await _pack(db, inv["id"], people["packer_a"]["id"])
        invoices.append(inv)
    return invoices


async def _batch_of(db, invoices, people):
    batch_id = (
        await db.execute(
            text(
                """
                insert into batches (planned_carton_count, created_by, status)
                values (:n, :who, 'open') returning id
                """
            ),
            {"n": len(invoices), "who": people["ops"]["id"]},
        )
    ).scalar_one()

    await db.execute(
        text(
            "update packing_records set batch_id = :b where invoice_id = any(cast(:ids as uuid[]))"
        ),
        {"b": batch_id, "ids": [str(i["id"]) for i in invoices]},
    )
    return batch_id


async def _approve_load(db, batch_id, people):
    """The guard counts the cartons and Ops approves the count.

    Added by migration 0018: a batch cannot reach 'released' without this, for
    the same reason a truck cannot enter the gate without CONTROL POINT 1. The
    two people have to differ, so the guard is looked up rather than reusing an
    Ops actor.
    """
    guard = (
        await db.execute(
            text("select id from profiles where employee_code = 'EMP-G01'")
        )
    ).scalar_one()

    actual = (
        await db.execute(
            text("select count(*)::int from packing_records where batch_id = :b"),
            {"b": batch_id},
        )
    ).scalar_one()

    approval_id = (
        await db.execute(
            text(
                """
                insert into batch_load_approvals
                  (batch_id, counted_cartons, counted_by, expected_cartons)
                values (:b, :n, :g, :n) returning id
                """
            ),
            {"b": batch_id, "n": actual, "g": guard},
        )
    ).scalar_one()

    await db.execute(
        text(
            """
            update batch_load_approvals
               set status = 'approved', decided_by = :ops
             where id = :id
            """
        ),
        {"ops": people["ops"]["id"], "id": approval_id},
    )
    return approval_id


async def _out_scan(db, code, who):
    return (
        await db.execute(
            text(
                """
                insert into scan_events
                  (client_event_id, scan_type, raw_code, accepted, scanned_by, scanned_at)
                values (:cid, 'out_scan', :code, false, :who, now())
                returning accepted, reject_reason::text as reject_reason, invoice_id
                """
            ),
            {"cid": str(uuid.uuid4()), "code": code, "who": who},
        )
    ).mappings().one()


class TestControlPoint6:
    async def test_unpacked_carton_cannot_be_out_scanned(self, db, invoice, people):
        """Otherwise goods reach the pickup area with no record of who packed them."""
        result = await _out_scan(db, invoice["invoice_number"], people["ops"]["id"])
        assert result["accepted"] is False
        assert result["reject_reason"] == "not_packed"

    async def test_packed_but_unbatched_carton_is_refused(self, db, people):
        invoices = await _packed_invoices(db, people, 1)
        result = await _out_scan(db, invoices[0]["invoice_number"], people["ops"]["id"])
        assert result["accepted"] is False
        assert result["reject_reason"] == "not_in_batch"

    async def test_out_scan_advances_the_batch(self, db, people):
        invoices = await _packed_invoices(db, people, 3)
        batch_id = await _batch_of(db, invoices, people)

        result = await _out_scan(db, invoices[0]["invoice_number"], people["ops"]["id"])
        assert result["accepted"] is True

        row = (
            await db.execute(
                text(
                    """
                    select status, assigned_cartons, scanned_cartons, remaining_cartons
                      from v_batch_status where batch_id = :id
                    """
                ),
                {"id": batch_id},
            )
        ).mappings().one()

        assert row["status"] == "scanning", "the first carton moves the batch off 'open'"
        assert (row["assigned_cartons"], row["scanned_cartons"]) == (3, 1)

    async def test_same_carton_cannot_be_counted_twice(self, db, people):
        invoices = await _packed_invoices(db, people, 2)
        await _batch_of(db, invoices, people)

        first = await _out_scan(db, invoices[0]["invoice_number"], people["ops"]["id"])
        second = await _out_scan(db, invoices[0]["invoice_number"], people["ops"]["id"])

        assert first["accepted"] is True
        assert second["accepted"] is False
        assert second["reject_reason"] == "already_scanned"

    async def test_batch_cannot_complete_with_a_carton_missing(self, db, people):
        """The failure this prevents: a truck leaving one carton short."""
        invoices = await _packed_invoices(db, people, 3)
        batch_id = await _batch_of(db, invoices, people)

        for inv in invoices[:2]:
            await _out_scan(db, inv["invoice_number"], people["ops"]["id"])

        async with rejected(db, containing="CONTROL POINT 6"):
            await db.execute(
                text("update batches set status = 'complete' where id = :id"),
                {"id": batch_id},
            )

    async def test_batch_completes_when_all_cartons_scanned(self, db, people):
        invoices = await _packed_invoices(db, people, 3)
        batch_id = await _batch_of(db, invoices, people)

        for inv in invoices:
            await _out_scan(db, inv["invoice_number"], people["ops"]["id"])

        await db.execute(
            text("update batches set status = 'complete' where id = :id"), {"id": batch_id}
        )

        status = (
            await db.execute(
                text("select status::text from batches where id = :id"), {"id": batch_id}
            )
        ).scalar_one()
        assert status == "complete"

    async def test_batch_cannot_be_released_before_completion(self, db, people):
        invoices = await _packed_invoices(db, people, 2)
        batch_id = await _batch_of(db, invoices, people)
        await _out_scan(db, invoices[0]["invoice_number"], people["ops"]["id"])

        async with rejected(db, containing="CONTROL POINT 6"):
            await db.execute(
                text(
                    """
                    update batches set status = 'released', released_by = :who
                     where id = :id
                    """
                ),
                {"who": people["ops"]["id"], "id": batch_id},
            )

    async def test_release_requires_a_named_user(self, db, people):
        invoices = await _packed_invoices(db, people, 1)
        batch_id = await _batch_of(db, invoices, people)
        await _out_scan(db, invoices[0]["invoice_number"], people["ops"]["id"])
        await db.execute(
            text("update batches set status = 'complete' where id = :id"), {"id": batch_id}
        )
        await _approve_load(db, batch_id, people)

        async with rejected(db, containing="named releasing user"):
            await db.execute(
                text("update batches set status = 'released' where id = :id"),
                {"id": batch_id},
            )

    async def test_cartons_cannot_be_dropped_once_scanning_starts(self, db, people):
        """Otherwise 'all cartons scanned' could be satisfied by quietly removing
        the carton that is missing."""
        invoices = await _packed_invoices(db, people, 3)
        batch_id = await _batch_of(db, invoices, people)
        await _out_scan(db, invoices[0]["invoice_number"], people["ops"]["id"])

        async with rejected(db, containing="cannot be moved out of batch"):
            await db.execute(
                text("update packing_records set batch_id = null where invoice_id = :i"),
                {"i": invoices[2]["id"]},
            )

    async def test_nothing_can_be_added_to_a_closed_batch(self, db, people):
        invoices = await _packed_invoices(db, people, 2)
        batch_id = await _batch_of(db, invoices, people)
        for inv in invoices:
            await _out_scan(db, inv["invoice_number"], people["ops"]["id"])
        await db.execute(
            text("update batches set status = 'complete' where id = :id"), {"id": batch_id}
        )

        extra = await _packed_invoices(db, people, 1)
        async with rejected(db, containing="no longer open"):
            await db.execute(
                text("update packing_records set batch_id = :b where invoice_id = :i"),
                {"b": batch_id, "i": extra[0]["id"]},
            )

    async def test_scanning_into_a_closed_batch_is_refused(self, db, people):
        invoices = await _packed_invoices(db, people, 2)
        batch_id = await _batch_of(db, invoices, people)
        await _out_scan(db, invoices[0]["invoice_number"], people["ops"]["id"])
        await db.execute(
            text("update batches set status = 'cancelled' where id = :id"), {"id": batch_id}
        )

        result = await _out_scan(db, invoices[1]["invoice_number"], people["ops"]["id"])
        assert result["accepted"] is False
        assert result["reject_reason"] == "batch_closed"
