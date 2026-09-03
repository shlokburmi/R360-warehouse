"""Carton stickers (migrations 0020-0021).

Outbound cartons get their own printed, Admin-issued QR sticker — a third
sticker family alongside box and unit — instead of relying solely on the
invoice number already printed outside this system. Out-scan and gate-exit
resolve the sticker first and fall back to the raw invoice number, the same
"QR first, human-readable text as fallback" shape every other sticker here
already has.
"""

import uuid

import pytest
from sqlalchemy import text

from app.core.errors import AppError
from app.services import stickers as sticker_service
from tests.conftest import act_as, rejected
from tests.test_packing import (  # noqa: F401 (people is a fixture)
    _batch_of,
    _new_invoice,
    _out_scan,
    _packed_invoices,
    people,
)

pytestmark = pytest.mark.asyncio


async def _carton_sticker(db, invoice_id, who):
    await act_as(db, who)
    return await sticker_service.issue_carton_sticker(db, invoice_id)


class TestCartonStickerIssue:
    async def test_admin_can_issue_a_carton_sticker(self, db, actors):
        inv = await _new_invoice(db)
        sticker = await _carton_sticker(db, inv["id"], actors["admin"])

        assert sticker["code"].startswith("CTN-")
        assert sticker["invoice_number"] == inv["invoice_number"]
        assert sticker["status"] == "applied"

    async def test_reissuing_voids_the_old_one(self, db, actors):
        inv = await _new_invoice(db)
        first = await _carton_sticker(db, inv["id"], actors["admin"])
        second = await _carton_sticker(db, inv["id"], actors["admin"])

        assert second["code"] != first["code"]

        old_status = (
            await db.execute(
                text("select status::text from stickers where id = :id"), {"id": first["id"]}
            )
        ).scalar_one()
        assert old_status == "void"

    async def test_unknown_invoice_is_refused(self, db, actors):
        await act_as(db, actors["admin"])
        with pytest.raises(AppError):
            await sticker_service.issue_carton_sticker(db, uuid.uuid4())


class TestFamilyShape:
    """The CHECK constraint 0021 adds, enforced even against a direct insert —
    not just against the service function that happens to shape rows
    correctly."""

    async def test_a_carton_sticker_cannot_carry_a_gate_entry(self, db, actors, gate_entry):
        async with rejected(db, containing="stickers_family_shape"):
            await db.execute(
                text(
                    """
                    insert into stickers (code, sticker_type, gate_entry_id, invoice_id, sequence_no)
                    values (:c, 'carton', :g, null, 1)
                    """
                ),
                {"c": f"CTN-{uuid.uuid4().hex[:8].upper()}", "g": gate_entry["id"]},
            )

    async def test_a_box_sticker_cannot_carry_an_invoice(self, db, actors, gate_entry):
        from tests.test_control_points import _issue_boxes

        await _issue_boxes(db, gate_entry["id"], actors)
        inv = await _new_invoice(db)
        async with rejected(db, containing="stickers_family_shape"):
            await db.execute(
                text(
                    """
                    insert into stickers
                      (code, sticker_type, gate_entry_id, sheet_id, invoice_id, sequence_no)
                    select :c, 'box', :g, ss.id, :inv, 999
                      from sticker_sheets ss
                     where ss.gate_entry_id = :g and ss.sticker_type = 'box' limit 1
                    """
                ),
                {"c": f"BOX-{uuid.uuid4().hex[:8].upper()}", "g": gate_entry["id"], "inv": inv["id"]},
            )

    async def test_only_one_live_carton_sticker_per_invoice(self, db, actors):
        inv = await _new_invoice(db)
        await _carton_sticker(db, inv["id"], actors["admin"])

        async with rejected(db):
            await db.execute(
                text(
                    "insert into stickers (code, sticker_type, invoice_id, sequence_no) "
                    "values (:c, 'carton', :inv, 1)"
                ),
                {"c": f"CTN-{uuid.uuid4().hex[:8].upper()}", "inv": inv["id"]},
            )


class TestResolution:
    """fn_scan_resolve's out_scan/gate_exit branch, extended by 0021."""

    async def test_out_scan_accepts_the_carton_sticker(self, db, actors, people):
        invoices = await _packed_invoices(db, people, count=1)
        inv = invoices[0]
        sticker = await _carton_sticker(db, inv["id"], actors["admin"])
        batch_id = await _batch_of(db, invoices, people)

        result = await _out_scan(db, sticker["code"], people["ops"]["id"])
        assert result["accepted"] is True
        assert str(result["invoice_id"]) == str(inv["id"])

        batch_status = (
            await db.execute(
                text("select status::text from batches where id = :id"), {"id": batch_id}
            )
        ).scalar_one()
        assert batch_status == "scanning"

    async def test_out_scan_still_falls_back_to_the_invoice_number(self, db, people):
        """No carton sticker exists for this invoice — the pre-0020 mechanism
        must keep working exactly as it did."""
        invoices = await _packed_invoices(db, people, count=1)
        inv = invoices[0]
        await _batch_of(db, invoices, people)

        result = await _out_scan(db, inv["invoice_number"], people["ops"]["id"])
        assert result["accepted"] is True

    async def test_a_box_sticker_code_is_refused_with_a_specific_reason(self, db, actors, gate_entry):
        """Scanning the wrong sticker family at the loading dock is a real
        mistake — the message must say so, not just 'unknown_code'."""
        from tests.test_control_points import _issue_boxes, _sticker_code

        boxes = await _issue_boxes(db, gate_entry["id"], actors)
        code = await _sticker_code(db, boxes[0])

        result = await _out_scan(db, code, actors["admin"])
        assert result["accepted"] is False
        assert result["reject_reason"] == "wrong_sticker_type"

    async def test_a_voided_carton_sticker_is_refused(self, db, actors):
        inv = await _new_invoice(db)
        sticker = await _carton_sticker(db, inv["id"], actors["admin"])
        await _carton_sticker(db, inv["id"], actors["admin"])  # reissue voids `sticker`

        result = await _out_scan(db, sticker["code"], actors["admin"])
        assert result["accepted"] is False
        assert result["reject_reason"] == "sticker_void"
