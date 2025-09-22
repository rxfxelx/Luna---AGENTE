"""
Database configuration and helper functions.

This module sets up the asynchronous SQLAlchemy engine and session
factory.  The database connection URL is read from the ``DATABASE_URL``
environment variable.  A simple table creation helper is also provided
to initialise the schema at application startup when using SQLite or
when migrations have not yet been applied.
"""

from __future__ import annotations

import os
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# Read the database URL from environment.  It must be in a form
# supported by SQLAlchemy async engines, for example:
# ``postgresql+asyncpg://user:password@hostname:port/database``
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL environment variable must be set. "
        "See .env.example for an example configuration."
    )

# Create the asynchronous engine.  ``future=True`` enables SQLAlchemy
# 2.0 style usage; ``echo=False`` disables verbose SQL logging by
# default (can be set to True for debugging).
engine = create_async_engine(DATABASE_URL, future=True, echo=False)

# Session factory bound to the async engine.  ``expire_on_commit=False``
# prevents objects from being expired after each commit, which makes
# them usable without re-loading.
AsyncSessionLocal = sessionmaker(
    bind=engine,
    expire_on_commit=False,
    class_=AsyncSession,
)

# Declarative base class.  All models should inherit from this.
Base = declarative_base()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Provide a transactional scope around a series of operations.

    This dependency creates a new :class:`AsyncSession`, yields it
    (allowing the caller to use it for database operations), and
    ensures the session is properly closed after the request finishes.
    """
    async with AsyncSessionLocal() as session:
        yield session


async def init_models() -> None:
    """Create database tables if they do not already exist.

    This helper imports the model module so that SQLAlchemy is aware of
    the defined tables and then calls ``create_all`` on the metadata.
    In a production environment you should instead apply migrations
    using a tool such as Alembic, but this function allows the schema
    to be generated in environments where migrations have not yet
    been applied (for example, during initial local development).
    """
    # Import the model definitions.  This side‑effect registers the
    # table metadata with SQLAlchemy's metadata object on Base.
    from .models import db_models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)