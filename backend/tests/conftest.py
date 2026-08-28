"""Test fixtures.

These tests run against a real Postgres, because what they are testing *is* the
database — triggers, constraints and RLS policies. Mocking the database would
mean asserting that the mock does what we told it to, which proves nothing about
whether CONTROL POINT 3 actually holds.

Start the stack first:  supabase start && supabase db reset
If no database is reachable the whole module skips rather than failing red, so
`pytest` stays useful on a laptop with nothing running.
"""

import os
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

ADMIN_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@127.0.0.1:54322/postgres",
)


# Function-scoped, with NullPool. pytest-asyncio gives each test its own event
# loop, and an asyncpg connection is bound to the loop that created it — a
# pooled connection shared across tests fails with "attached to a different
# loop". Reconnecting per test costs a few milliseconds against a local
# Postgres and removes the whole class of problem.
@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(ADMIN_URL, poolclass=NullPool)
    try:
        async with eng.connect() as conn:
            await conn.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001
        await eng.dispose()
        pytest.skip(f"No test database at {ADMIN_URL}: {exc}")
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db(engine) -> AsyncIterator[AsyncConnection]:
    """A connection wrapped in a transaction that is always rolled back.

    Every test therefore starts from the seeded state and leaves nothing behind,
    which matters more than usual here: the no-deletion policy means a test that
    committed could not clean up after itself even if it wanted to.
    """
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            yield conn
        finally:
            await trans.rollback()


@pytest_asyncio.fixture
async def actors(db) -> dict:
    """The seeded demo users, keyed by role."""
    rows = await db.execute(
        text(
            """
            select p.id, p.role::text as role, p.employee_code
              from profiles p
             where p.employee_code in
                   ('EMP-G01','EMP-O01','EMP-O02','EMP-F01','EMP-I01','EMP-W01','EMP-A01',
                    'EMP-P01','EMP-M01')
            """
        )
    )
    by_code = {r["employee_code"]: r["id"] for r in rows.mappings()}

    if "EMP-G01" not in by_code:
        pytest.skip("Seed data not loaded — run `supabase db reset`")

    return {
        "guard": by_code["EMP-G01"],
        "ops": by_code["EMP-O01"],
        "ops_backup": by_code["EMP-O02"],
        "offloader": by_code["EMP-F01"],
        "inbound": by_code["EMP-I01"],
        "storeman": by_code["EMP-W01"],
        "admin": by_code["EMP-A01"],
        "packer": by_code["EMP-P01"],
        "matcher": by_code["EMP-M01"],
    }


@asynccontextmanager
async def rejected(conn: AsyncConnection, *, containing: Optional[str] = None):
    """Assert the database refuses what happens inside, and stay usable after.

    Postgres aborts the whole transaction on any error, so a test that simply
    wraps the statement in `pytest.raises` finds every later query failing with
    "current transaction is aborted" — including the ones checking that nothing
    changed. A savepoint scopes the rollback to just the rejected statement,
    which is also exactly how the API handles a rejected scan mid-batch.
    """
    with pytest.raises(DBAPIError) as err:
        async with conn.begin_nested():
            yield

    if containing is not None:
        assert containing in str(err.value), (
            f"expected the refusal to mention {containing!r}, got: {err.value}"
        )


async def act_as(conn: AsyncConnection, actor_id) -> None:
    """Make `auth.uid()` and the audit trail resolve to this user.

    This is the same mechanism the API uses (see app/db/session.py), so the
    tests exercise the real identity path rather than a test-only shortcut.
    """
    import json

    await conn.execute(
        text("select set_config('request.jwt.claims', :claims, true)"),
        {"claims": json.dumps({"sub": str(actor_id), "role": "authenticated"})},
    )
    await conn.execute(
        text("select set_config('app.actor_id', :uid, true)"), {"uid": str(actor_id)}
    )


@pytest_asyncio.fixture
async def gate_entry(db, actors):
    """An approved gate entry with a vehicle inside, linked to PO-2026-0001."""
    await act_as(db, actors["guard"])

    po = (
        await db.execute(
            text(
                """
                select po.id, po.vendor_id from purchase_orders po
                 where po.po_number = 'PO-2026-0001'
                """
            )
        )
    ).mappings().one()

    entry_id = (
        await db.execute(
            text(
                """
                insert into gate_entries
                  (status, vehicle_number, vendor_id, purchase_order_id, requested_by, requested_at)
                values ('pending_approval', :vehicle, :vendor, :po, :guard, now())
                returning id
                """
            ),
            {
                # Must be exactly 2 letters, 2 digits, 2 letters, 4 digits
                # (gate_entries_vehicle_number_check, 0027) — a hex suffix can
                # contain a-f, which a strict numeric suffix can't.
                "vehicle": f"KA01AB{uuid.uuid4().int % 10000:04d}",
                "vendor": po["vendor_id"],
                "po": po["id"],
                "guard": actors["guard"],
            },
        )
    ).scalar_one()

    await act_as(db, actors["ops"])
    await db.execute(
        text(
            """
            update gate_entries
               set status = 'approved', decided_by = :ops, decided_at = now()
             where id = :id
            """
        ),
        {"ops": actors["ops"], "id": entry_id},
    )

    await act_as(db, actors["guard"])
    await db.execute(
        text("update gate_entries set status = 'inside' where id = :id"), {"id": entry_id}
    )

    return {"id": entry_id, "po_id": po["id"], "vendor_id": po["vendor_id"]}
