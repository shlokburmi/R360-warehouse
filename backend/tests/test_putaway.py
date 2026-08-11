"""Phase 2 putaway rules, tested against the database that enforces them.

Three failures matter here, and each has a test:
  * putting away goods the inbound team has not agreed (CP4 becomes advisory)
  * placing more units than arrived (putaway invents inventory)
  * shelving damaged units as good stock (damaged goods reach a customer)
"""

import uuid

import pytest
from sqlalchemy import text

from tests.conftest import act_as, rejected

pytestmark = pytest.mark.asyncio


async def _closed_box(db, gate_entry, actors, *, damaged=0):
    """Take one box all the way to 'complete' so it is eligible for putaway."""
    from tests.test_control_points import _issue_boxes, _scan, _sticker_code

    # One box only. CONTROL POINT 3 refuses to finish offloading while any box
    # is still open, so a fixture that created six and closed one could never
    # reach the reconciled state these tests need.
    boxes = await _issue_boxes(db, gate_entry["id"], actors, count=1)

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

    box_id = boxes[0]
    expected = (
        await db.execute(text("select expected_units from boxes where id = :id"), {"id": box_id})
    ).scalar_one()

    await act_as(db, actors["ops"])
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

    await act_as(db, actors["offloader"])
    for index, code in enumerate(codes):
        disposition = "quarantine" if index < damaged else "stock"
        await db.execute(
            text(
                """
                insert into scan_events
                  (client_event_id, scan_type, raw_code, accepted, scanned_by, scanned_at,
                   disposition)
                values (:cid, 'unit_verify', :code, false, :actor, now(),
                        cast(:disp as unit_disposition))
                """
            ),
            {
                "cid": str(uuid.uuid4()),
                "code": code,
                "actor": actors["offloader"],
                "disp": disposition,
            },
        )

    await db.execute(
        text(
            """
            update boxes
               set damage_level = cast(:lvl as damage_level),
                   damage_note = case when :lvl = 'none' then null else 'crushed corner' end
             where id = :id
            """
        ),
        {"lvl": "none" if damaged == 0 else "product", "id": box_id},
    )
    await db.execute(
        text("update boxes set status = 'complete' where id = :id"), {"id": box_id}
    )

    return box_id, expected


async def _location(db, *, quarantine=False):
    return (
        await db.execute(
            text(
                """
                select id, code from locations
                 where is_active and is_quarantine = :q
                 order by code limit 1
                """
            ),
            {"q": quarantine},
        )
    ).mappings().one()


async def _reconcile(db, gate_entry, actors):
    """Push the entry through CP4 so putaway is permitted."""
    await db.execute(
        text("update gate_entries set status = 'offloaded' where id = :id"),
        {"id": gate_entry["id"]},
    )

    lines = list(
        (
            await db.execute(
                text(
                    """
                    select pol.id,
                           coalesce((select sum(total_units) from v_warehouse_counts wc
                                      where wc.gate_entry_id = :e
                                        and wc.purchase_order_line_id = pol.id), 0)::int as cnt
                      from purchase_order_lines pol
                     where pol.purchase_order_id = :po
                    """
                ),
                {"e": gate_entry["id"], "po": gate_entry["po_id"]},
            )
        ).mappings()
    )

    await act_as(db, actors["inbound"])
    for line in lines:
        await db.execute(
            text(
                """
                insert into inbound_reconciliations
                  (gate_entry_id, purchase_order_line_id, warehouse_count, inbound_count,
                   verified_by)
                values (:e, :l, :c, :c, :who)
                """
            ),
            {"e": gate_entry["id"], "l": line["id"], "c": line["cnt"], "who": actors["inbound"]},
        )

    await db.execute(
        text("update gate_entries set status = 'reconciled' where id = :id"),
        {"id": gate_entry["id"]},
    )


class TestPutawayControlPoint:
    async def test_cannot_put_away_before_inbound_verification(self, db, gate_entry, actors):
        """This is what stops CONTROL POINT 4 from being merely advisory."""
        box_id, units = await _closed_box(db, gate_entry, actors)
        location = await _location(db)

        await act_as(db, actors["storeman"])
        async with rejected(db, containing="CONTROL POINT 4"):
            await db.execute(
                text(
                    """
                    insert into putaways
                      (box_id, location_id, purchase_order_line_id, units, disposition, moved_by)
                    select :b, :l, purchase_order_line_id, :u, 'stock', :who
                      from boxes where id = :b
                    """
                ),
                {"b": box_id, "l": location["id"], "u": units, "who": actors["storeman"]},
            )

    async def test_putaway_after_reconciliation_succeeds_and_empties_the_box(
        self, db, gate_entry, actors
    ):
        box_id, units = await _closed_box(db, gate_entry, actors)
        await _reconcile(db, gate_entry, actors)
        location = await _location(db)

        await act_as(db, actors["storeman"])
        await db.execute(
            text(
                """
                insert into putaways
                  (box_id, location_id, purchase_order_line_id, units, disposition, moved_by)
                select :b, :l, purchase_order_line_id, :u, 'stock', :who
                  from boxes where id = :b
                """
            ),
            {"b": box_id, "l": location["id"], "u": units, "who": actors["storeman"]},
        )

        status = (
            await db.execute(
                text("select status::text from boxes where id = :id"), {"id": box_id}
            )
        ).scalar_one()
        assert status == "emptied", "a fully shelved box becomes an empty carton"

        remaining = (
            await db.execute(
                text("select stock_remaining from v_box_putaway_status where box_id = :id"),
                {"id": box_id},
            )
        ).scalar_one()
        assert remaining == 0

    async def test_cannot_place_more_units_than_arrived(self, db, gate_entry, actors):
        """Otherwise putaway is a second, unaudited way to create inventory."""
        box_id, units = await _closed_box(db, gate_entry, actors)
        await _reconcile(db, gate_entry, actors)
        location = await _location(db)

        await act_as(db, actors["storeman"])
        async with rejected(db, containing="left to place"):
            await db.execute(
                text(
                    """
                    insert into putaways
                      (box_id, location_id, purchase_order_line_id, units, disposition, moved_by)
                    select :b, :l, purchase_order_line_id, :u, 'stock', :who
                      from boxes where id = :b
                    """
                ),
                {"b": box_id, "l": location["id"], "u": units + 1, "who": actors["storeman"]},
            )

    async def test_splitting_a_box_across_racks_is_allowed(self, db, gate_entry, actors):
        box_id, units = await _closed_box(db, gate_entry, actors)
        await _reconcile(db, gate_entry, actors)

        racks = list(
            (
                await db.execute(
                    text(
                        """
                        select id from locations
                         where is_active and not is_quarantine order by code limit 2
                        """
                    )
                )
            ).scalars()
        )

        await act_as(db, actors["storeman"])
        first, second = units - 3, 3
        for rack, qty in ((racks[0], first), (racks[1], second)):
            await db.execute(
                text(
                    """
                    insert into putaways
                      (box_id, location_id, purchase_order_line_id, units, disposition, moved_by)
                    select :b, :l, purchase_order_line_id, :u, 'stock', :who
                      from boxes where id = :b
                    """
                ),
                {"b": box_id, "l": rack, "u": qty, "who": actors["storeman"]},
            )

        row = (
            await db.execute(
                text(
                    """
                    select b.status::text as status, s.stock_remaining
                      from boxes b join v_box_putaway_status s on s.box_id = b.id
                     where b.id = :id
                    """
                ),
                {"id": box_id},
            )
        ).mappings().one()

        assert row["stock_remaining"] == 0
        assert row["status"] == "emptied"

    async def test_damaged_units_cannot_go_to_a_stock_rack(self, db, gate_entry, actors):
        """The failure this prevents is damaged goods being sold as new."""
        box_id, units = await _closed_box(db, gate_entry, actors, damaged=3)
        await _reconcile(db, gate_entry, actors)
        stock_rack = await _location(db, quarantine=False)

        await act_as(db, actors["storeman"])
        async with rejected(db, containing="quarantine location"):
            await db.execute(
                text(
                    """
                    insert into putaways
                      (box_id, location_id, purchase_order_line_id, units, disposition, moved_by)
                    select :b, :l, purchase_order_line_id, 3, 'quarantine', :who
                      from boxes where id = :b
                    """
                ),
                {"b": box_id, "l": stock_rack["id"], "who": actors["storeman"]},
            )

    async def test_good_units_cannot_go_to_quarantine(self, db, gate_entry, actors):
        box_id, units = await _closed_box(db, gate_entry, actors, damaged=3)
        await _reconcile(db, gate_entry, actors)
        q_rack = await _location(db, quarantine=True)

        await act_as(db, actors["storeman"])
        async with rejected(db, containing="cannot be placed in quarantine"):
            await db.execute(
                text(
                    """
                    insert into putaways
                      (box_id, location_id, purchase_order_line_id, units, disposition, moved_by)
                    select :b, :l, purchase_order_line_id, 1, 'stock', :who
                      from boxes where id = :b
                    """
                ),
                {"b": box_id, "l": q_rack["id"], "who": actors["storeman"]},
            )

    async def test_damaged_and_good_units_split_correctly(self, db, gate_entry, actors):
        box_id, units = await _closed_box(db, gate_entry, actors, damaged=3)
        await _reconcile(db, gate_entry, actors)

        stock_rack = await _location(db, quarantine=False)
        q_rack = await _location(db, quarantine=True)

        status = (
            await db.execute(
                text(
                    """
                    select stock_remaining, quarantine_remaining
                      from v_box_putaway_status where box_id = :id
                    """
                ),
                {"id": box_id},
            )
        ).mappings().one()

        assert status["quarantine_remaining"] == 3
        assert status["stock_remaining"] == units - 3

        await act_as(db, actors["storeman"])
        for rack, qty, disp in (
            (stock_rack["id"], units - 3, "stock"),
            (q_rack["id"], 3, "quarantine"),
        ):
            await db.execute(
                text(
                    """
                    insert into putaways
                      (box_id, location_id, purchase_order_line_id, units, disposition, moved_by)
                    select :b, :l, purchase_order_line_id, :u,
                           cast(:d as unit_disposition), :who
                      from boxes where id = :b
                    """
                ),
                {"b": box_id, "l": rack, "u": qty, "d": disp, "who": actors["storeman"]},
            )

        box_status = (
            await db.execute(
                text("select status::text from boxes where id = :id"), {"id": box_id}
            )
        ).scalar_one()
        assert box_status == "emptied", "empty only once both dispositions are placed"

        quarantined = (
            await db.execute(
                text(
                    """
                    select sum(units)::int from v_stock_by_location
                     where is_quarantine and sku is not null
                    """
                )
            )
        ).scalar_one()
        assert quarantined >= 3


async def as_authenticated(conn, actor_id):
    import json

    await conn.execute(text("set local role authenticated"))
    await conn.execute(
        text("select set_config('request.jwt.claims', :c, true)"),
        {"c": json.dumps({"sub": str(actor_id), "role": "authenticated"})},
    )


class TestPutawayAccess:
    async def test_storeman_putaway_empties_the_box_under_rls(self, db, gate_entry, actors):
        """Regression: the whole flow, with RLS actually switched on.

        The tests above run as the table owner, so RLS is bypassed and they
        passed while the box-update policy still excluded warehouse_staff. The
        result was `fn_putaway_close_box` matching zero rows: no error, no
        exception, just a carton stuck at 'complete' forever. Only a test that
        assumes the role catches that, so this one does.
        """
        box_id, units = await _closed_box(db, gate_entry, actors)
        await _reconcile(db, gate_entry, actors)
        location = await _location(db)

        await as_authenticated(db, actors["storeman"])
        await db.execute(
            text(
                """
                insert into putaways
                  (box_id, location_id, purchase_order_line_id, units, disposition, moved_by)
                select :b, :l, purchase_order_line_id, :u, 'stock', :who
                  from boxes where id = :b
                """
            ),
            {"b": box_id, "l": location["id"], "u": units, "who": actors["storeman"]},
        )
        await db.execute(text("reset role"))

        status = (
            await db.execute(
                text("select status::text from boxes where id = :id"), {"id": box_id}
            )
        ).scalar_one()
        assert status == "emptied", (
            "the close-box trigger must succeed for warehouse staff, not silently "
            "match zero rows"
        )

    async def test_guard_cannot_put_goods_away(self, db, gate_entry, actors):
        box_id, units = await _closed_box(db, gate_entry, actors)
        await _reconcile(db, gate_entry, actors)
        location = await _location(db)

        import json

        await db.execute(text("set local role authenticated"))
        await db.execute(
            text("select set_config('request.jwt.claims', :c, true)"),
            {"c": json.dumps({"sub": str(actors["guard"]), "role": "authenticated"})},
        )

        async with rejected(db, containing="row-level security"):
            await db.execute(
                text(
                    """
                    insert into putaways
                      (box_id, location_id, purchase_order_line_id, units, disposition, moved_by)
                    select :b, :l, purchase_order_line_id, 1, 'stock', :who
                      from boxes where id = :b
                    """
                ),
                {"b": box_id, "l": location["id"], "who": actors["guard"]},
            )

        await db.execute(text("reset role"))
