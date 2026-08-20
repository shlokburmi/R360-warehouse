"""Order No capture from the delivery challan — OCR provenance and refusals.

The failures these prevent:
  * a misread order number reaching the invoice because it looked plausible
  * a second challan silently overwriting the order an invoice is booked against
  * an OCR failure vanishing, so a station reading badly all morning looks fine
  * a bad read being edited out of the log after the fact
"""

import pytest
from sqlalchemy import text

from app.core.errors import AppError
from app.services import packing as packing_service
from tests.conftest import act_as, rejected

pytestmark = pytest.mark.asyncio

# The Order No from the sample challan (DC SC0629159).
GOOD = "CP002458380_0001"


@pytest.fixture
async def matcher(db):
    row = (
        await db.execute(
            text(
                """
                select id, badge_code from profiles
                 where employee_code = 'EMP-M01'
                """
            )
        )
    ).mappings().first()
    if row is None:
        pytest.skip("Seed data not loaded")
    return dict(row)


async def _an_invoice(db):
    """Any open, unverified invoice. Order No capture does not care which."""
    row = (
        await db.execute(
            text(
                """
                select invoice_number from v_invoice_status
                 where is_open and verified_at is null and order_no is null
                 limit 1
                """
            )
        )
    ).mappings().first()
    if row is None:
        pytest.skip("No open unverified invoice in the seed data")
    return row["invoice_number"]


async def _scans(db, invoice_number):
    rows = await db.execute(
        text(
            """
            select s.parsed_order_no, s.raw_text, s.confidence, s.source, s.was_corrected
              from order_no_scans s
              join invoices i on i.id = s.invoice_id
             where i.invoice_number = :num
             order by s.scanned_at
            """
        ),
        {"num": invoice_number},
    )
    return [dict(r) for r in rows.mappings()]


class TestCapture:
    async def test_a_good_read_attaches_and_is_logged(self, db, matcher):
        invoice_number = await _an_invoice(db)
        await act_as(db, matcher["id"])

        result = await packing_service.record_order_no(
            db,
            invoice_number=invoice_number,
            order_no=GOOD,
            source="ocr",
            actor_id=str(matcher["id"]),
            raw_text=f"Order No : {GOOD}",
            confidence=91.5,
        )

        assert result["recorded"] is True
        assert result["invoice"]["order_no"] == GOOD

        scans = await _scans(db, invoice_number)
        assert len(scans) == 1
        assert scans[0]["parsed_order_no"] == GOOD
        assert scans[0]["source"] == "ocr"
        assert scans[0]["was_corrected"] is False

    async def test_lowercase_is_normalised_not_rejected(self, db, matcher):
        invoice_number = await _an_invoice(db)
        await act_as(db, matcher["id"])

        result = await packing_service.record_order_no(
            db,
            invoice_number=invoice_number,
            order_no=GOOD.lower(),
            source="manual",
            actor_id=str(matcher["id"]),
        )
        assert result["invoice"]["order_no"] == GOOD

    async def test_a_correction_is_recorded_as_one(self, db, matcher):
        """The rate of this column is the honest measure of whether OCR helps."""
        invoice_number = await _an_invoice(db)
        await act_as(db, matcher["id"])

        await packing_service.record_order_no(
            db,
            invoice_number=invoice_number,
            order_no=GOOD,
            source="ocr",
            actor_id=str(matcher["id"]),
            raw_text="Order No : CP0O2458380_0001",
            confidence=54.0,
            was_corrected=True,
        )

        scans = await _scans(db, invoice_number)
        assert scans[0]["was_corrected"] is True
        # The raw text keeps the misread that was corrected away, which is the
        # only reason the correction can be explained later.
        assert "CP0O2458380_0001" in scans[0]["raw_text"]


class TestFailedReads:
    async def test_a_miss_is_logged_and_leaves_the_invoice_alone(self, db, matcher):
        invoice_number = await _an_invoice(db)
        await act_as(db, matcher["id"])

        result = await packing_service.record_order_no(
            db,
            invoice_number=invoice_number,
            order_no=None,
            source="ocr",
            actor_id=str(matcher["id"]),
            raw_text="Ordar Ne  CPOO24S838O OOO1",
            confidence=22.0,
        )

        assert result["recorded"] is False
        assert result["invoice"]["order_no"] is None

        scans = await _scans(db, invoice_number)
        assert len(scans) == 1
        assert scans[0]["parsed_order_no"] is None

    async def test_a_miss_does_not_erase_a_value_already_captured(self, db, matcher):
        invoice_number = await _an_invoice(db)
        await act_as(db, matcher["id"])

        await packing_service.record_order_no(
            db,
            invoice_number=invoice_number,
            order_no=GOOD,
            source="ocr",
            actor_id=str(matcher["id"]),
            confidence=95.0,
        )
        await packing_service.record_order_no(
            db,
            invoice_number=invoice_number,
            order_no=None,
            source="ocr",
            actor_id=str(matcher["id"]),
            raw_text="blur",
        )

        after = await packing_service.get_invoice_by_number(db, invoice_number)
        assert after["order_no"] == GOOD
        assert len(await _scans(db, invoice_number)) == 2


class TestRefusals:
    @pytest.mark.parametrize(
        "bad",
        [
            "CP0O2458380_0001",   # letter O substituted for zero
            "CP0024S8380_0001",   # S for 5
            "SC002458380_0001",   # the DC No mistaken for the Order No
            "CP002458380-0001",   # hyphen for underscore
            "CP002458380_001",    # suffix a digit short
            "CP0024583800_0001",  # a digit too many
        ],
    )
    async def test_a_malformed_order_no_is_refused(self, db, matcher, bad):
        invoice_number = await _an_invoice(db)
        await act_as(db, matcher["id"])

        with pytest.raises(AppError) as err:
            await packing_service.record_order_no(
                db,
                invoice_number=invoice_number,
                order_no=bad,
                source="ocr",
                actor_id=str(matcher["id"]),
            )
        assert err.value.code == "bad_order_no"

    async def test_a_different_order_no_is_a_conflict_not_an_overwrite(self, db, matcher):
        invoice_number = await _an_invoice(db)
        await act_as(db, matcher["id"])

        await packing_service.record_order_no(
            db,
            invoice_number=invoice_number,
            order_no=GOOD,
            source="ocr",
            actor_id=str(matcher["id"]),
            confidence=95.0,
        )

        with pytest.raises(AppError) as err:
            await packing_service.record_order_no(
                db,
                invoice_number=invoice_number,
                order_no="CP009999999_0002",
                source="ocr",
                actor_id=str(matcher["id"]),
                confidence=95.0,
            )
        assert err.value.code == "order_no_conflict"

        after = await packing_service.get_invoice_by_number(db, invoice_number)
        assert after["order_no"] == GOOD

    async def test_re_reading_the_same_order_no_is_allowed(self, db, matcher):
        """A matcher re-scanning the same challan is not an error."""
        invoice_number = await _an_invoice(db)
        await act_as(db, matcher["id"])

        for _ in range(2):
            await packing_service.record_order_no(
                db,
                invoice_number=invoice_number,
                order_no=GOOD,
                source="ocr",
                actor_id=str(matcher["id"]),
                confidence=88.0,
            )

        assert len(await _scans(db, invoice_number)) == 2


class TestTheLogIsEvidence:
    async def test_the_database_refuses_a_malformed_value_directly(self, db, matcher):
        """Belt and braces: the service validates, and so does the table.

        The check constraint is what protects the column from anything that
        reaches it without going through `record_order_no`.
        """
        invoice_number = await _an_invoice(db)
        await act_as(db, matcher["id"])

        async with rejected(db, containing="invoices_order_no_format"):
            await db.execute(
                text("update invoices set order_no = :bad where invoice_number = :num"),
                {"bad": "CP0O2458380_0001", "num": invoice_number},
            )

    async def test_a_scan_cannot_be_edited_after_the_fact(self, db, matcher):
        invoice_number = await _an_invoice(db)
        await act_as(db, matcher["id"])

        await packing_service.record_order_no(
            db,
            invoice_number=invoice_number,
            order_no=GOOD,
            source="ocr",
            actor_id=str(matcher["id"]),
            raw_text="Order No : CP002458380_0001",
            confidence=91.0,
        )

        async with rejected(db, containing="append-only"):
            await db.execute(
                text("update order_no_scans set confidence = 100 where parsed_order_no = :n"),
                {"n": GOOD},
            )

    async def test_a_scan_cannot_be_deleted(self, db, matcher):
        invoice_number = await _an_invoice(db)
        await act_as(db, matcher["id"])

        await packing_service.record_order_no(
            db,
            invoice_number=invoice_number,
            order_no=GOOD,
            source="ocr",
            actor_id=str(matcher["id"]),
        )

        async with rejected(db):
            await db.execute(
                text("delete from order_no_scans where parsed_order_no = :n"), {"n": GOOD}
            )


class TestFindingAnInvoiceFromTheChallan:
    """The reverse direction: OCR read an Order No, which invoice is it?

    This is what the file-upload path on the matching screen depends on. Note that
    the lookup is also the *validation* of the OCR read — a misread cannot resolve
    to the wrong invoice, only to none — so these refusals matter more than usual.
    """

    async def test_a_captured_order_no_finds_its_invoice(self, db, matcher):
        invoice_number = await _an_invoice(db)
        await act_as(db, matcher["id"])

        await packing_service.record_order_no(
            db,
            invoice_number=invoice_number,
            order_no=GOOD,
            source="ocr",
            actor_id=str(matcher["id"]),
            confidence=90.0,
        )

        found = await packing_service.get_invoice_by_order_no(db, GOOD)
        assert found["invoice_number"] == invoice_number
        assert found["order_no"] == GOOD

    async def test_lowercase_from_a_read_still_resolves(self, db, matcher):
        invoice_number = await _an_invoice(db)
        await act_as(db, matcher["id"])
        await packing_service.record_order_no(
            db,
            invoice_number=invoice_number,
            order_no=GOOD,
            source="ocr",
            actor_id=str(matcher["id"]),
        )
        found = await packing_service.get_invoice_by_order_no(db, GOOD.lower())
        assert found["invoice_number"] == invoice_number

    async def test_an_uncaptured_order_no_is_a_clean_404(self, db, matcher):
        """The ordinary case for a challan nobody has read yet — not an error state."""
        await act_as(db, matcher["id"])
        with pytest.raises(AppError) as err:
            await packing_service.get_invoice_by_order_no(db, "CP999999999_9999")
        assert err.value.code == "unknown_order_no"
        assert err.value.http_status == 404

    @pytest.mark.parametrize(
        "misread",
        ["CP0O2458380_0001", "CP0024S8380_0001", "SC002458380_0001", "CP002458380-0001"],
    )
    async def test_a_misread_is_refused_before_it_reaches_the_database(
        self, db, matcher, misread
    ):
        """This is why the upload path needs no human confirmation step.

        A misread cannot resolve to a different real invoice. It either fails the
        format check here or matches nothing at all.
        """
        await act_as(db, matcher["id"])
        with pytest.raises(AppError) as err:
            await packing_service.get_invoice_by_order_no(db, misread)
        assert err.value.code == "bad_order_no"

    async def test_two_invoices_on_one_order_refuse_to_guess(self, db, matcher):
        """0015 deliberately left order_no non-unique, so this is reachable."""
        await act_as(db, matcher["id"])
        rows = await db.execute(
            text(
                """
                select invoice_number from v_invoice_status
                 where is_open and verified_at is null and order_no is null
                 limit 2
                """
            )
        )
        numbers = [r["invoice_number"] for r in rows.mappings()]
        if len(numbers) < 2:
            pytest.skip("Need two open unverified invoices")

        for number in numbers:
            await packing_service.record_order_no(
                db,
                invoice_number=number,
                order_no=GOOD,
                source="manual",
                actor_id=str(matcher["id"]),
            )

        with pytest.raises(AppError) as err:
            await packing_service.get_invoice_by_order_no(db, GOOD)
        assert err.value.code == "ambiguous_order_no"
