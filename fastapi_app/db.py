"""
Database configuration and helper functions.

This module sets up the asynchronous SQLAlchemy engine and session
factory. It also normalises the DATABASE_URL so common provider
formats like 'postgres://...' or 'postgresql://...' are converted to
the required 'postgresql+asyncpg://' for SQLAlchemy asyncio.

Env vars:
- DATABASE_URL   -> connection string (postgres://, postgresql://, postgresql+psycopg2://, postgresql+asyncpg://)
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
    """
    Convert common Postgres URLs to the asyncpg dialect required by SQLAlchemy asyncio.
    Output is always 'postgresql+asyncpg://...'
    """
    if not raw_url or not raw_url.strip():
        raise RuntimeError(
            "DATABASE_URL is empty or not set. "
            "Provide a valid Postgres connection string."
        )
    url = raw_url.strip()
    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://") :]
    elif url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    elif url.startswith("postgresql+psycopg2://"):
        url = "postgresql+asyncpg://" + url[len("postgresql+psycopg2://") :]
    # if already postgresql+asyncpg:// keep as is
    return url


def _build_connect_args() -> Dict[str, Any]:
    """
    Map DB_SSLMODE to asyncpg's 'ssl' connect arg.
    - 'disable'           -> {}
    - 'require' (default) -> {'ssl': True}  (usa CA do sistema - requer ca-certificates)
    - 'require_noverify'  -> {'ssl': <SSLContext sem verificação>}
    - 'verify-ca'/'verify-full' -> carrega CA de DB_SSLROOTCERT; em 'verify-full' mantém check_hostname=True
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
                "DB_SSLMODE set to verify-ca/verify-full but DB_SSLROOTCERT was not provided or not found."
            )
        ctx = ssl.create_default_context(cafile=ca_path)
        # verify-full também valida hostname (padrão do context)
        return {"ssl": ctx}

    # default: require (usa bundle do sistema)
    return {"ssl": True}


# --- Engine & Session ---
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
    """FastAPI dependency that yields an AsyncSession."""
    async with SessionLocal() as session:
        yield session


async def init_models() -> None:
    """Create tables on startup (for simple setups without Alembic)."""
    from .models.db_models import Base  # noqa: WPS433,F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
