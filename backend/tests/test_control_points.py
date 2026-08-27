"""The hard stops in PRD §4, tested against the database that enforces them.

Each test asserts that the *database* refuses, not that a service function
returns an error. That distinction is the whole point: the PRD's first success
metric is zero manual overrides, and a rule enforced only in Python is one
hotfix away from not being a rule.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.conftest import act_as, rejected

pytestmark = pytest.mark.asyncio


async def _sticker_code(db, box_id):
    return (
        await db.execute(
            text("select s.code from stickers s join boxes b on b.sticker_id = s.id where b.id = :id"),
            {"id": box_id},
        )
    ).scalar_one()


async def _issue_boxes(db, entry_id, actors, count=6):
    """Mimic what the sticker service does, at SQL level."""
    await act_as(db, actors["guard"])
    await db.execute(
        text(
            """
            update gate_entries
               set declared_box_count = :n, declared_by = :guard,
                   declared_at = now(), status = 'counting'
             where id = :id
            """
        ),
        {"n": count, "guard": actors["guard"], "id": entry_id},
    )

    await act_as(db, actors["ops"])
    sheet_id = (
        await db.execute(
            text(
                """
                insert into sticker_sheets (gate_entry_id, sticker_type, quantity, generated_by)
                values (:id, 'box', :n, :ops) returning id
                """
            ),
            {"id": entry_id, "n": count, "ops": actors["ops"]},
        )
    ).scalar_one()

    lines = list(
        (
            await db.execute(
                text(
                    """
                    select pol.id, pol.units_per_box, pol.expected_units
                      from purchase_order_lines pol
                      join gate_entries ge on ge.purchase_order_id = pol.purchase_order_id
                     where ge.id = :id order by pol.line_no
                    """
                ),
                {"id": entry_id},
            )
        ).mappings()
    )

    allocation = []
    for line in lines:
        remaining = line["expected_units"]
        while remaining > 0:
            units = min(remaining, line["units_per_box"])
            allocation.append((line["id"], units))
            remaining -= units

    box_ids = []
    for seq, (line_id, units) in enumerate(allocation[:count], start=1):
        sticker_id = (
            await db.execute(
                text(
                    """
                    insert into stickers
                      (code, sticker_type, sheet_id, gate_entry_id,
                       purchase_order_line_id, expected_units, sequence_no, status)
                    values (:code, 'box', :sheet, :entry, :line, :units, :seq, 'applied')
                    returning id
                    """
                ),
                {
                    "code": f"BOX-{uuid.uuid4().hex[:8].upper()}",
                    "sheet": sheet_id,
                    "entry": entry_id,
                    "line": line_id,
                    "units": units,
                    "seq": seq,
                },
            )
        ).scalar_one()

        box_id = (
            await db.execute(
                text(
                    """
                    insert into boxes
                      (gate_entry_id, sticker_id, box_number, purchase_order_line_id, expected_units)
                    values (:entry, :sticker, :seq, :line, :units) returning id
                    """
                ),
                {
                    "entry": entry_id,
                    "sticker": sticker_id,
                    "seq": seq,
                    "line": line_id,
                    "units": units,
                },
            )
        ).scalar_one()

        await db.execute(
            text("update stickers set box_id = :b where id = :s"),
            {"b": box_id, "s": sticker_id},
        )
        box_ids.append(box_id)

    await db.execute(
        text("update gate_entries set issued_box_sticker_count = :n where id = :id"),
        {"n": count, "id": entry_id},
    )
    return box_ids


async def _scan(db, code, scan_type, actor):
    return (
        await db.execute(
            text(
                """
                insert into scan_events
                  (client_event_id, scan_type, raw_code, accepted, scanned_by, scanned_at)
                values (:cid, cast(:st as scan_type), :code, false, :actor, now())
                returning accepted, reject_reason::text as reject_reason, box_id
                """
            ),
            {"cid": str(uuid.uuid4()), "st": scan_type, "code": code, "actor": actor},
        )
    ).mappings().one()


# ===========================================================================
# CONTROL POINT 1 — gate entry approval
# ===========================================================================


class TestControlPoint1:
    async def test_vehicle_cannot_enter_without_approval(self, db, actors):
        """The core of CP1: no approval, no gate."""
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
                    values ('pending_approval', 'KA01ZZ9999', :v, :po, :g, now())
                    returning id
                    """
                ),
                {"v": po["vendor_id"], "po": po["id"], "g": actors["guard"]},
            )
        ).scalar_one()

        async with rejected(db, containing="CONTROL POINT 1"):
            await db.execute(
                text("update gate_entries set status = 'inside' where id = :id"),
                {"id": entry_id},
            )

        # The refusal must also leave the entry exactly where it was.
        status = (
            await db.execute(
                text("select status::text from gate_entries where id = :id"), {"id": entry_id}
            )
        ).scalar_one()
        assert status == "pending_approval"

    async def test_guard_cannot_approve_own_request(self, db, actors):
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
                    values ('pending_approval', 'KA01ZZ8888', :v, :po, :g, now())
                    returning id
                    """
                ),
                {"v": po["vendor_id"], "po": po["id"], "g": actors["guard"]},
            )
        ).scalar_one()

        with pytest.raises((IntegrityError, DBAPIError)):
            await db.execute(
                text(
                    """
                    update gate_entries
                       set status = 'approved', decided_by = :g, decided_at = now()
                     where id = :id
                    """
                ),
                {"g": actors["guard"], "id": entry_id},
            )

    async def test_non_ops_role_cannot_approve(self, db, actors):
        """Even a different person is not enough — it must be Admin."""
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
                    values ('pending_approval', 'KA01ZZ7777', :v, :po, :g, now())
                    returning id
                    """
                ),
                {"v": po["vendor_id"], "po": po["id"], "g": actors["guard"]},
            )
        ).scalar_one()

        with pytest.raises(DBAPIError) as err:
            await db.execute(
                text(
                    """
                    update gate_entries
                       set status = 'approved', decided_by = :o, decided_at = now()
                     where id = :id
                    """
                ),
                {"o": actors["offloader"], "id": entry_id},
            )

        assert "Only an Admin" in str(err.value)

    async def test_approved_entry_admits_and_stamps_time_in(self, db, gate_entry):
        row = (
            await db.execute(
                text("select status::text as status, time_in from gate_entries where id = :id"),
                {"id": gate_entry["id"]},
            )
        ).mappings().one()

        assert row["status"] == "inside"
        assert row["time_in"] is not None


# ===========================================================================
# CONTROL POINT 2 — box count
# ===========================================================================


class TestControlPoint2:
    async def test_partial_box_scan_blocks_verification(self, db, gate_entry, actors):
        boxes = await _issue_boxes(db, gate_entry["id"], actors)

        await act_as(db, actors["guard"])
        for box_id in boxes[:-1]:            # one box left unscanned
            code = await _sticker_code(db, box_id)
            await _scan(db, code, "box_verify", actors["guard"])

        with pytest.raises(DBAPIError) as err:
            await db.execute(
                text("update gate_entries set status = 'box_verified' where id = :id"),
                {"id": gate_entry["id"]},
            )

        assert "CONTROL POINT 2" in str(err.value)

    async def test_full_box_scan_passes(self, db, gate_entry, actors):
        boxes = await _issue_boxes(db, gate_entry["id"], actors)

        await act_as(db, actors["guard"])
        for box_id in boxes:
            code = await _sticker_code(db, box_id)
            result = await _scan(db, code, "box_verify", actors["guard"])
            assert result["accepted"] is True

        await db.execute(
            text("update gate_entries set status = 'box_verified' where id = :id"),
            {"id": gate_entry["id"]},
        )

        status = (
            await db.execute(
                text("select status::text from gate_entries where id = :id"),
                {"id": gate_entry["id"]},
            )
        ).scalar_one()
        assert status == "box_verified"

    async def test_same_box_sticker_cannot_be_counted_twice(self, db, gate_entry, actors):
        """Double-scanning is how a short truck passes a box count."""
        boxes = await _issue_boxes(db, gate_entry["id"], actors)
        await act_as(db, actors["guard"])

        code = await _sticker_code(db, boxes[0])
        first = await _scan(db, code, "box_verify", actors["guard"])
        second = await _scan(db, code, "box_verify", actors["guard"])

        assert first["accepted"] is True
        assert second["accepted"] is False
        assert second["reject_reason"] == "already_scanned"

    async def test_unknown_sticker_is_rejected_but_recorded(self, db, gate_entry, actors):
        await _issue_boxes(db, gate_entry["id"], actors)
        await act_as(db, actors["guard"])

        # Unique per run: the database is shared with other runs and other
        # test data, so a fixed literal would count scans this test never made.
        bogus = f"BOX-{uuid.uuid4().hex[:8].upper()}"

        result = await _scan(db, bogus, "box_verify", actors["guard"])
        assert result["accepted"] is False
        assert result["reject_reason"] == "unknown_code"

        recorded = (
            await db.execute(
                text("select count(*) from scan_events where raw_code = :code"),
                {"code": bogus},
            )
        ).scalar_one()
        assert recorded == 1, "rejected scans must still be logged"


# ===========================================================================
# CONTROL POINT 3 — unit count
# ===========================================================================


class TestControlPoint3:
    async def _prepare_open_box(self, db, gate_entry, actors):
        boxes = await _issue_boxes(db, gate_entry["id"], actors)

        await act_as(db, actors["guard"])
        for box_id in boxes:
            code = await _sticker_code(db, box_id)
            await _scan(db, code, "box_verify", actors["guard"])

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
        expected = (
            await db.execute(
                text("select expected_units from boxes where id = :id"), {"id": box_id}
            )
        ).scalar_one()

        sheet_id = (
            await db.execute(
                text(
                    """
                    insert into sticker_sheets (gate_entry_id, sticker_type, quantity, generated_by)
                    values (:e, 'unit', :n, :ops) returning id
                    """
                ),
                {"e": gate_entry["id"], "n": expected, "ops": actors["ops"]},
            )
        ).scalar_one()

        codes = []
        for seq in range(1, expected + 1):
            code = f"UNT-{uuid.uuid4().hex[:8].upper()}"
            await db.execute(
                text(
                    """
                    insert into stickers
                      (code, sticker_type, sheet_id, gate_entry_id, box_id, sequence_no, status)
                    values (:c, 'unit', :sh, :e, :b, :seq, 'applied')
                    """
                ),
                {"c": code, "sh": sheet_id, "e": gate_entry["id"], "b": box_id, "seq": seq},
            )
            codes.append(code)

        return box_id, expected, codes

    async def test_short_count_cannot_close_the_box(self, db, gate_entry, actors):
        """8 of 10 units: the box must not close. This is the PRD's own example."""
        box_id, expected, codes = await self._prepare_open_box(db, gate_entry, actors)

        await act_as(db, actors["offloader"])
        for code in codes[:-2]:
            await _scan(db, code, "unit_verify", actors["offloader"])

        await db.execute(
            text("update boxes set damage_level = 'none' where id = :id"), {"id": box_id}
        )

        async with rejected(db, containing="CONTROL POINT 3"):
            await db.execute(
                text("update boxes set status = 'complete' where id = :id"), {"id": box_id}
            )

        scanned = (
            await db.execute(
                text("select scanned_units from boxes where id = :id"), {"id": box_id}
            )
        ).scalar_one()
        assert scanned == expected - 2

    async def test_exact_count_closes_the_box(self, db, gate_entry, actors):
        box_id, expected, codes = await self._prepare_open_box(db, gate_entry, actors)

        await act_as(db, actors["offloader"])
        for code in codes:
            await _scan(db, code, "unit_verify", actors["offloader"])

        await db.execute(
            text("update boxes set damage_level = 'none' where id = :id"), {"id": box_id}
        )
        await db.execute(
            text("update boxes set status = 'complete' where id = :id"), {"id": box_id}
        )

        row = (
            await db.execute(
                text(
                    "select status::text as status, scanned_units, completed_at "
                    "from boxes where id = :id"
                ),
                {"id": box_id},
            )
        ).mappings().one()

        assert row["status"] == "complete"
        assert row["scanned_units"] == expected
        assert row["completed_at"] is not None

    async def test_over_scan_is_refused(self, db, gate_entry, actors):
        """An 11th unit in a 10-unit box would make a wrong box look complete."""
        box_id, expected, codes = await self._prepare_open_box(db, gate_entry, actors)

        await act_as(db, actors["ops"])
        extra_code = f"UNT-{uuid.uuid4().hex[:8].upper()}"
        sheet_id = (
            await db.execute(
                text("select id from sticker_sheets where gate_entry_id = :e and sticker_type = 'unit'"),
                {"e": gate_entry["id"]},
            )
        ).scalar()
        await db.execute(
            text(
                """
                insert into stickers
                  (code, sticker_type, sheet_id, gate_entry_id, box_id, sequence_no, status)
                values (:c, 'unit', :sh, :e, :b, 999, 'applied')
                """
            ),
            {"c": extra_code, "sh": sheet_id, "e": gate_entry["id"], "b": box_id},
        )

        await act_as(db, actors["offloader"])
        for code in codes:
            await _scan(db, code, "unit_verify", actors["offloader"])

        result = await _scan(db, extra_code, "unit_verify", actors["offloader"])
        assert result["accepted"] is False
        assert result["reject_reason"] == "over_expected_quantity"

    async def test_box_cannot_close_without_damage_check(self, db, gate_entry, actors):
        box_id, expected, codes = await self._prepare_open_box(db, gate_entry, actors)

        await act_as(db, actors["offloader"])
        for code in codes:
            await _scan(db, code, "unit_verify", actors["offloader"])

        with pytest.raises(DBAPIError) as err:
            await db.execute(
                text("update boxes set status = 'complete' where id = :id"), {"id": box_id}
            )
        assert "damage check" in str(err.value).lower()

    async def test_box_cannot_be_forced_to_short_accepted(self, db, gate_entry, actors):
        """The only route to short_accepted is a resolved exception."""
        box_id, expected, codes = await self._prepare_open_box(db, gate_entry, actors)

        await act_as(db, actors["offloader"])
        await db.execute(
            text("update boxes set status = 'held' where id = :id"), {"id": box_id}
        )

        await act_as(db, actors["ops"])
        async with rejected(db, containing="cannot be set to short_accepted directly"):
            await db.execute(
                text("update boxes set status = 'short_accepted' where id = :id"),
                {"id": box_id},
            )

    async def test_scanned_units_cannot_be_written_directly(self, db, gate_entry, actors):
        """The counter is derived. If it were writable, CP3 would prove nothing."""
        box_id, expected, codes = await self._prepare_open_box(db, gate_entry, actors)

        await act_as(db, actors["offloader"])
        with pytest.raises(DBAPIError) as err:
            await db.execute(
                text("update boxes set scanned_units = :n where id = :id"),
                {"n": expected, "id": box_id},
            )
        assert "derived from scan_events" in str(err.value)


# ===========================================================================
# EXCEPTION RESOLUTION (DECISIONS.md §3)
# ===========================================================================


class TestExceptionResolution:
    async def test_accept_short_releases_only_what_arrived(self, db, gate_entry, actors):
        helper = TestControlPoint3()
        box_id, expected, codes = await helper._prepare_open_box(db, gate_entry, actors)

        await act_as(db, actors["offloader"])
        for code in codes[:-2]:
            await _scan(db, code, "unit_verify", actors["offloader"])
        await db.execute(text("update boxes set status = 'held' where id = :id"), {"id": box_id})

        exc_id = (
            await db.execute(
                text(
                    """
                    insert into exceptions
                      (exception_type, gate_entry_id, box_id, title, reported_by)
                    values ('unit_count_mismatch', :e, :b, 'Short delivery', :who)
                    returning id
                    """
                ),
                {"e": gate_entry["id"], "b": box_id, "who": actors["offloader"]},
            )
        ).scalar_one()

        line_id = (
            await db.execute(
                text("select purchase_order_line_id from boxes where id = :id"), {"id": box_id}
            )
        ).scalar_one()
        before = (
            await db.execute(
                text("select received_units from purchase_order_lines where id = :id"),
                {"id": line_id},
            )
        ).scalar_one()

        await act_as(db, actors["ops"])
        await db.execute(
            text(
                """
                update exceptions
                   set status = 'resolved', resolution = 'accept_short',
                       resolution_note = 'Vendor confirmed short supply',
                       resolved_by = :ops, resolved_at = now()
                 where id = :id
                """
            ),
            {"ops": actors["ops"], "id": exc_id},
        )

        box_status = (
            await db.execute(
                text("select status::text from boxes where id = :id"), {"id": box_id}
            )
        ).scalar_one()
        after = (
            await db.execute(
                text("select received_units from purchase_order_lines where id = :id"),
                {"id": line_id},
            )
        ).scalar_one()

        assert box_status == "short_accepted"
        assert after - before == expected - 2, "only the units that arrived are received"

    async def test_recount_reopens_the_box_and_keeps_the_evidence(self, db, gate_entry, actors):
        helper = TestControlPoint3()
        box_id, expected, codes = await helper._prepare_open_box(db, gate_entry, actors)

        await act_as(db, actors["offloader"])
        for code in codes[:-2]:
            await _scan(db, code, "unit_verify", actors["offloader"])
        await db.execute(text("update boxes set status = 'held' where id = :id"), {"id": box_id})

        scans_before = (
            await db.execute(
                text("select count(*) from scan_events where box_id = :id and accepted"),
                {"id": box_id},
            )
        ).scalar_one()

        exc_id = (
            await db.execute(
                text(
                    """
                    insert into exceptions
                      (exception_type, gate_entry_id, box_id, title, reported_by)
                    values ('unit_count_mismatch', :e, :b, 'Suspected miscount', :who)
                    returning id
                    """
                ),
                {"e": gate_entry["id"], "b": box_id, "who": actors["offloader"]},
            )
        ).scalar_one()

        await act_as(db, actors["ops"])
        await db.execute(
            text(
                """
                update exceptions
                   set status = 'resolved', resolution = 'recount',
                       resolution_note = 'Recount requested', resolved_by = :ops, resolved_at = now()
                 where id = :id
                """
            ),
            {"ops": actors["ops"], "id": exc_id},
        )

        box = (
            await db.execute(
                text("select status::text as status, scanned_units from boxes where id = :id"),
                {"id": box_id},
            )
        ).mappings().one()
        scans_after = (
            await db.execute(
                text("select count(*) from scan_events where box_id = :id and accepted"),
                {"id": box_id},
            )
        ).scalar_one()

        assert box["status"] == "verified", "box reopens for scanning"
        assert box["scanned_units"] == 0, "counter resets"
        assert scans_after == scans_before, "the original scans stay in the ledger as evidence"


# ===========================================================================
# CONTROL POINT 4 — inbound reconciliation
# ===========================================================================


class TestControlPoint4:
    async def test_unmatched_count_is_flagged(self, db, gate_entry, actors):
        """`matched` is a generated column, so it cannot be set to true by hand."""
        line = (
            await db.execute(
                text(
                    """
                    select id from purchase_order_lines
                     where purchase_order_id = :po order by line_no limit 1
                    """
                ),
                {"po": gate_entry["po_id"]},
            )
        ).scalar_one()

        await act_as(db, actors["inbound"])
        await db.execute(
            text(
                """
                insert into inbound_reconciliations
                  (gate_entry_id, purchase_order_line_id, warehouse_count, inbound_count, verified_by)
                values (:e, :l, 10, 9, :who)
                """
            ),
            {"e": gate_entry["id"], "l": line, "who": actors["inbound"]},
        )

        matched = (
            await db.execute(
                text("select matched from inbound_reconciliations where gate_entry_id = :e"),
                {"e": gate_entry["id"]},
            )
        ).scalar_one()
        assert matched is False


# ===========================================================================
# IMMUTABILITY AND AUDIT (PRD §7)
# ===========================================================================


class TestImmutability:
    async def test_gate_entries_cannot_be_deleted(self, db, gate_entry, actors):
        await act_as(db, actors["admin"])
        with pytest.raises(DBAPIError) as err:
            await db.execute(
                text("delete from gate_entries where id = :id"), {"id": gate_entry["id"]}
            )
        assert "Deletion is not permitted" in str(err.value)

    async def test_scan_events_cannot_be_edited(self, db, gate_entry, actors):
        boxes = await _issue_boxes(db, gate_entry["id"], actors)
        await act_as(db, actors["guard"])
        code = await _sticker_code(db, boxes[0])
        await _scan(db, code, "box_verify", actors["guard"])

        with pytest.raises(DBAPIError) as err:
            await db.execute(text("update scan_events set accepted = false where raw_code = :c"),
                             {"c": code})
        assert "append-only" in str(err.value)

    async def test_every_change_is_audited_with_an_actor(self, db, gate_entry, actors):
        rows = list(
            (
                await db.execute(
                    text(
                        """
                        select action, actor_id, actor_role, changed_keys
                          from audit_log
                         where table_name = 'gate_entries' and record_id = :id
                         order by occurred_at
                        """
                    ),
                    {"id": gate_entry["id"]},
                )
            ).mappings()
        )

        assert len(rows) >= 3, "insert, approval and admission are all recorded"
        assert all(r["actor_id"] is not None for r in rows), "no anonymous changes"

        approval = [r for r in rows if r["changed_keys"] and "decided_by" in r["changed_keys"]]
        assert approval, "the approval itself is in the trail"
        # actors["ops"] (boopathi) is the Ops Manager who decides gate entries.
        assert approval[0]["actor_role"] == "ops_manager"
