"""Role reintroduction (PRD §2 / §8), enforced by RLS — 0023_role_split.sql.

`ops_manager`, `invoice_matcher` and `warehouse_staff` were reintroduced as
distinct roles at the user's request, reversing part of the consolidation
docs/DECISIONS.md §CE1/§C5 recorded. This exercises the actual boundary each
one now has: what it can do that its old stand-in role could not, and what it
still cannot do. Every test connects as `authenticated` with a role's JWT
claims — the same mechanism app/db/session.py uses per request — because the
guarantee under test is that RLS enforces this independently of the API.

`warehouse_staff` has since been retired along with putaway (0042). Its enum
value and the seeded EMP-W01 account both still exist — an account cannot be
un-created — so it stays fixtured here, now to prove it grants nothing.
"""

import uuid

import pytest
from sqlalchemy import text

from tests.conftest import rejected
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
                # Must be exactly 2 letters, 2 digits, 2 letters, 4 digits
                # (gate_entries_vehicle_number_check, 0027).
                "veh": f"KA01RS{uuid.uuid4().int % 10000:04d}",
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


class TestRetiredPutaway:
    """0042. Putaway is gone, and with it the role that existed to do it.

    What is left to check is that the door is actually shut: `putaways` keeps
    its rows and its SELECT policy (PRD §7 — a retired feature's history is
    still history), and nothing can write to it any more. The role that used to
    is checked with it, because "the API no longer offers it" and "the database
    no longer allows it" are different claims.
    """

    async def test_nobody_can_write_a_putaway_any_more(self, db, people):
        for who in ("warehouse_staff", "offloading", "admin"):
            await as_authenticated(db, people[who])
            async with rejected(db):
                await db.execute(
                    text(
                        """
                        insert into putaways
                          (box_id, location_id, purchase_order_line_id, units,
                           disposition, moved_by)
                        select b.id, l.id, b.purchase_order_line_id, 1, 'stock', :who
                          from boxes b cross join locations l limit 1
                        """
                    ),
                    {"who": people[who]},
                )
            await as_postgres(db)

    async def test_the_history_is_still_readable(self, db, people):
        """Retired, not deleted. A row that was shelved last month is still a
        row that was shelved last month."""
        await as_authenticated(db, people["offloading"])
        await db.execute(text("select count(*) from putaways"))
        await db.execute(text("select count(*) from locations"))
        await as_postgres(db)

    async def test_the_views_that_drove_the_screen_are_gone(self, db):
        for view in ("v_putaway_queue", "v_box_putaway_status", "v_stock_by_location"):
            assert (
                await db.execute(
                    text("select to_regclass(:v)"), {"v": f"public.{view}"}
                )
            ).scalar_one() is None, f"{view} still exists"

    async def test_a_box_can_no_longer_be_emptied(self, db, people):
        """`emptied` was only ever reachable through a completed putaway. The
        status stays in the enum — Postgres cannot drop one, and old rows may
        hold it — but nothing can set it now, which is the correct end state
        rather than a gap."""
        box_id = (
            await db.execute(text("select id from boxes limit 1"))
        ).scalar_one_or_none()
        if box_id is None:
            pytest.skip("No boxes in this database to try it on")

        await as_authenticated(db, people["offloading"])
        async with rejected(db):
            await db.execute(
                text("update boxes set status = 'emptied' where id = :id"), {"id": box_id}
            )
        await as_postgres(db)


class TestRoleGrantsAreAdminOnly:
    """0039. Staff CRUD is Ops Manager's; deciding what role an account holds
    is not.

    0033 moved `profiles_admin_all` to is_ops_manager() so an Ops Manager could
    add and edit staff. `role` is a column on that row, so "edit staff"
    silently included "promote yourself to Admin" — which hands over password
    reset on every account, the audit history, and both halves of CONTROL
    POINT 5, i.e. exactly what 0033 said it was not extending. Confirmed over
    HTTP before the fix: PATCH /admin/staff/{own id} {"role":"admin"} → 200.
    """

    async def test_ops_manager_cannot_promote_themselves(self, db, people):
        await as_authenticated(db, people["ops_manager"])
        async with rejected(db, containing="Only an Admin can change what role"):
            await db.execute(
                text("update profiles set role = 'admin' where id = :id"),
                {"id": people["ops_manager"]},
            )
        await as_postgres(db)

        assert (
            await db.execute(
                text("select role::text from profiles where id = :id"),
                {"id": people["ops_manager"]},
            )
        ).scalar_one() == "ops_manager"

    async def test_ops_manager_cannot_promote_anyone_else_either(self, db, people):
        """The self-promotion case is the obvious one; making a *colleague* an
        Admin and borrowing their account is the same escalation with a step in
        between."""
        await as_authenticated(db, people["ops_manager"])
        async with rejected(db, containing="Only an Admin can change what role"):
            await db.execute(
                text("update profiles set role = 'admin' where id = :id"),
                {"id": people["packer"]},
            )
        await as_postgres(db)

    async def test_ops_manager_cannot_create_an_admin(self, db, people):
        await as_authenticated(db, people["ops_manager"])
        async with rejected(db, containing="Only an Admin can create an Admin"):
            await db.execute(
                text(
                    """
                    insert into profiles (id, full_name, role, employee_code)
                    values (:id, 'Smuggled Admin', 'admin', :code)
                    """
                ),
                {"id": str(uuid.uuid4()), "code": f"EMP-X{uuid.uuid4().hex[:3].upper()}"},
            )
        await as_postgres(db)

    async def test_ops_manager_keeps_the_staff_edits_0033_granted(self, db, people):
        """The fix has to be narrow: everything else on the Staff screen is
        still theirs, or 0039 would be a rollback of 0033 rather than a
        correction to it."""
        await as_authenticated(db, people["ops_manager"])
        await db.execute(
            text("update profiles set full_name = 'Kavitha S.' where id = :id"),
            {"id": people["packer"]},
        )
        await db.execute(
            text("update profiles set is_active = false where id = :id"),
            {"id": people["packer"]},
        )
        await as_postgres(db)

        row = (
            await db.execute(
                text("select full_name, is_active from profiles where id = :id"),
                {"id": people["packer"]},
            )
        ).mappings().one()
        assert row["full_name"] == "Kavitha S."
        assert row["is_active"] is False

    async def test_an_admin_can_still_grant_roles(self, db, people):
        await as_authenticated(db, people["admin"])
        await db.execute(
            text("update profiles set role = 'ops_manager' where id = :id"),
            {"id": people["packer"]},
        )
        await as_postgres(db)

        assert (
            await db.execute(
                text("select role::text from profiles where id = :id"),
                {"id": people["packer"]},
            )
        ).scalar_one() == "ops_manager"

    async def test_the_seed_and_migrations_are_unaffected(self, db, people):
        """auth_role() is null when nobody is signed in — a migration, the seed,
        the worker. The guard has to let those through, or the seed could not
        create the first Admin and there would be no way into the system."""
        await as_postgres(db)
        await db.execute(text("select set_config('request.jwt.claims', '', true)"))
        await db.execute(
            text("update profiles set role = 'admin' where id = :id"),
            {"id": people["packer"]},
        )
        assert (
            await db.execute(
                text("select role::text from profiles where id = :id"),
                {"id": people["packer"]},
            )
        ).scalar_one() == "admin"
