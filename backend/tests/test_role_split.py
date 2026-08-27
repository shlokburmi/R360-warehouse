"""Role reintroduction (PRD §2 / §8), enforced by RLS — 0023_role_split.sql.

`ops_manager`, `invoice_matcher` and `warehouse_staff` were reintroduced as
distinct roles at the user's request, reversing part of the consolidation
docs/DECISIONS.md §CE1/§C5 recorded. This exercises the actual boundary each
one now has: what it can do that its old stand-in role could not, and what it
still cannot do. Every test connects as `authenticated` with a role's JWT
claims — the same mechanism app/db/session.py uses per request — because the
guarantee under test is that RLS enforces this independently of the API.
"""

import uuid

import pytest
from sqlalchemy import text

from tests.conftest import rejected
from tests.test_packing import _new_invoice, _receive_goods
from tests.test_rls import as_authenticated, as_postgres

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def people(db):
    rows = await db.execute(
        text(
            """
            select employee_code, id, role::text as role
              from profiles
             where employee_code in
                   ('EMP-G01','EMP-O01','EMP-F01','EMP-W01','EMP-M01','EMP-P01','EMP-A01')
            """
        )
    )
    by_code = {r["employee_code"]: dict(r) for r in rows.mappings()}
    if "EMP-O01" not in by_code:
        pytest.skip("Seed data not loaded — run `supabase db reset`")
    return {
        "guard": by_code["EMP-G01"]["id"],
        "ops_manager": by_code["EMP-O01"]["id"],
        "offloading": by_code["EMP-F01"]["id"],
        "warehouse_staff": by_code["EMP-W01"]["id"],
        "invoice_matcher": by_code["EMP-M01"]["id"],
        "packer": by_code["EMP-P01"]["id"],
        "admin": by_code["EMP-A01"]["id"],
    }


@pytest.fixture
async def pending_entry(db, people):
    """A gate entry sitting in pending_approval, for CP1 decision tests."""
    po = (
        await db.execute(
            text("select id, vendor_id from purchase_orders where po_number = 'PO-2026-0001'")
        )
    ).mappings().one()

    await as_authenticated(db, people["guard"])
    entry_id = (
        await db.execute(
            text(
                """
                insert into gate_entries
                  (status, vehicle_number, vendor_id, purchase_order_id, requested_by, requested_at)
                values ('pending_approval', :veh, :vendor, :po, :guard, now())
                returning id
                """
            ),
            {
                "veh": f"KA01RS{uuid.uuid4().hex[:4].upper()}",
                "vendor": po["vendor_id"],
                "po": po["id"],
                "guard": people["guard"],
            },
        )
    ).scalar_one()
    await as_postgres(db)
    return entry_id


class TestOpsManager:
    """PRD §5.8/§8: approvals, sticker sheets, out-scan, batch release, reports."""

    async def test_ops_manager_can_decide_a_gate_entry(self, db, people, pending_entry):
        await as_authenticated(db, people["ops_manager"])
        await db.execute(
            text(
                """
                update gate_entries set status = 'approved',
                       decided_by = :who, decided_at = now()
                 where id = :id
                """
            ),
            {"who": people["ops_manager"], "id": pending_entry},
        )
        await as_postgres(db)

        row = (
            await db.execute(
                text("select status from gate_entries where id = :id"), {"id": pending_entry}
            )
        ).mappings().one()
        assert row["status"] == "approved"

    async def test_a_packer_cannot_decide_a_gate_entry(self, db, people, pending_entry):
        """RLS turns a forbidden UPDATE into a no-op, not an error (the RLS USING
        clause simply matches no rows for this role/status) — DECISIONS.md Part
        D — so the assertion is "nothing changed", not "it raised"."""
        await as_authenticated(db, people["packer"])
        await db.execute(
            text(
                """
                update gate_entries set status = 'approved',
                       decided_by = :who, decided_at = now()
                 where id = :id
                """
            ),
            {"who": people["packer"], "id": pending_entry},
        )
        await as_postgres(db)

        row = (
            await db.execute(
                text("select status, decided_by from gate_entries where id = :id"),
                {"id": pending_entry},
            )
        ).mappings().one()
        assert row["status"] == "pending_approval"
        assert row["decided_by"] is None

    async def test_ops_manager_can_read_the_audit_log(self, db, people, pending_entry):
        """PRD §8: 'Ops Manager can see everything' — the same access Admin has."""
        await as_authenticated(db, people["ops_manager"])
        count = (await db.execute(text("select count(*) from audit_log"))).scalar_one()
        await as_postgres(db)
        assert count > 0

    async def test_admin_still_covers_the_approval(self, db, people, pending_entry):
        """require_roles()/has_role() union with admin everywhere — Admin keeps
        covering every station, same as it already does for packer (§CC3)."""
        await as_authenticated(db, people["admin"])
        await db.execute(
            text(
                """
                update gate_entries set status = 'approved',
                       decided_by = :who, decided_at = now()
                 where id = :id
                """
            ),
            {"who": people["admin"], "id": pending_entry},
        )
        await as_postgres(db)

        row = (
            await db.execute(
                text("select status from gate_entries where id = :id"), {"id": pending_entry}
            )
        ).mappings().one()
        assert row["status"] == "approved"


class TestInvoiceMatcher:
    """CONTROL POINT 5, first half — the badge that may hold it."""

    async def test_an_invoice_matcher_badge_may_verify(self, db, people):
        inv = await _new_invoice(db, units=1)
        inv["units"] = 1
        codes = await _receive_goods(
            db, inv, {"guard": {"id": people["guard"]}, "ops": {"id": people["ops_manager"]},
                      "offloader_id": people["offloading"]},
        )
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
                    "inv": inv["id"],
                    "who": people["invoice_matcher"],
                },
            )

        await as_authenticated(db, people["invoice_matcher"])
        await db.execute(
            text(
                "insert into invoice_verifications (invoice_id, verified_by) values (:i, :w)"
            ),
            {"i": inv["id"], "w": people["invoice_matcher"]},
        )
        await as_postgres(db)

        row = (
            await db.execute(
                text("select verified_by from invoice_verifications where invoice_id = :i"),
                {"i": inv["id"]},
            )
        ).mappings().one()
        assert str(row["verified_by"]) == str(people["invoice_matcher"])

    async def test_an_offloading_badge_may_not_verify(self, db, people):
        """Role-scoped like every other badge (fn_badge_holder_guard) — an
        Offloading account is not one of the roles CONTROL POINT 5 accepts."""
        inv = await _new_invoice(db, units=1)

        await as_authenticated(db, people["offloading"])
        async with rejected(db, containing="not permitted"):
            await db.execute(
                text(
                    "insert into invoice_verifications (invoice_id, verified_by) values (:i, :w)"
                ),
                {"i": inv["id"], "w": people["offloading"]},
            )
        await as_postgres(db)


class TestWarehouseStaff:
    """Putaway, carved back out of Offloading.

    The positive case — a warehouse_staff account putting a reconciled box
    away under RLS — is already covered by
    test_putaway.py::TestPutawayAccess::test_storeman_putaway_empties_the_box_under_rls
    (the "storeman" actor there is EMP-W01, now warehouse_staff). What that
    file did not have before this split is a check that Offloading — which
    could putaway before 0023_role_split.sql — no longer can.
    """

    async def test_offloading_can_no_longer_putaway(self, db, gate_entry, actors):
        from tests.test_putaway import _closed_box, _location, _reconcile

        box_id, units = await _closed_box(db, gate_entry, actors)
        await _reconcile(db, gate_entry, actors)
        location = await _location(db)

        line_id = (
            await db.execute(
                text("select purchase_order_line_id from boxes where id = :id"), {"id": box_id}
            )
        ).scalar_one()

        await as_authenticated(db, actors["offloader"])
        async with rejected(db, containing="row-level security"):
            await db.execute(
                text(
                    """
                    insert into putaways
                      (box_id, location_id, purchase_order_line_id, units, disposition, moved_by)
                    values (:box, :loc, :line, :u, 'stock', :who)
                    """
                ),
                {
                    "box": box_id,
                    "loc": location["id"],
                    "line": line_id,
                    "u": units,
                    "who": actors["offloader"],
                },
            )
        await as_postgres(db)

    async def test_warehouse_staff_cannot_reconcile(self, db, people, pending_entry):
        """CONTROL POINT 4 stays Offloading's, not Warehouse Staff's — the two
        duties that were folded into one role during consolidation split
        apart again, each to its own role.

        Same no-op-not-error shape as the packer/gate-entry case above: the
        RLS USING clause on gate_entries_update_reconcile matches nothing for
        this role, so the UPDATE silently changes zero rows.
        """
        await as_authenticated(db, people["warehouse_staff"])
        await db.execute(
            text(
                "update gate_entries set status = 'reconciled' where id = :id"
            ),
            {"id": pending_entry},
        )
        await as_postgres(db)

        row = (
            await db.execute(
                text("select status from gate_entries where id = :id"), {"id": pending_entry}
            )
        ).mappings().one()
        assert row["status"] == "pending_approval"
