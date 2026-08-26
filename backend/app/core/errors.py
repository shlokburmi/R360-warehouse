"""Turning database refusals into answers a security guard can act on.

The control points in 0004_control_points.sql raise Postgres exceptions. A raw
`asyncpg.exceptions.CheckViolationError` leaking to a phone screen at the gate
at 6am is useless. This module maps SQLSTATE codes to HTTP status codes and
surfaces the trigger's MESSAGE and HINT, which were written to be read by the
person holding the scanner.
"""

import logging
import re
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, IntegrityError

log = logging.getLogger(__name__)

# SQLSTATE → (HTTP status, stable error code for the frontend to switch on)
_SQLSTATE_MAP: Dict[str, Any] = {
    "23514": (status.HTTP_409_CONFLICT, "control_point_failed"),   # check_violation
    "23001": (status.HTTP_409_CONFLICT, "immutable_record"),       # restrict_violation
    "23505": (status.HTTP_409_CONFLICT, "duplicate"),              # unique_violation
    "23503": (status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_reference"),
    "23502": (status.HTTP_422_UNPROCESSABLE_ENTITY, "missing_field"),
    "42501": (status.HTTP_403_FORBIDDEN, "not_permitted"),         # insufficient_privilege
    "P0001": (status.HTTP_409_CONFLICT, "business_rule"),          # raise_exception
    "57014": (status.HTTP_504_GATEWAY_TIMEOUT, "query_timeout"),
}

# RLS refusals do not raise; they return zero rows, or fail the WITH CHECK with
# code 42501. This catches the WITH CHECK phrasing so we can say something more
# useful than "new row violates row-level security policy".
_RLS_PATTERN = re.compile(r"row-level security policy", re.IGNORECASE)


class AppError(Exception):
    """A business rule the API enforces before the database gets involved."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "bad_request",
        http_status: int = status.HTTP_400_BAD_REQUEST,
        hint: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status
        self.hint = hint
        self.details = details or {}


class ControlPointError(AppError):
    """A PRD §4 hard stop was hit. Never retried, never overridden."""

    def __init__(self, message: str, *, hint: Optional[str] = None, **kw):
        super().__init__(
            message,
            code="control_point_failed",
            http_status=status.HTTP_409_CONFLICT,
            hint=hint,
            **kw,
        )


def _payload(code: str, message: str, hint: Optional[str] = None, **extra) -> Dict[str, Any]:
    body = {"error": {"code": code, "message": message}}
    if hint:
        body["error"]["hint"] = hint
    if extra:
        body["error"].update(extra)
    return body


def _extract_pg(exc: DBAPIError):
    """Pull SQLSTATE, message and hint off an asyncpg error, if present."""
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(exc, "code", None)
    message = getattr(orig, "message", None) or str(orig or exc)
    hint = getattr(orig, "hint", None)
    detail = getattr(orig, "detail", None)
    return sqlstate, message, hint, detail


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return JSONResponse(
            status_code=exc.http_status,
            content=_payload(exc.code, exc.message, exc.hint, **exc.details),
        )

    @app.exception_handler(IntegrityError)
    @app.exception_handler(DBAPIError)
    async def _db_error(request: Request, exc: DBAPIError):
        sqlstate, message, hint, detail = _extract_pg(exc)

        if _RLS_PATTERN.search(message or ""):
            log.warning("RLS refusal on %s: %s", request.url.path, message)
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content=_payload(
                    "not_permitted",
                    "Your role is not allowed to perform this action.",
                    "If you believe this is wrong, ask an Admin to check your role assignment.",
                ),
            )

        http_status, code = _SQLSTATE_MAP.get(
            sqlstate or "", (status.HTTP_500_INTERNAL_SERVER_ERROR, "database_error")
        )

        if http_status >= 500:
            log.exception("Unhandled database error on %s", request.url.path)
            return JSONResponse(
                status_code=http_status,
                content=_payload("database_error", "Something went wrong. Please retry."),
            )

        # Postgres prefixes messages when they bubble through PL/pgSQL contexts;
        # keep only the first line, which is the one the trigger wrote.
        clean = (message or "").split("\n")[0].strip()
        log.info("Business rule refusal (%s) on %s: %s", sqlstate, request.url.path, clean)

        return JSONResponse(
            status_code=http_status,
            content=_payload(code, clean, hint, detail=detail),
        )
