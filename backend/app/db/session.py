"""Database access.

Two engines, deliberately separate:

* `engine`      — the request path. Connects as `api_user`, which is not a
                  superuser and does not own the tables, so `FORCE ROW LEVEL
                  SECURITY` applies to it. Every request opens a transaction,
                  assumes the `authenticated` role, and installs the verified
                  JWT claims. From that point on `auth.uid()` inside a policy
                  resolves exactly as it would for a direct supabase-js call.

* `admin_engine` — migrations and background workers only. Bypasses RLS. There
                  is no dependency that exposes it to a route handler, and that
                  is the point.

If these two are ever collapsed into one, every policy in 0005_rls.sql stops
doing anything and nothing visibly breaks — which is the failure mode worth
engineering against.
"""

import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.core.config import Settings, get_settings

_engine: Optional[AsyncEngine] = None
_admin_engine: Optional[AsyncEngine] = None


def get_engine(settings: Optional[Settings] = None) -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = settings or get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=True,
            echo=settings.debug,
            # Server-side statement cache off: pgbouncer in transaction mode
            # (Supabase's pooler) does not guarantee the same backend between
            # statements, and a cached plan from another session is a very
            # confusing bug to chase.
            connect_args={"statement_cache_size": 0, "prepared_statement_cache_size": 0},
        )
    return _engine


def get_admin_engine(settings: Optional[Settings] = None) -> AsyncEngine:
    global _admin_engine
    if _admin_engine is None:
        settings = settings or get_settings()
        url = settings.admin_database_url or settings.database_url
        _admin_engine = create_async_engine(url, pool_size=2, max_overflow=2, pool_pre_ping=True)
    return _admin_engine


async def dispose_engines() -> None:
    global _engine, _admin_engine
    for eng in (_engine, _admin_engine):
        if eng is not None:
            await eng.dispose()
    _engine = None
    _admin_engine = None


@asynccontextmanager
async def rls_transaction(
    claims: Dict[str, Any],
    settings: Optional[Settings] = None,
) -> AsyncIterator[AsyncConnection]:
    """Open a transaction that Postgres sees as the authenticated end user.

    Everything inside is subject to RLS. The transaction commits on clean exit
    and rolls back on any exception, which is what gives PRD §7 atomicity: a
    gate entry and its people rows either both land or neither does.
    """
    settings = settings or get_settings()
    engine = get_engine(settings)

    async with engine.connect() as conn:
        async with conn.begin():
            # SET LOCAL confines all of this to the current transaction, so a
            # pooled connection can never leak one user's identity into the
            # next request that borrows it.
            await conn.execute(text("SET LOCAL ROLE authenticated"))
            await conn.execute(
                text("SELECT set_config('request.jwt.claims', :claims, true)"),
                {"claims": json.dumps(claims)},
            )
            await conn.execute(
                text("SELECT set_config('app.actor_id', :uid, true)"),
                {"uid": str(claims.get("sub", ""))},
            )
            await conn.execute(
                text("SELECT set_config('app.actor_source', 'api', true)")
            )
            await conn.execute(
                text(f"SET LOCAL statement_timeout = {int(settings.db_statement_timeout_ms)}")
            )
            yield conn


@asynccontextmanager
async def admin_transaction(actor_label: str = "worker") -> AsyncIterator[AsyncConnection]:
    """Privileged transaction for background jobs. Not usable from a request."""
    engine = get_admin_engine()
    async with engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text("SELECT set_config('app.actor_source', :src, true)"),
                {"src": actor_label},
            )
            yield conn
