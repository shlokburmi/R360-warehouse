"""Identity photo retention (PRD §8, DPDP Act 2023).

0006_storage.sql deferred retention to "a scheduled job" and there was no such
job, so these tests exist as much to pin the *policy* as the code: what gets
destroyed, what is deliberately kept, and what survives the destruction so that
the policy can be shown to have run.

The last of those is the one that is easy to get wrong. A retention job that
leaves no trace is indistinguishable from one that never ran, and "nothing
happened" is the failure mode of every deletion policy — because unlike almost
every other bug, not deleting breaks nothing.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.services import retention as retention_service
from tests.conftest import act_as, rejected

pytestmark = pytest.mark.asyncio

RETENTION_DAYS = 180


async def _visitor(db, *, age_days: int | None, blocked: bool = False, mobile_suffix: str):
    """A visitor with a photo captured `age_days` ago, or no photo at all."""
    captured = (
        None
        if age_days is None
        else datetime.now(timezone.utc) - timedelta(days=age_days)
    )
    row = (
        await db.execute(
            text(
                """
                insert into visitors
                  (mobile, full_name, id_photo_path, id_photo_captured_at, is_blocked)
                values (:mobile, :name, :path, :captured, :blocked)
                returning id, id_photo_path
                """
            ),
            {
                "mobile": f"98765{mobile_suffix}",
                "name": f"Visitor {mobile_suffix}",
                "path": None if captured is None else f"98765{mobile_suffix}/photo.jpg",
                "captured": captured,
                "blocked": blocked,
            },
        )
    ).mappings().one()
    return dict(row)


class TestWhatExpires:
    async def test_a_photo_past_the_window_is_a_candidate(self, db, actors):
        await act_as(db, actors["guard"])
        old = await _visitor(db, age_days=200, mobile_suffix="11111")
        fresh = await _visitor(db, age_days=10, mobile_suffix="22222")

        candidates = await retention_service.expired_photos(db, RETENTION_DAYS)
        ids = {str(c["id"]) for c in candidates}

        assert str(old["id"]) in ids
        assert str(fresh["id"]) not in ids

    async def test_the_threshold_is_the_revalidation_age(self, db, actors):
        """DECISIONS.md §2 forces a re-capture at 180 days because the photo
        verifies nothing by then. Retention uses the same number rather than a
        second one, so there is no window in which a photo is both useless and
        retained."""
        await act_as(db, actors["guard"])
        just_inside = await _visitor(db, age_days=RETENTION_DAYS - 1, mobile_suffix="33333")
        just_outside = await _visitor(db, age_days=RETENTION_DAYS + 1, mobile_suffix="44444")

        ids = {str(c["id"]) for c in await retention_service.expired_photos(db, RETENTION_DAYS)}

        assert str(just_inside["id"]) not in ids
        assert str(just_outside["id"]) in ids

    async def test_a_blocked_visitors_photo_is_kept(self, db, actors):
        """Purpose limitation working in both directions, not an exception to
        it. A block is enforced on the mobile number and a number is trivially
        borrowed, so the photo is still doing the job it was captured for."""
        await act_as(db, actors["guard"])
        blocked = await _visitor(db, age_days=900, blocked=True, mobile_suffix="55555")

        ids = {str(c["id"]) for c in await retention_service.expired_photos(db, RETENTION_DAYS)}

        assert str(blocked["id"]) not in ids

    async def test_purging_a_blocked_visitor_is_refused_by_the_database(self, db, actors):
        """Enforced in the function, not only in the sweep's WHERE clause, so a
        second caller cannot quietly bypass it."""
        await act_as(db, actors["guard"])
        blocked = await _visitor(db, age_days=900, blocked=True, mobile_suffix="66666")

        async with rejected(db, containing="blocked"):
            await db.execute(
                text("select purge_identity_photo(cast(:id as uuid), 'retention')"),
                {"id": blocked["id"]},
            )


class TestWhatSurvivesTheDestruction:
    async def test_the_photo_is_gone_and_the_fact_remains(self, db, actors):
        await act_as(db, actors["guard"])
        visitor = await _visitor(db, age_days=400, mobile_suffix="77777")

        purged = (
            await db.execute(
                text("select purge_identity_photo(cast(:id as uuid), 'retention')"),
                {"id": visitor["id"]},
            )
        ).scalar_one()

        row = (
            await db.execute(
                text(
                    """
                    select id_photo_path, id_photo_captured_at, id_photo_purged_at,
                           id_photo_purge_reason, id_photo_purged_age_days
                      from visitors where id = :id
                    """
                ),
                {"id": visitor["id"]},
            )
        ).mappings().one()

        assert purged is True
        assert row["id_photo_path"] is None
        # Cleared because visitors_photo_consistent requires the pair to travel
        # together — and because a visitor with no capture date is treated as
        # needing a fresh photo, which is what "expired" means.
        assert row["id_photo_captured_at"] is None
        # PRD §7: the bytes are the one thing that genuinely goes. The record
        # that they existed does not.
        assert row["id_photo_purged_at"] is not None
        assert row["id_photo_purge_reason"] == "retention"
        assert row["id_photo_purged_age_days"] >= 399

    async def test_purging_twice_is_a_no_op_not_an_error(self, db, actors):
        """The sweep deletes the object before clearing the row, so a crash
        between the two leaves work that the next sweep must be able to retry."""
        await act_as(db, actors["guard"])
        visitor = await _visitor(db, age_days=400, mobile_suffix="88888")

        first = (
            await db.execute(
                text("select purge_identity_photo(cast(:id as uuid), 'retention')"),
                {"id": visitor["id"]},
            )
        ).scalar_one()
        second = (
            await db.execute(
                text("select purge_identity_photo(cast(:id as uuid), 'retention')"),
                {"id": visitor["id"]},
            )
        ).scalar_one()

        assert first is True
        assert second is False

    async def test_a_purged_visitor_is_not_a_candidate_again(self, db, actors):
        await act_as(db, actors["guard"])
        visitor = await _visitor(db, age_days=400, mobile_suffix="99999")
        await db.execute(
            text("select purge_identity_photo(cast(:id as uuid), 'retention')"),
            {"id": visitor["id"]},
        )

        ids = {str(c["id"]) for c in await retention_service.expired_photos(db, RETENTION_DAYS)}
        assert str(visitor["id"]) not in ids


class TestProvingItRan:
    async def test_overdue_counts_what_the_sweep_has_not_reached(self, db, actors):
        """`overdue` staying above zero is the only signal that the job has
        stopped. Nothing else surfaces it, because not deleting breaks nothing."""
        await act_as(db, actors["guard"])
        before = (
            await db.execute(
                text("select overdue from photo_retention_status(:d)"), {"d": RETENTION_DAYS}
            )
        ).scalar_one()

        visitor = await _visitor(db, age_days=400, mobile_suffix="10101")

        during = (
            await db.execute(
                text("select overdue from photo_retention_status(:d)"), {"d": RETENTION_DAYS}
            )
        ).scalar_one()

        await db.execute(
            text("select purge_identity_photo(cast(:id as uuid), 'retention')"),
            {"id": visitor["id"]},
        )

        after = (
            await db.execute(
                text("select overdue from photo_retention_status(:d)"), {"d": RETENTION_DAYS}
            )
        ).scalar_one()

        assert during == before + 1
        assert after == before

    async def test_blocked_photos_are_reported_separately_not_as_overdue(self, db, actors):
        """Otherwise a deliberately retained photo looks like a compliance
        breach, and the number an auditor reads stops meaning anything."""
        await act_as(db, actors["guard"])
        await _visitor(db, age_days=900, blocked=True, mobile_suffix="12121")

        row = (
            await db.execute(
                text(
                    """
                    select overdue, retained_for_block
                      from photo_retention_status(:d)
                    """
                ),
                {"d": RETENTION_DAYS},
            )
        ).mappings().one()

        assert row["retained_for_block"] >= 1
        assert row["overdue"] == 0

    async def test_the_status_says_whether_the_job_can_run_at_all(self, db, actors):
        """A backlog because the worker is behind and a backlog because it was
        never configured need different fixes, so they are different fields."""
        from app.core.config import Settings

        await act_as(db, actors["admin"])
        unconfigured = Settings(supabase_service_role_key="")
        status = await retention_service.retention_status(db, unconfigured)

        assert status["enabled"] is False
        assert status["retention_days"] == unconfigured.id_photo_revalidation_days

    async def test_the_sweep_refuses_to_run_silently_unconfigured(self, db, actors):
        from app.core.config import Settings

        await act_as(db, actors["guard"])
        await _visitor(db, age_days=400, mobile_suffix="13131")

        result = await retention_service.purge_expired_photos(
            db, Settings(supabase_service_role_key="")
        )

        assert result["unconfigured"] == 1
        assert result["purged"] == 0


class TestStorageStaysLockedDown:
    async def test_nobody_can_delete_an_identity_photo_through_rls(self, db, actors):
        """0006 creates no DELETE policy on storage.objects, so the purge is
        worker-only. If this ever passes, a guard can destroy evidence."""
        policies = (
            await db.execute(
                text(
                    """
                    select count(*)::int from pg_policies
                     where schemaname = 'storage' and tablename = 'objects'
                       and cmd = 'DELETE'
                    """
                )
            )
        ).scalar_one()

        assert policies == 0
