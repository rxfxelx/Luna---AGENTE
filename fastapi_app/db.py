"""
Database configuration and helper functions.

This module sets up the asynchronous SQLAlchemy engine and session
factory. It also normalises the DATABASE_URL so common provider
formats like 'postgres://...' or 'postgresql://...' are converted to
the required 'postgresql+asyncpg://...' for SQLAlchemy asyncio.

Env vars:
- DATABASE_URL  -> connection string (any of postgres://, postgresql://,
                   postgresql+psycopg2://, postgresql+asyncpg://)
- DB_SSLMODE    -> optional, default 'require' (set to 'disable' for local)
"""

from __future__ import annotations

import os
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def _normalize_db_url(raw_url: str) -> str:
    """
    Convert common Postgres URLs to the asyncpg dialect required by SQLAlchemy asyncio,
    and add sslmode if requested.

    Accepted inputs:
      - postgres://user:pass@host:5432/db
      - postgresql://user:pass@host:5432/db
      - postgresql+psycopg2://user:pass@host:5432/db
      - postgresql+asyncpg://user:pass@host:5432/db

    Will output:
      - postgresql+asyncpg://user:pass@host:5432/db?sslmode=<value>
    """
    if not raw_url or not raw_url.strip():
        raise RuntimeError(
            "DATABASE_URL is empty or not set. "
            "Provide a valid Postgres connection string."
        )

    url = raw_url.strip()

    # Normaliza o esquema para o driver assíncrono
    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://") :]
    elif url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    elif url.startswith("postgresql+psycopg2://"):
        url = "postgresql+asyncpg://" + url[len("postgresql+psycopg2://") :]
    # se já for postgresql+asyncpg://, mantém como está

    # sslmode (útil em Railway e afins). Para local, use DB_SSLMODE=disable
    sslmode = os.getenv("DB_SSLMODE", "require").lower()
    if "sslmode=" not in url and sslmode and sslmode != "disable":
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode={sslmode}"

    return url


# --- Engine & Session ---
DATABASE_URL = _normalize_db_url(os.getenv("DATABASE_URL", ""))

engine: AsyncEngine = create_async_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    pool_pre_ping=True,
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
    # Import aqui para registrar o metadata
    from .models.db_models import Base  # noqa: WPS433,F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
