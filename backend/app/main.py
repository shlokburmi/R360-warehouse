"""Reward360 Warehouse Management API."""

import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.v1 import (
    admin,
    gate,
    loading,
    meta,
    packing,
    pickup,
    reports,
    warehouse,
)
from app.core.config import get_settings
from app.core.errors import install_error_handlers
from app.db.session import dispose_engines, get_engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("warehouse")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    log.info("Starting %s (%s)", settings.app_name, settings.environment)

    # Verify at boot that the request-path connection is actually subject to RLS.
    # If someone points DATABASE_URL at a superuser, every policy silently stops
    # applying and nothing else would ever tell us.
    try:
        engine = get_engine(settings)
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("select current_user, usesuper from pg_user where usename = current_user")
                )
            ).first()
            if row and row[1]:
                message = (
                    f"DATABASE_URL connects as superuser '{row[0]}' — row level security "
                    "will be bypassed. Use the api_user role (see docs/DECISIONS.md §B1)."
                )
                if settings.is_production:
                    raise RuntimeError(message)
                log.warning("SECURITY: %s", message)
            else:
                log.info("Database connection OK as '%s' (RLS enforced)", row[0] if row else "?")
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 - startup diagnostics only
        log.warning("Could not verify database connection at startup: %s", exc)

    yield
    await dispose_engines()


settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description=(
        "Warehouse operations for Reward360 bank service platforms. "
        "Inbound: gate entry, box counting, unit scanning, inbound reconciliation. "
        "Outbound: invoice matching, packing attribution, out-scan, batch release, "
        "pickup verification and gate exit."
    ),
    lifespan=lifespan,
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None,
)

# DELETE belongs here: /admin/staff/{id} is a real route the Staff screen calls,
# and a method missing from this list fails at the preflight — the request never
# leaves the browser, so it presents as "no connection" rather than as a refusal.
# It only ever worked locally because the Vite dev server proxies the API onto
# the app's own origin; in production (Vercel → Render) it is cross-origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    # Auth is a bearer token, not a cookie, so credentials are not needed for
    # this app to work — but it stays on because it forces the middleware to
    # echo an exact origin rather than "*", which is the behaviour we want.
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

install_error_handlers(app)

api = APIRouter(prefix=settings.api_prefix)
api.include_router(meta.router)
api.include_router(gate.router)
api.include_router(warehouse.router)
api.include_router(packing.router)
api.include_router(pickup.router)
api.include_router(reports.router)
api.include_router(admin.router)
api.include_router(loading.router)
app.include_router(api)


@app.get("/health", tags=["health"])
async def health():
    """Liveness probe for Render. Deliberately does not touch the database —
    a slow query should not cause the platform to restart a healthy process."""
    return {"status": "ok", "environment": settings.environment}


@app.get("/health/db", tags=["health"])
async def health_db():
    engine = get_engine(settings)
    async with engine.connect() as conn:
        await conn.execute(text("select 1"))
    return {"status": "ok", "database": "reachable"}
