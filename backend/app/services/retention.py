"""Identity photo retention (PRD §8, DPDP Act 2023).

0006_storage.sql made the bucket private and deferred retention to "a scheduled
job". This is that job. Private forever is still forever, and the Act's
storage-limitation principle is about how long personal data is held, not who
can see it.

The threshold is `ID_PHOTO_REVALIDATION_DAYS` — the same number that forces a
re-capture — rather than a second, independent retention setting. A photo past
that age already verifies nothing (DECISIONS.md §2), and data held with no
purpose left is exactly what the Act asks you not to hold. One number means
there is no window in which a photo is both useless and retained.

Two properties this module is built around:

**Order matters.** The object is deleted from storage *first*, and only then is
the row updated. The reverse order has a failure mode with no recovery: if the
row is cleared and the delete then fails, nothing records where the file was, so
the bytes survive with no path pointing at them and no way to find them again.
In this order a crash leaves a file already gone and a row still pointing at it,
which the next sweep retries and storage answers with 404 — treated as success,
because it is.

**Nothing breaks when data is not deleted.** A retention job that silently stops
looks exactly like one that has nothing to do. That is why the sweep reports
counts, why `photo_retention_status` exposes `overdue`, and why an unexpectedly
large batch is logged rather than quietly processed.
"""

import logging
from typing import Any, Dict, List

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import Settings, get_settings

log = logging.getLogger(__name__)

BUCKET = "identity-photos"

# A ceiling on one sweep, not on the work. The backlog is drained across
# successive sweeps instead of one pass holding a transaction open over
# thousands of HTTP round trips. It also bounds the damage of a mistake: a
# misconfigured retention window destroys at most this many photos before
# someone notices the log.
MAX_PER_SWEEP = 200


async def _delete_object(settings: Settings, path: str) -> bool:
    """Remove one object from the private bucket.

    Storage RLS grants no DELETE to anyone (0006), so this is service-role only
    and therefore worker-only. A 404 counts as success: it means a previous
    sweep deleted the file and did not get as far as clearing the row.
    """
    url = f"{settings.supabase_url}/storage/v1/object/{BUCKET}/{path}"

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.delete(
            url,
            headers={
                "Authorization": f"Bearer {settings.supabase_service_role_key}",
                "apikey": settings.supabase_service_role_key,
            },
        )

    if response.status_code == 404:
        log.info("Identity photo already absent from storage: %s", path)
        return True

    if response.status_code >= 400:
        log.warning(
            "Could not delete identity photo %s: %s %s",
            path,
            response.status_code,
            response.text[:200],
        )
        return False

    return True


async def expired_photos(conn: AsyncConnection, retention_days: int) -> List[Dict[str, Any]]:
    """Visitors whose identity photo is past its useful life.

    Blocked visitors are excluded, and that is purpose limitation working in
    both directions rather than an exception to it. A block is enforced on the
    mobile number, and a number is trivially borrowed — so for a blocked visitor
    the photo is still doing the exact job it was captured for: letting a guard
    confirm that the person at the gate is the person who was blocked.
    """
    rows = await conn.execute(
        text(
            """
            select id, mobile, id_photo_path, id_photo_captured_at
              from visitors
             where id_photo_path is not null
               and not is_blocked
               and id_photo_captured_at < identity_photo_cutoff(:days)
             order by id_photo_captured_at
             limit :limit
            """
        ),
        {"days": retention_days, "limit": MAX_PER_SWEEP},
    )
    return [dict(r) for r in rows.mappings()]


async def purge_expired_photos(
    conn: AsyncConnection, settings: Settings = None
) -> Dict[str, int]:
    """Delete every identity photo past the retention window.

    Returns counts rather than raising on a partial failure. One unreachable
    object must not stop the rest of the sweep — the whole point is that the
    backlog shrinks every cycle, and a single stuck file that blocks the queue
    would turn one storage hiccup into an indefinite retention breach.
    """
    settings = settings or get_settings()

    if not settings.supabase_service_role_key:
        # Loudly, every sweep. A retention job that cannot delete anything and
        # says nothing is worse than one that is switched off on purpose.
        log.error(
            "Identity photo retention cannot run: SUPABASE_SERVICE_ROLE_KEY is not set. "
            "Photos are being kept past the %s-day limit (PRD §8).",
            settings.id_photo_revalidation_days,
        )
        return {"purged": 0, "failed": 0, "unconfigured": 1}

    candidates = await expired_photos(conn, settings.id_photo_revalidation_days)
    if not candidates:
        return {"purged": 0, "failed": 0, "unconfigured": 0}

    if len(candidates) >= MAX_PER_SWEEP:
        log.warning(
            "Identity photo retention is at its per-sweep ceiling (%s). "
            "There is a backlog; it will drain over the next few sweeps.",
            MAX_PER_SWEEP,
        )

    purged = 0
    failed = 0

    for visitor in candidates:
        # Storage first. See the module docstring — the reverse order can lose
        # the only pointer to a file that still exists.
        if not await _delete_object(settings, visitor["id_photo_path"]):
            failed += 1
            continue

        recorded = (
            await conn.execute(
                text("select purge_identity_photo(cast(:id as uuid), :reason)"),
                {"id": str(visitor["id"]), "reason": "retention"},
            )
        ).scalar_one()

        if recorded:
            purged += 1
        else:
            # The row was already clear — another sweep got there first. The
            # object delete above was then a no-op 404. Not an error.
            log.info("Visitor %s was already purged", visitor["id"])

    if purged or failed:
        log.info(
            "Identity photo retention: %s purged, %s failed (older than %s days)",
            purged,
            failed,
            settings.id_photo_revalidation_days,
        )

    return {"purged": purged, "failed": failed, "unconfigured": 0}


async def retention_status(
    conn: AsyncConnection, settings: Settings = None
) -> Dict[str, Any]:
    """What an auditor asks for: how much is held, how old, and is any overdue."""
    settings = settings or get_settings()

    row = (
        await conn.execute(
            text(
                """
                select photos_held, photos_purged, oldest_held_at, last_purge_at,
                       overdue, retained_for_block
                  from photo_retention_status(:days)
                """
            ),
            {"days": settings.id_photo_revalidation_days},
        )
    ).mappings().one()

    return {
        **dict(row),
        "retention_days": settings.id_photo_revalidation_days,
        # Surfaced rather than inferred from `overdue`: an operator seeing a
        # backlog needs to know whether the job is behind or not running at all,
        # and those have different fixes.
        "enabled": bool(settings.supabase_service_role_key),
    }
