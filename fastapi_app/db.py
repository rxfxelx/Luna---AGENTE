"""
Database configuration and helper functions.

This module sets up the asynchronous SQLAlchemy engine and session
factory. It also normalises the DATABASE_URL so common provider
formats like 'postgres://...' or 'postgresql://...' are converted to
the required 'postgresql+asyncpg://' for SQLAlchemy asyncio.

Env vars:
- DATABASE_URL   -> connection string (postgres://, postgresql://, etc.)
- DB_SSLMODE     -> 'require' (default), 'disable', 'require_noverify', 'verify-ca', 'verify-full'
- DB_SSLROOTCERT -> path to CA file when using verify-ca/full (optional)
"""

from __future__ import annotations

import os
import ssl
from typing import Any, AsyncGenerator, Dict

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def _normalize_db_url(raw_url: str) -> str:
    """Ensure that the DB URL uses the asyncpg driver."""
    if not raw_url or not raw_url.strip():
        raise RuntimeError(
            "DATABASE_URL is empty or not set. Provide a valid Postgres connection string."
        )
    url = raw_url.strip()
    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://") :]
    elif url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    elif url.startswith("postgresql+psycopg2://"):
        url = "postgresql+asyncpg://" + url[len("postgresql+psycopg2://") :]
    return url


def _build_connect_args() -> Dict[str, Any]:
    """
    Map DB_SSLMODE to asyncpg's connect_args.

    Modes:
    - disable            : no SSL
    - require (default)  : SSL with system CA bundle
    - require_noverify   : SSL without certificate/hostname verification
    - verify-ca/full     : SSL with custom CA (provide DB_SSLROOTCERT)
    """
    mode = os.getenv("DB_SSLMODE", "require").lower()
    ca_path = os.getenv("DB_SSLROOTCERT")
    if mode in {"disable", "off", "false", "no"}:
        return {}
    if mode in {"require_noverify", "noverify", "insecure"}:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return {"ssl": ctx}
    if mode in {"verify-ca", "verify_full", "verify-full"}:
        if not ca_path or not os.path.exists(ca_path):
            raise RuntimeError(
                "DB_SSLROOTCERT must be set when using verify-ca or verify-full."
            )
        ctx = ssl.create_default_context(cafile=ca_path)
        return {"ssl": ctx}
    return {"ssl": True}


DATABASE_URL = _normalize_db_url(os.getenv("DATABASE_URL", ""))

engine: AsyncEngine = create_async_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    pool_pre_ping=True,
    connect_args=_build_connect_args(),
)

SessionLocal = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an async database session."""
    async with SessionLocal() as session:
        yield session


async def init_models() -> None:
    """Create tables on startup if they don't exist."""
    from .models.db_models import Base  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
