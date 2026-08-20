"""Phase 4 — CONTROL POINT 7, at the database level.

The failure this prevents is the only one in the system that cannot be corrected
afterwards: a vehicle leaving with a carton missing. Everything earlier can be
recounted, re-scanned or held. Once a truck is on the road, a missing carton is
somebody else's problem and nobody's record.
"""

import uuid

import pytest
from sqlalchemy import text

from tests.conftest import rejected
from tests.test_packing import (  # noqa: F401
    _approve_load,
    _batch_of,
    _out_scan,
    _packed_invoices,
    people,
)

pytestmark = pytest.mark.asyncio


async def _released_batch(db, people, count=3):
    """A batch taken all the way through out-scan and release."""
    invoices = await _packed_invoices(db, people, count)
    batch_id = await _batch_of(db, invoices, people)

    for inv in invoices:
        await _out_scan(db, inv["invoice_number"], people["ops"]["id"])

    await db.execute(
        text("update batches set status = 'complete' where id = :id"), {"id": batch_id}
    )
    # Migration 0018: the guard's carton count has to be approved before a batch
    # can be released to the pickup area.
    await _approve_load(db, batch_id, people)
    await db.execute(
        text(
            """
            update batches
               set status = 'released', released_by = :who, released_at = now()
             where id = :id
            """
        ),
        {"who": people["ops"]["id"], "id": batch_id},
    )
    return batch_id, invoices


async def _visitor(db, mobile="9876512345"):
    return (
        await db.execute(
            text(
                """
                insert into visitors (mobile, full_name, id_photo_path, id_photo_captured_at)
                values (:m, 'Courier Driver', 'seed/courier.jpg', now())
                on conflict (mobile) do update set last_seen_at = now()
                returning id
                """
            ),
            {"m": mobile},
        )
    ).scalar_one()


async def _pickup(db, batch_id, guard_id, vehicle="KA05CD9999"):
    pickup_id = (
        await db.execute(
            text(
                """
                insert into pickups (batch_id, vehicle_number, registered_by)
                values (:b, :v, :who) returning id
                """
            ),
            {"b": batch_id, "v": vehicle, "who": guard_id},
        )
    ).scalar_one()

    visitor_id = await _visitor(db)
    await db.execute(
        text(
            """
            insert into pickup_persons (pickup_id, visitor_id, visitor_role)
            values (:p, :v, 'driver')
            """
        ),
        {"p": pickup_id, "v": visitor_id},
    )
    return pickup_id


async def _request_and_approve_exit(db, pickup_id, actors, people):
    """The guard asks for the gate to be opened and Ops approves.

    Added by migration 0018. The two people must differ, so the guard requests
    and Ops decides — the same separation as CONTROL POINT 1 at the other end of
    the process.
    """
    await db.execute(
        text(
            """
            update pickups
               set status = 'exit_pending', exit_requested_by = :guard
             where id = :id
            """
        ),
        {"guard": actors["guard"], "id": pickup_id},
    )
    await db.execute(
        text(
            """
            update pickups
               set exit_approved_by = :ops, exit_approved_at = now()
             where id = :id
            """
        ),
        {"ops": people["ops"]["id"], "id": pickup_id},
    )


async def _exit_scan(db, code, who):
    return (
        await db.execute(
            text(
                """
                insert into scan_events
                  (client_event_id, scan_type, raw_code, accepted, scanned_by, scanned_at)
                values (:cid, 'gate_exit', :code, false, :who, now())
                returning accepted, reject_reason::text as reject_reason, invoice_id
                """
            ),
            {"cid": str(uuid.uuid4()), "code": code, "who": who},
        )
    ).mappings().one()


class TestPickupRegistration:
    async def test_cannot_register_against_an_unreleased_batch(self, db, actors, people):
        """Otherwise a truck starts loading goods Ops has not finished checking."""
        invoices = await _packed_invoices(db, people, 2)
        batch_id = await _batch_of(db, invoices, people)

        async with rejected(db, containing="has not been released"):
            await _pickup(db, batch_id, actors["guard"])

    async def test_can_register_against_a_released_batch(self, db, actors, people):
        batch_id, _ = await _released_batch(db, people, 2)
        pickup_id = await _pickup(db, batch_id, actors["guard"])

        row = (
            await db.execute(
                text(
                    """
                    select pickup_code, status, released_cartons, verified_cartons
                      from v_pickup_status where pickup_id = :id
                    """
                ),
                {"id": pickup_id},
            )
        ).mappings().one()

        assert row["status"] == "registered"
        assert row["pickup_code"].startswith("PU-")
        assert (row["released_cartons"], row["verified_cartons"]) == (2, 0)

    async def test_only_one_pickup_per_batch(self, db, actors, people):
        """Two vehicles for one batch would make 'all cartons present' ambiguous."""
        batch_id, _ = await _released_batch(db, people, 2)
        await _pickup(db, batch_id, actors["guard"])

        async with rejected(db):
            await _pickup(db, batch_id, actors["guard"], vehicle="KA09XX1111")

    async def test_cannot_be_created_already_departed(self, db, actors, people):
        batch_id, _ = await _released_batch(db, people, 1)

        async with rejected(db, containing="already verified or departed"):
            await db.execute(
                text(
                    """
                    insert into pickups
                      (batch_id, vehicle_number, registered_by, time_out)
                    values (:b, 'KA01AA0001', :who, now())
                    """
                ),
                {"b": batch_id, "who": actors["guard"]},
            )


class TestGateExitScanning:
    async def test_unreleased_carton_cannot_be_loaded(self, db, actors, people):
        """Stops a carton going straight from the packing bench onto a truck."""
        invoices = await _packed_invoices(db, people, 1)
        await _batch_of(db, invoices, people)

        result = await _exit_scan(db, invoices[0]["invoice_number"], actors["guard"])
        assert result["accepted"] is False
        assert result["reject_reason"] == "batch_not_released"

    async def test_released_carton_needs_a_registered_vehicle(self, db, actors, people):
        batch_id, invoices = await _released_batch(db, people, 1)

        result = await _exit_scan(db, invoices[0]["invoice_number"], actors["guard"])
        assert result["accepted"] is False
        assert result["reject_reason"] == "no_pickup_registered"

    async def test_scan_advances_the_pickup(self, db, actors, people):
        batch_id, invoices = await _released_batch(db, people, 3)
        pickup_id = await _pickup(db, batch_id, actors["guard"])

        result = await _exit_scan(db, invoices[0]["invoice_number"], actors["guard"])
        assert result["accepted"] is True

        row = (
            await db.execute(
                text(
                    """
                    select status, verified_cartons, remaining_cartons
                      from v_pickup_status where pickup_id = :id
                    """
                ),
                {"id": pickup_id},
            )
        ).mappings().one()

        assert row["status"] == "verifying", "the first carton moves it off 'registered'"
        assert (row["verified_cartons"], row["remaining_cartons"]) == (1, 2)

    async def test_same_carton_cannot_be_loaded_twice(self, db, actors, people):
        batch_id, invoices = await _released_batch(db, people, 2)
        await _pickup(db, batch_id, actors["guard"])

        first = await _exit_scan(db, invoices[0]["invoice_number"], actors["guard"])
        second = await _exit_scan(db, invoices[0]["invoice_number"], actors["guard"])

        assert first["accepted"] is True
        assert second["accepted"] is False
        assert second["reject_reason"] == "already_scanned"

    async def test_scanning_into_a_cancelled_pickup_is_refused(self, db, actors, people):
        """A cancelled pickup is the only way a carton can still be unscanned while
        its pickup is closed — departure requires everything scanned, so there is
        nothing left to scan afterwards."""
        batch_id, invoices = await _released_batch(db, people, 3)
        pickup_id = await _pickup(db, batch_id, actors["guard"])

        await _exit_scan(db, invoices[0]["invoice_number"], actors["guard"])

        await db.execute(
            text(
                """
                update pickups
                   set status = 'cancelled', cancel_reason = 'Courier left without loading'
                 where id = :id
                """
            ),
            {"id": pickup_id},
        )

        result = await _exit_scan(db, invoices[1]["invoice_number"], actors["guard"])
        assert result["accepted"] is False
        assert result["reject_reason"] == "wrong_pickup"

    async def test_a_carton_from_another_batch_is_refused(self, db, actors, people):
        batch_id, invoices = await _released_batch(db, people, 1)
        await _pickup(db, batch_id, actors["guard"])

        other = await _packed_invoices(db, people, 1)
        result = await _exit_scan(db, other[0]["invoice_number"], actors["guard"])

        assert result["accepted"] is False
        assert result["reject_reason"] == "not_in_batch"


class TestControlPoint7:
    async def test_cannot_verify_with_a_carton_missing(self, db, actors, people):
        """The whole point: the truck does not leave one carton short."""
        batch_id, invoices = await _released_batch(db, people, 3)
        pickup_id = await _pickup(db, batch_id, actors["guard"])

        for inv in invoices[:2]:
            await _exit_scan(db, inv["invoice_number"], actors["guard"])

        async with rejected(db, containing="CONTROL POINT 7"):
            await db.execute(
                text(
                    """
                    update pickups set status = 'verified', verified_by = :who
                     where id = :id
                    """
                ),
                {"who": actors["guard"], "id": pickup_id},
            )

    async def test_cannot_depart_without_verification(self, db, actors, people):
        batch_id, invoices = await _released_batch(db, people, 2)
        pickup_id = await _pickup(db, batch_id, actors["guard"])
        await _exit_scan(db, invoices[0]["invoice_number"], actors["guard"])

        async with rejected(db, containing="CONTROL POINT 7"):
            await db.execute(
                text(
                    """
                    update pickups set status = 'departed', released_by = :who
                     where id = :id
                    """
                ),
                {"who": actors["guard"], "id": pickup_id},
            )

    async def test_full_load_verifies_and_departs(self, db, actors, people):
        batch_id, invoices = await _released_batch(db, people, 3)
        pickup_id = await _pickup(db, batch_id, actors["guard"])

        for inv in invoices:
            await _exit_scan(db, inv["invoice_number"], actors["guard"])

        await db.execute(
            text(
                """
                update pickups set status = 'verified', verified_by = :who
                 where id = :id
                """
            ),
            {"who": actors["guard"], "id": pickup_id},
        )
        await _request_and_approve_exit(db, pickup_id, actors, people)
        await db.execute(
            text(
                """
                update pickups set status = 'departed', released_by = :who
                 where id = :id
                """
            ),
            {"who": actors["guard"], "id": pickup_id},
        )

        row = (
            await db.execute(
                text(
                    """
                    select status, verified_cartons, released_cartons,
                           time_in, time_out, verified_by_name, released_by_name
                      from v_pickup_status where pickup_id = :id
                    """
                ),
                {"id": pickup_id},
            )
        ).mappings().one()

        assert row["status"] == "departed"
        assert row["verified_cartons"] == row["released_cartons"] == 3
        assert row["time_out"] is not None, "time out is stamped on release"
        assert row["time_out"] >= row["time_in"]
        assert row["verified_by_name"] and row["released_by_name"]

    async def test_release_requires_a_named_user(self, db, actors, people):
        batch_id, invoices = await _released_batch(db, people, 1)
        pickup_id = await _pickup(db, batch_id, actors["guard"])
        await _exit_scan(db, invoices[0]["invoice_number"], actors["guard"])
        await db.execute(
            text(
                "update pickups set status = 'verified', verified_by = :who where id = :id"
            ),
            {"who": actors["guard"], "id": pickup_id},
        )
        await _request_and_approve_exit(db, pickup_id, actors, people)

        async with rejected(db, containing="named user"):
            await db.execute(
                text("update pickups set status = 'departed' where id = :id"),
                {"id": pickup_id},
            )

    async def test_verification_requires_a_named_user(self, db, actors, people):
        batch_id, invoices = await _released_batch(db, people, 1)
        pickup_id = await _pickup(db, batch_id, actors["guard"])
        await _exit_scan(db, invoices[0]["invoice_number"], actors["guard"])

        async with rejected(db, containing="named user"):
            await db.execute(
                text("update pickups set status = 'verified' where id = :id"),
                {"id": pickup_id},
            )


class TestPickupAccess:
    async def test_offloader_cannot_register_a_pickup(self, db, actors, people):
        import json

        batch_id, _ = await _released_batch(db, people, 1)

        await db.execute(text("set local role authenticated"))
        await db.execute(
            text("select set_config('request.jwt.claims', :c, true)"),
            {"c": json.dumps({"sub": str(actors["offloader"]), "role": "authenticated"})},
        )

        async with rejected(db, containing="row-level security"):
            await db.execute(
                text(
                    """
                    insert into pickups (batch_id, vehicle_number, registered_by)
                    values (:b, 'KA01AA0002', :who)
                    """
                ),
                {"b": batch_id, "who": actors["offloader"]},
            )

        await db.execute(text("reset role"))

    async def test_guard_can_register_and_scan_under_rls(self, db, actors, people):
        """The full outbound flow with RLS actually switched on.

        Worth a dedicated test: the gate-exit trigger updates packing_records, and
        the Phase-3 policy allowed only Ops to do that — so under RLS the stamp
        would have silently matched zero rows and the carton would never count.
        """
        import json

        batch_id, invoices = await _released_batch(db, people, 2)

        await db.execute(text("set local role authenticated"))
        await db.execute(
            text("select set_config('request.jwt.claims', :c, true)"),
            {"c": json.dumps({"sub": str(actors["guard"]), "role": "authenticated"})},
        )

        pickup_id = await _pickup(db, batch_id, actors["guard"])
        for inv in invoices:
            result = await _exit_scan(db, inv["invoice_number"], actors["guard"])
            assert result["accepted"] is True

        await db.execute(
            text(
                "update pickups set status = 'verified', verified_by = :who where id = :id"
            ),
            {"who": actors["guard"], "id": pickup_id},
        )
        await db.execute(
            text(
                """
                update pickups set status = 'exit_pending', exit_requested_by = :who
                 where id = :id
                """
            ),
            {"who": actors["guard"], "id": pickup_id},
        )

        # Ops approves, not the guard. Stepping out of the guard's role to do it
        # is the point: migration 0018 refuses an approval from the person who
        # requested it, and refuses one from anybody who is not Ops.
        await db.execute(text("reset role"))
        await db.execute(
            text(
                """
                update pickups set exit_approved_by = :ops, exit_approved_at = now()
                 where id = :id
                """
            ),
            {"ops": people["ops"]["id"], "id": pickup_id},
        )
        await db.execute(text("set local role authenticated"))

        await db.execute(
            text(
                "update pickups set status = 'departed', released_by = :who where id = :id"
            ),
            {"who": actors["guard"], "id": pickup_id},
        )
        await db.execute(text("reset role"))

        row = (
            await db.execute(
                text(
                    """
                    select status, verified_cartons, released_cartons
                      from v_pickup_status where pickup_id = :id
                    """
                ),
                {"id": pickup_id},
            )
        ).mappings().one()

        assert row["status"] == "departed"
        assert row["verified_cartons"] == row["released_cartons"] == 2, (
            "the gate-exit stamp must succeed for a guard, not silently match zero rows"
        )
