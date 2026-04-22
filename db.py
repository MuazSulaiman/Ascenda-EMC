# db.py
from dotenv import load_dotenv
load_dotenv()

import os
from sqlalchemy import create_engine


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