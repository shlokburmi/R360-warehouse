"""Application settings, loaded from environment / .env."""

from functools import lru_cache
from typing import List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # --- App ---------------------------------------------------------------
    app_name: str = "Reward360 Warehouse API"
    environment: str = Field(default="local")  # local | staging | production
    debug: bool = Field(default=False)
    api_prefix: str = "/api/v1"

    # --- Database ----------------------------------------------------------
    # The request-path connection. MUST be the non-superuser `api_user` role,
    # otherwise RLS is bypassed and every policy in 0005_rls.sql stops applying.
    # See docs/DECISIONS.md §B1.
    database_url: str = Field(
        default="postgresql+asyncpg://api_user:api_password@127.0.0.1:54322/postgres"
    )
    # Privileged connection, used only by background workers (SLA escalation,
    # email dispatch). Never reachable from a request handler.
    admin_database_url: Optional[str] = None

    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_statement_timeout_ms: int = 15_000

    # --- Supabase ----------------------------------------------------------
    supabase_url: str = Field(default="http://127.0.0.1:54321")
    supabase_anon_key: str = Field(default="")
    supabase_service_role_key: str = Field(default="")
    # Shared secret GoTrue signs access tokens with. Local default is the
    # well-known Supabase CLI development secret.
    supabase_jwt_secret: str = Field(
        default="super-secret-jwt-token-with-at-least-32-characters-long"
    )
    jwt_algorithm: str = "HS256"
    jwt_audience: str = "authenticated"

    # --- CORS --------------------------------------------------------------
    # Kept as a raw string rather than List[str]: pydantic-settings tries to
    # JSON-decode complex types straight from the environment, before any
    # validator runs, so a plain comma-separated value in .env fails to parse.
    # Splitting in a property keeps .env readable.
    cors_origins_raw: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173",
        alias="CORS_ORIGINS",
    )

    # --- Business rules ----------------------------------------------------
    # Each of these answers an open question in PRD §13; see docs/DECISIONS.md.
    gate_approval_sla_minutes: int = 15       # → backup approver
    gate_escalation_minutes: int = 30         # → admin, flag sla_breached
    id_photo_revalidation_days: int = 180     # re-capture a stale photo
    signed_url_ttl_seconds: int = 300         # identity photo links
    scan_backdate_tolerance_hours: int = 24   # reject absurd offline clocks

    # --- Notifications -----------------------------------------------------
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_from: str = "warehouse@reward360.local"
    superadmin_email: Optional[str] = None

    @property
    def cors_origins(self) -> List[str]:
        return [o.strip() for o in self.cors_origins_raw.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()

    # A production deployment pointing at a superuser connection would disable
    # RLS silently. Fail loudly at boot instead of discovering it in an audit.
    if settings.is_production:
        if "postgres:" in settings.database_url.split("@")[0]:
            raise RuntimeError(
                "database_url must use the non-superuser `api_user` role in production; "
                "connecting as `postgres` bypasses row level security."
            )
        if not settings.supabase_jwt_secret or len(settings.supabase_jwt_secret) < 32:
            raise RuntimeError("supabase_jwt_secret is missing or too short.")

    return settings
