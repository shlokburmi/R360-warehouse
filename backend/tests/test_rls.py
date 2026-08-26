"""Row level security (PRD §8), exercised the way the API exercises it.

Each test assumes the `authenticated` database role and installs a user's JWT
claims, exactly as app/db/session.py does per request. Nothing here relies on
the API layer, because the guarantee being tested is that the API layer is not
the thing enforcing it.
"""

import json
import uuid

import pytest
from sqlalchemy import text

from tests.conftest import rejected
from tests.test_control_points import _issue_boxes, _sticker_code
from tests.test_packing import _new_invoice

pytestmark = pytest.mark.asyncio


async def as_authenticated(conn, actor_id):
    await conn.execute(text("set local role authenticated"))
    await conn.execute(
        text("select set_config('request.jwt.claims', :c, true)"),
        {"c": json.dumps({"sub": str(actor_id), "role": "authenticated"})},
    )


async def as_postgres(conn):
    await conn.execute(text("reset role"))


class TestRoleIsolation:
    async def test_guard_cannot_read_audit_log(self, db, actors):
        """PRD §8: a guard sees gate pages. The audit trail is not one of them."""
        await as_authenticated(db, actors["guard"])
        count = (await db.execute(text("select count(*) from audit_log"))).scalar_one()
        await as_postgres(db)

        assert count == 0, "RLS returns zero rows rather than raising"

    async def test_ops_can_read_audit_log(self, db, actors, gate_entry):
        await as_authenticated(db, actors["ops"])
        count = (await db.execute(text("select count(*) from audit_log"))).scalar_one()
        await as_postgres(db)

        assert count > 0

    async def test_offloader_cannot_read_visitor_records(self, db, actors):
        """Visitor identity data is gate and Ops only — DPDP Act minimisation."""
        await as_authenticated(db, actors["offloader"])
        count = (await db.execute(text("select count(*) from visitors"))).scalar_one()
        await as_postgres(db)

        assert count == 0

    async def test_guard_can_read_visitor_records(self, db, actors):
        await as_authenticated(db, actors["guard"])
        count = (await db.execute(text("select count(*) from visitors"))).scalar_one()
        await as_postgres(db)

        assert count > 0

    async def test_guard_cannot_issue_stickers(self, db, actors, gate_entry):
        """If the floor could print its own stickers, CONTROL POINT 2 would be
        comparing a number against itself."""
        await as_authenticated(db, actors["guard"])

        async with rejected(db, containing="row-level security"):
            await db.execute(
                text(
                    """
                    insert into sticker_sheets
                      (gate_entry_id, sticker_type, quantity, generated_by)
                    values (:e, 'box', 5, :who)
                    """
                ),
                {"e": gate_entry["id"], "who": actors["guard"]},
            )
        await as_postgres(db)

    async def test_offloader_cannot_scan_boxes_at_the_gate(self, db, actors, gate_entry):
        """Scan type is pinned to the role that performs it in the real process."""
        await as_authenticated(db, actors["offloader"])

        async with rejected(db, containing="row-level security"):
            await db.execute(
                text(
                    """
                    insert into scan_events
                      (client_event_id, scan_type, raw_code, accepted, scanned_by, scanned_at)
                    values (:cid, 'box_verify', 'BOX-TEST0001', false, :who, now())
                    """
                ),
                {"cid": str(uuid.uuid4()), "who": actors["offloader"]},
            )
        await as_postgres(db)

    async def test_guard_cannot_scan_boxes_at_the_gate(self, db, actors, gate_entry):
        """Sticker scanning at intake moved to packers (DECISIONS.md §CG6) —
        the guard only declares the count, and no longer scans stickers."""
        await as_authenticated(db, actors["guard"])

        async with rejected(db, containing="row-level security"):
            await db.execute(
                text(
                    """
                    insert into scan_events
                      (client_event_id, scan_type, raw_code, accepted, scanned_by, scanned_at)
                    values (:cid, 'box_verify', 'BOX-TEST0004', false, :who, now())
                    """
                ),
                {"cid": str(uuid.uuid4()), "who": actors["guard"]},
            )
        await as_postgres(db)

    async def test_packer_can_scan_a_box_sticker_and_it_actually_updates(
        self, db, actors, gate_entry
    ):
        """Positive path for §CG6: packers scan both sticker types at intake.

        Checked past the insert succeeding, because a forbidden UPDATE inside
        the resolving trigger fails *silently* rather than with an error — RLS
        turns it into a no-op, not a refusal (DECISIONS.md Part D). If packer
        were missing from `boxes_update`, this insert would still say
        `accepted: true` while the box row never actually moved to 'verified'.
        """
        boxes = await _issue_boxes(db, gate_entry["id"], actors)
        box_id = boxes[0]
        code = await _sticker_code(db, box_id)

        await as_authenticated(db, actors["packer"])
        accepted = (
            await db.execute(
                text(
                    """
                    insert into scan_events
                      (client_event_id, scan_type, raw_code, accepted, scanned_by, scanned_at)
                    values (:cid, 'box_verify', :code, false, :who, now())
                    returning accepted
                    """
                ),
                {"cid": str(uuid.uuid4()), "code": code, "who": actors["packer"]},
            )
        ).scalar_one()
        await as_postgres(db)

        assert accepted is True

        box_status = (
            await db.execute(
                text("select status::text from boxes where id = :id"), {"id": box_id}
            )
        ).scalar_one()
        assert box_status == "verified", "boxes_update must permit packer, or this stays 'pending'"

    async def test_nobody_can_scan_as_someone_else(self, db, actors, gate_entry):
        """Attribution is the whole point of the ledger. `scanned_by` must be you.

        The actor here (packer) is otherwise permitted to scan box_verify, so
        the only thing that can make this fail is the impersonation itself.
        """
        await as_authenticated(db, actors["packer"])

        async with rejected(db):
            await db.execute(
                text(
                    """
                    insert into scan_events
                      (client_event_id, scan_type, raw_code, accepted, scanned_by, scanned_at)
                    values (:cid, 'box_verify', 'BOX-TEST0002', false, :other, now())
                    """
                ),
                {"cid": str(uuid.uuid4()), "other": actors["ops"]},
            )
        await as_postgres(db)

    async def test_offloader_cannot_resolve_exceptions(self, db, actors, gate_entry):
        await as_postgres(db)
        exc_id = (
            await db.execute(
                text(
                    """
                    insert into exceptions
                      (exception_type, gate_entry_id, title, reported_by)
                    values ('other', :e, 'Test exception', :who)
                    returning id
                    """
                ),
                {"e": gate_entry["id"], "who": actors["offloader"]},
            )
        ).scalar_one()

        await as_authenticated(db, actors["offloader"])
        result = await db.execute(
            text(
                """
                update exceptions
                   set status = 'resolved', resolution = 'accept',
                       resolution_note = 'trying to self-resolve',
                       resolved_by = :who, resolved_at = now()
                 where id = :id
                """
            ),
            {"who": actors["offloader"], "id": exc_id},
        )
        await as_postgres(db)

        assert result.rowcount == 0, "the USING clause hides the row from a non-Ops role"

        status = (
            await db.execute(
                text("select status::text from exceptions where id = :id"), {"id": exc_id}
            )
        ).scalar_one()
        assert status == "open"

    async def test_user_cannot_promote_themselves(self, db, actors):
        """A guard who could set their own role to admin would defeat CP1.

        The self-update policy lets someone edit their own row (name, mobile) but
        pins `role` to its current value in the WITH CHECK, so this is refused
        outright rather than silently updating nothing.
        """
        await as_authenticated(db, actors["guard"])

        async with rejected(db, containing="row-level security"):
            await db.execute(
                text("update profiles set role = 'admin' where id = :id"),
                {"id": actors["guard"]},
            )
        await as_postgres(db)

        role = (
            await db.execute(
                text("select role::text from profiles where id = :id"), {"id": actors["guard"]}
            )
        ).scalar_one()
        assert role == "security_guard"

    async def test_nobody_can_delete_through_the_authenticated_role(self, db, actors, gate_entry):
        await as_authenticated(db, actors["admin"])

        async with rejected(db):
            await db.execute(text("delete from scan_events where id is not null"))
        await as_postgres(db)


class TestCartonStickerRLS:
    """0020/0021: a carton sticker is issued through the same `stickers_insert`
    policy (0005) as every other sticker — is_ops() only."""

    async def test_packer_cannot_issue_a_carton_sticker(self, db, actors):
        inv = await _new_invoice(db)

        await as_authenticated(db, actors["packer"])
        async with rejected(db, containing="row-level security"):
            await db.execute(
                text(
                    "insert into stickers (code, sticker_type, invoice_id, sequence_no) "
                    "values (:c, 'carton', :inv, 1)"
                ),
                {"c": f"CTN-{uuid.uuid4().hex[:8].upper()}", "inv": inv["id"]},
            )
        await as_postgres(db)

    async def test_admin_can_issue_a_carton_sticker(self, db, actors):
        inv = await _new_invoice(db)

        await as_authenticated(db, actors["admin"])
        code = f"CTN-{uuid.uuid4().hex[:8].upper()}"
        await db.execute(
            text(
                "insert into stickers (code, sticker_type, invoice_id, sequence_no) "
                "values (:c, 'carton', :inv, 1)"
            ),
            {"c": code, "inv": inv["id"]},
        )
        await as_postgres(db)

        stored = (
            await db.execute(text("select code from stickers where invoice_id = :inv"), {"inv": inv["id"]})
        ).scalar_one()
        assert stored == code
