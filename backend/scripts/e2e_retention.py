"""End-to-end check of the identity photo retention job (PRD §8).

    python scripts/e2e_retention.py

Uploads a real object into the private `identity-photos` bucket, points an
expired visitor at it, runs the sweep, and checks the bytes are actually gone.

This exists because tests/test_retention.py cannot: it drives Postgres directly,
so it proves the policy and the bookkeeping but never touches Supabase Storage.
The one thing a retention job has to do is make bytes stop existing, and that is
the half a database test cannot see.

Not idempotent. It commits a visitor row (the whole point is surviving a
transaction boundary) — run `supabase db reset` afterwards.
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import get_settings  # noqa: E402
from app.db.session import admin_transaction, dispose_engines  # noqa: E402
from app.services.retention import purge_expired_photos, retention_status  # noqa: E402

MOBILE = "9000000001"
PATH = f"{MOBILE}/retention-e2e.jpg"

# A 1x1 JPEG. The bucket only accepts image mime types, so this has to be real.
PIXEL = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300ffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffc00011080001000103012200021101031101"
    "ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b51000"
    "02010303020403050504040000017d01020300041105122131410613516107227114328191a108"
    "2342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748"
    "494a535455565758595a636465666768696a737475767778797a838485868788898a9293949596"
    "9798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9"
    "dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00fbfeffd9"
)

checks = {"pass": 0, "fail": 0}


def ok(label, condition, extra=""):
    checks["pass" if condition else "fail"] += 1
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  <- {extra}" if extra and not condition else ""))


def storage_headers(settings):
    return {
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
        "apikey": settings.supabase_service_role_key,
    }


async def object_exists(settings, path) -> bool:
    url = f"{settings.supabase_url}/storage/v1/object/identity-photos/{path}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(url, headers=storage_headers(settings))
    return response.status_code == 200


async def main():
    settings = get_settings()

    if not settings.supabase_service_role_key:
        raise SystemExit("SUPABASE_SERVICE_ROLE_KEY is not set; nothing to test.")

    print("\n1. Set up an expired photo")
    url = f"{settings.supabase_url}/storage/v1/object/identity-photos/{PATH}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            url,
            headers={**storage_headers(settings), "Content-Type": "image/jpeg", "x-upsert": "true"},
            content=PIXEL,
        )
    ok("the photo uploads to the private bucket", response.status_code < 400, response.text[:200])
    ok("and it is really there", await object_exists(settings, PATH))

    captured = datetime.now(timezone.utc) - timedelta(days=settings.id_photo_revalidation_days + 5)
    async with admin_transaction("e2e") as conn:
        visitor_id = (
            await conn.execute(
                text(
                    """
                    insert into visitors (mobile, full_name, id_photo_path, id_photo_captured_at)
                    values (:m, 'Retention Test Visitor', :p, :c)
                    on conflict (mobile) do update
                      set id_photo_path = excluded.id_photo_path,
                          id_photo_captured_at = excluded.id_photo_captured_at,
                          id_photo_purged_at = null,
                          id_photo_purge_reason = null
                    returning id
                    """
                ),
                {"m": MOBILE, "p": PATH, "c": captured},
            )
        ).scalar_one()

    async with admin_transaction("e2e") as conn:
        before = await retention_status(conn, settings)
    ok("the status reports it as overdue", before["overdue"] >= 1, str(before))
    ok("the job reports itself as configured", before["enabled"] is True)

    print("\n2. Run the sweep")
    async with admin_transaction("e2e") as conn:
        result = await purge_expired_photos(conn, settings)
    ok("the sweep purged it", result["purged"] >= 1, str(result))
    ok("nothing failed", result["failed"] == 0, str(result))

    print("\n3. The bytes are gone")
    ok("the object is no longer in storage", not await object_exists(settings, PATH))

    async with admin_transaction("e2e") as conn:
        row = (
            await conn.execute(
                text(
                    """
                    select id_photo_path, id_photo_captured_at, id_photo_purged_at,
                           id_photo_purge_reason, id_photo_purged_age_days
                      from visitors where id = :id
                    """
                ),
                {"id": visitor_id},
            )
        ).mappings().one()
    ok("the path is cleared", row["id_photo_path"] is None)
    ok("the destruction is recorded", row["id_photo_purged_at"] is not None)
    ok("with a reason", row["id_photo_purge_reason"] == "retention")
    ok("and the age it reached", (row["id_photo_purged_age_days"] or 0) >= 180)

    print("\n4. A second sweep is a clean no-op")
    async with admin_transaction("e2e") as conn:
        again = await purge_expired_photos(conn, settings)
        after = await retention_status(conn, settings)
    ok("nothing left to purge", again["purged"] == 0 and again["failed"] == 0, str(again))
    ok("overdue is back to zero", after["overdue"] == 0, str(after))
    ok("the purge is counted", after["photos_purged"] >= 1, str(after))

    await dispose_engines()
    print(f"\n{checks['pass']} passed, {checks['fail']} failed")
    sys.exit(1 if checks["fail"] else 0)


asyncio.run(main())
