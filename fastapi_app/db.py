"""
Database configuration and helper functions.

This module sets up the asynchronous SQLAlchemy engine and session
factory. It also normalises the DATABASE_URL so common provider
formats like 'postgres://...' or 'postgresql://...' are converted to
the required 'postgresql+asyncpg://' for SQLAlchemy asyncio.

Env vars:
- DATABASE_URL  -> connection string (any of postgres://, postgresql://,
                   postgresql+psycopg2://, postgresql+asyncpg://)
- DB_SSLMODE    -> optional: 'require' (padrão) ou 'disable'
                   (com asyncpg, SSL é configurado via connect_args['ssl'])
"""

from __future__ import annotations

import os
from typing import AsyncGenerator, Dict, Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def _normalize_db_url(raw_url: str) -> str:
    """
    Convert common Postgres URLs to the asyncpg dialect required by SQLAlchemy asyncio.

    Accepted inputs:
      - postgres://user:pass@host:5432/db
      - postgresql://user:pass@host:5432/db
      - postgresql+psycopg2://user:pass@host:5432/db
      - postgresql+asyncpg://user:pass@host:5432/db

    Will output:
      - postgresql+asyncpg://user:pass@host:5432/db
    """
    if not raw_url or not raw_url.strip():
        raise RuntimeError(
            "DATABASE_URL is empty or not set. "
            "Provide a valid Postgres connection string."
        )

    url = raw_url.strip()

    # Normalize scheme to async driver
    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://") :]
    elif url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    elif url.startswith("postgresql+psycopg2://"):
        url = "postgresql+asyncpg://" + url[len("postgresql+psycopg2://") :]
    # if already postgresql+asyncpg:// keep as is

    # IMPORTANT: do NOT append 'sslmode' – asyncpg doesn't support it.
    return url


def _build_connect_args() -> Dict[str, Any]:
    """
    Map DB_SSLMODE to asyncpg's 'ssl' connect arg.

    - 'disable' -> {}
    - anything else (require/verify-*) -> {'ssl': True}
    """
    mode = os.getenv("DB_SSLMODE", "require").lower()
    if mode in {"disable", "off", "false", "no"}:
        return {}
    # Railway/Cloud: require SSL
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
    # Import here to register metadata
    from .models.db_models import Base  # noqa: WPS433,F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
