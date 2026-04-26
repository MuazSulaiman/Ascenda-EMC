# db.py
from dotenv import load_dotenv
load_dotenv()

import os
from sqlalchemy import create_engine, text


def _env(key: str, default: str | None = None) -> str | None:
    val = os.environ.get(key, default)
    return val if (val is not None and str(val).strip() != "") else default


def _normalize_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)

    app_env = os.environ.get("APP_ENV", "development").lower()

    if "sslmode=" not in url:
        sep = "&" if "?" in url else "?"
        if app_env == "production":
            url = f"{url}{sep}sslmode=require"
        else:
            url = f"{url}{sep}sslmode=disable"
    return url


def get_database_url() -> str:
    url = _env("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Set it in your local environment for development "
            "or in Render environment variables for production."
        )
    return _normalize_db_url(url)


# Global engine
engine = create_engine(get_database_url(), pool_pre_ping=True, future=True)


# Migrations


def _run_migrations() -> None:
    """Idempotent schema migrations. Safe to re-run on every startup."""
    # ALTER TYPE ADD VALUE cannot run inside a normal transaction on PG < 12.
    # Use AUTOCOMMIT isolation to avoid that restriction.
    with engine.execution_options(isolation_level="AUTOCOMMIT").connect() as conn:
        conn.execute(text(
            "ALTER TYPE public.asc_change_source ADD VALUE IF NOT EXISTS 'DELETE'"
        ))

    with engine.begin() as conn:
        conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name   = 'visits'
                      AND column_name  = 'is_deleted'
                ) THEN
                    ALTER TABLE visits
                        ADD COLUMN is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
                        ADD COLUMN deleted_at TIMESTAMPTZ,
                        ADD COLUMN deleted_by INTEGER REFERENCES users(user_id);
                END IF;
            END $$;
        """))


_run_migrations()