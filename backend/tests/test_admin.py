"""Admin provisioning and attribution badges.

The invariant under test throughout is the one from DECISIONS.md §CC2: **no
route through this system tells anyone the badge code of a badge that is
currently in someone's pocket.** Issuing returns a code, because it has just
minted one; nothing reads one back.

That invariant is what makes CONTROL POINT 5 a two-person rule rather than a
two-scan rule, so the tests that matter most here are the ones asserting the
absence of a read path — including the one through the audit log, which is
where it was actually leaking.
"""

import json
import uuid

import pytest
from sqlalchemy import text

from app.core.errors import AppError
from app.schemas.admin import StaffUpdate
from app.services import admin as admin_service
from tests.conftest import act_as, rejected

pytestmark = pytest.mark.asyncio


async def as_authenticated(conn, actor_id):
    """Assume the caller's identity exactly as app/db/session.py does.

    Without this the tests run as the table owner, RLS is bypassed, and a
    missing policy passes — the failure mode described in DECISIONS.md Part D.
    """
    await conn.execute(text("set local role authenticated"))
    await conn.execute(
        text("select set_config('request.jwt.claims', :c, true)"),
        {"c": json.dumps({"sub": str(actor_id), "role": "authenticated"})},
    )
    await conn.execute(
        text("select set_config('app.actor_id', :uid, true)"), {"uid": str(actor_id)}
    )


async def as_postgres(conn):
    await conn.execute(text("reset role"))


@pytest.fixture
async def badge_holders(db):
    rows = await db.execute(
        text(
            """
            select employee_code, id, badge_code, role::text as role
              from profiles
             where employee_code in ('EMP-P01', 'EMP-M01', 'EMP-G01', 'EMP-O01', 'EMP-A01')
            """
        )
    )
    by_code = {r["employee_code"]: dict(r) for r in rows.mappings()}
    if "EMP-P01" not in by_code:
        pytest.skip("Seed data not loaded — run `supabase db reset`")
    return {
        "packer": by_code["EMP-P01"],
        "matcher": by_code["EMP-M01"],
        "guard": by_code["EMP-G01"],
        "ops": by_code["EMP-O01"],
        "admin": by_code["EMP-A01"],
    }


class TestBadgeCodesAreUnreadable:
    """The read paths that must not exist. One of these used to."""

    async def test_ops_cannot_select_the_badge_column(self, db, badge_holders):
        """§CC2: the column grant. The visible door."""
        await as_authenticated(db, badge_holders["ops"]["id"])
        async with rejected(db, containing="badge_code"):
            await db.execute(text("select badge_code from profiles"))
        await as_postgres(db)

    async def test_ops_cannot_read_a_badge_out_of_the_audit_trail(
        self, db, badge_holders
    ):
        """§CC2 again, through the window beside the door.

        fn_audit stores the whole row as JSONB and `audit_read` lets anyone
        passing is_ops() read it, so before 0013 an Ops Manager could lift any
        packer's badge code out of the trail — and an Ops Manager carries a
        badge of their own, which is the exact pair CONTROL POINT 5 forbids.
        """
        await act_as(db, badge_holders["admin"]["id"])
        await db.execute(
            text("update profiles set badge_code = generate_badge_code() where id = :id"),
            {"id": badge_holders["packer"]["id"]},
        )

        await as_authenticated(db, badge_holders["ops"]["id"])
        rows = await db.execute(
            text(
                """
                select before_data ->> 'badge_code' as before_badge,
                       after_data  ->> 'badge_code' as after_badge,
                       changed_keys
                  from audit_log
                 where table_name = 'profiles' and record_id = :id
                 order by occurred_at desc limit 1
                """
            ),
            {"id": badge_holders["packer"]["id"]},
        )
        row = rows.mappings().one()
        await as_postgres(db)

        assert row["after_badge"] == "[redacted]"
        assert row["before_badge"] == "[redacted]"
        # Redacting the value must not redact the fact. "This person's badge was
        # reissued, by that Admin, at that time" is what the trail is for.
        assert "badge_code" in row["changed_keys"]

    async def test_a_null_badge_stays_null_rather_than_redacted(self, db, badge_holders):
        """"Did they have a badge at all?" is a legitimate audit question."""
        await act_as(db, badge_holders["admin"]["id"])
        await db.execute(
            text("update profiles set full_name = 'Sanjeev Kumar Jr' where id = :id"),
            {"id": badge_holders["guard"]["id"]},
        )

        row = (
            await db.execute(
                text(
                    """
                    select after_data ? 'badge_code' as has_key,
                           after_data ->> 'badge_code' as badge
                      from audit_log
                     where table_name = 'profiles' and record_id = :id
                     order by occurred_at desc limit 1
                    """
                ),
                {"id": badge_holders["guard"]["id"]},
            )
        ).mappings().one()

        assert row["has_key"] is True
        assert row["badge"] is None

    async def test_the_staff_directory_has_no_badge_column(self, db, badge_holders):
        """The Admin screen shows whether a badge exists, never what it is."""
        await as_authenticated(db, badge_holders["admin"]["id"])
        columns = {
            r[0]
            for r in await db.execute(
                text(
                    """
                    select column_name from information_schema.columns
                     where table_name = 'v_staff_directory'
                    """
                )
            )
        }
        await as_postgres(db)

        assert "badge_code" not in columns
        assert {"has_badge", "badge_active", "badge_usable"} <= columns


class TestIssuingABadge:
    async def test_only_an_admin_can_issue(self, db, badge_holders):
        """An Ops Manager who could issue a badge could issue themselves a
        packer's — one person on both halves of CONTROL POINT 5."""
        await as_authenticated(db, badge_holders["ops"]["id"])
        async with rejected(db, containing="Only an Admin"):
            await db.execute(
                text("select admin_issue_badge(cast(:id as uuid))"),
                {"id": badge_holders["packer"]["id"]},
            )
        await as_postgres(db)

    async def test_issuing_replaces_the_old_code(self, db, badge_holders):
        """A reissue kills the badge it replaces, which is what makes "lost
        badge" a safe operation rather than a standing risk."""
        old_code = badge_holders["packer"]["badge_code"]

        await as_authenticated(db, badge_holders["admin"]["id"])
        new_code = (
            await db.execute(
                text("select admin_issue_badge(cast(:id as uuid))"),
                {"id": badge_holders["packer"]["id"]},
            )
        ).scalar_one()

        found_old = (
            await db.execute(
                text("select count(*)::int from resolve_badge_holder(:c)"), {"c": old_code}
            )
        ).scalar_one()
        holder = (
            await db.execute(
                text("select id, badge_active from resolve_badge_holder(:c)"),
                {"c": new_code},
            )
        ).mappings().one()
        await as_postgres(db)

        assert new_code != old_code
        assert new_code.startswith("BDG-")
        assert found_old == 0, "the replaced badge must stop resolving"
        assert str(holder["id"]) == str(badge_holders["packer"]["id"])
        assert holder["badge_active"] is True

    async def test_a_guard_cannot_be_given_a_badge(self, db, badge_holders):
        """A badge is attribution for work at a station that scans one. On a
        guard it would attribute nothing."""
        await as_authenticated(db, badge_holders["admin"]["id"])
        async with rejected(db, containing="does not carry an attribution badge"):
            await db.execute(
                text("select admin_issue_badge(cast(:id as uuid))"),
                {"id": badge_holders["guard"]["id"]},
            )
        await as_postgres(db)

    async def test_a_deactivated_account_cannot_be_given_a_badge(self, db, badge_holders):
        await act_as(db, badge_holders["admin"]["id"])
        await db.execute(
            text("update profiles set is_active = false where id = :id"),
            {"id": badge_holders["packer"]["id"]},
        )

        await as_authenticated(db, badge_holders["admin"]["id"])
        async with rejected(db, containing="deactivated"):
            await db.execute(
                text("select admin_issue_badge(cast(:id as uuid))"),
                {"id": badge_holders["packer"]["id"]},
            )
        await as_postgres(db)

    async def test_revoking_leaves_the_person_identifiable(self, db, badge_holders):
        """The code is kept, not nulled: past packing records point at the
        person, and PRD §7 supersedes rather than erases."""
        await as_authenticated(db, badge_holders["admin"]["id"])
        await db.execute(
            text("select admin_revoke_badge(cast(:id as uuid))"),
            {"id": badge_holders["packer"]["id"]},
        )
        holder = (
            await db.execute(
                text("select badge_active from resolve_badge_holder(:c)"),
                {"c": badge_holders["packer"]["badge_code"]},
            )
        ).mappings().one()
        await as_postgres(db)

        # Still resolvable, so the station can say "that badge was withdrawn"
        # instead of the unhelpful "not recognised".
        assert holder["badge_active"] is False

    async def test_only_an_admin_can_revoke(self, db, badge_holders):
        await as_authenticated(db, badge_holders["ops"]["id"])
        async with rejected(db, containing="Only an Admin"):
            await db.execute(
                text("select admin_revoke_badge(cast(:id as uuid))"),
                {"id": badge_holders["packer"]["id"]},
            )
        await as_postgres(db)


class TestAccountChanges:
    """The service-layer guards. These are usability rules rather than
    integrity ones — a second Admin is allowed to do any of them — so they live
    in Python, and the tests call the service directly."""

    async def test_an_admin_cannot_deactivate_themselves(self, db, badge_holders):
        admin_id = badge_holders["admin"]["id"]
        await act_as(db, admin_id)

        with pytest.raises(AppError) as err:
            await admin_service.update_staff(
                db, admin_id, admin_id, StaffUpdate(is_active=False)
            )

        assert err.value.code == "self_lockout"

    async def test_the_last_admin_cannot_be_demoted(self, db, badge_holders):
        """Losing every Admin is only recoverable with a psql prompt, which is
        the thing this screen exists to make unnecessary."""
        admin_id = badge_holders["admin"]["id"]
        await act_as(db, badge_holders["ops"]["id"])

        with pytest.raises(AppError) as err:
            await admin_service.update_staff(
                db, badge_holders["ops"]["id"], admin_id, StaffUpdate(role="packer")
            )

        assert err.value.code == "last_admin"

    async def test_a_second_admin_makes_demotion_allowed(self, db, badge_holders):
        admin_id = badge_holders["admin"]["id"]
        await act_as(db, admin_id)
        await db.execute(
            text("update profiles set role = 'admin' where id = :id"),
            {"id": badge_holders["ops"]["id"]},
        )

        updated = await admin_service.update_staff(
            db, badge_holders["ops"]["id"], admin_id, StaffUpdate(role="packer")
        )

        assert updated.role == "packer"

    async def test_backup_approver_is_only_meaningful_for_ops(self, db, badge_holders):
        """DECISIONS §4: gate approvals escalate at T+15m to an Ops Manager
        flagged as backup. Flagging a guard produces an escalation path that
        notifies nobody, which is worse than not having one."""
        await act_as(db, badge_holders["admin"]["id"])

        with pytest.raises(AppError) as err:
            await admin_service.update_staff(
                db,
                badge_holders["admin"]["id"],
                badge_holders["guard"]["id"],
                StaffUpdate(is_backup_approver=True),
            )

        assert err.value.code == "invalid_flag"

    async def test_moving_off_a_badge_role_deactivates_the_badge(self, db, badge_holders):
        """Done silently as part of the role change rather than refused: the
        alternative is two steps in an order the Admin has to know."""
        await act_as(db, badge_holders["admin"]["id"])

        updated = await admin_service.update_staff(
            db,
            badge_holders["admin"]["id"],
            badge_holders["packer"]["id"],
            StaffUpdate(role="warehouse_staff"),
        )

        assert updated.role == "warehouse_staff"
        assert updated.badge_active is False
        assert updated.has_badge is True, "the record is superseded, not erased"

    async def test_an_unknown_account_is_a_404_not_a_silent_no_op(self, db, badge_holders):
        await act_as(db, badge_holders["admin"]["id"])

        with pytest.raises(AppError) as err:
            await admin_service.update_staff(
                db,
                badge_holders["admin"]["id"],
                uuid.uuid4(),
                StaffUpdate(full_name="Nobody At All"),
            )

        assert err.value.http_status == 404


class TestStaffDirectory:
    async def test_it_counts_what_a_person_has_been_attributed(self, db, badge_holders):
        """"Deactivate" is a considered decision when you can see the person
        has 200 cartons against their name."""
        await as_authenticated(db, badge_holders["admin"]["id"])
        staff = await admin_service.list_staff(db)
        await as_postgres(db)

        by_id = {str(s.id): s for s in staff}
        packer = by_id[str(badge_holders["packer"]["id"])]

        assert packer.can_hold_badge is True
        assert packer.role_label == "Packing"
        assert packer.cartons_packed >= 0
        assert not hasattr(packer, "badge_code")

    async def test_inactive_accounts_can_be_filtered_out(self, db, badge_holders):
        await act_as(db, badge_holders["admin"]["id"])
        await db.execute(
            text("update profiles set is_active = false where id = :id"),
            {"id": badge_holders["guard"]["id"]},
        )

        active_only = await admin_service.list_staff(db, include_inactive=False)
        everyone = await admin_service.list_staff(db, include_inactive=True)

        ids = {str(s.id) for s in active_only}
        assert str(badge_holders["guard"]["id"]) not in ids
        assert len(everyone) > len(active_only)
