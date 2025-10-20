# db.py
import os
from sqlalchemy import create_engine

def _normalize_db_url(url: str) -> str:
    # Normalize postgres:// → postgresql+psycopg:// for SQLAlchemy psycopg v3
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    if "sslmode=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode=require"
    return url

def get_database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. In Render → Web Service → Environment, add it "
            "from your Render PostgreSQL’s External Connection string."
        )
    return _normalize_db_url(url)

# Global engine (safe with pool_pre_ping)
engine = create_engine(get_database_url(), pool_pre_ping=True, future=True)
