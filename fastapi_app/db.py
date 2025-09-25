# fastapi_app/db.py
"""
Configuração do banco (SQLAlchemy + asyncpg) com SSL compatível com Railway
e dependência FastAPI correta para injeção de AsyncSession.

Problemas tratados:
- Railway/Postgres com certificado autoassinado: criamos SSLContext que cifra
  sem exigir verificação (equivalente prático a 'require' do libpq), com opção
  de verificação via CA próprio (verify-ca/verify-full).
- get_db() sem @asynccontextmanager: FastAPI injeta a sessão correta, evitando
  AttributeError em 'db.execute(...)'.

ENVs aceitos:
- DATABASE_URL                       (ex.: postgresql://user:pass@host:port/db)
- DB_SSLMODE=disable|require|prefer|allow|verify-ca|verify-full
- DB_SSLROOTCERT                     (caminho para arquivo .pem)
- DB_SSLROOTCERT_B64                 (conteúdo base64 do CA)
- SQLALCHEMY_ECHO=0|1
"""

from __future__ import annotations

import base64
import os
import re
import ssl
import tempfile
from typing import Optional, AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import declarative_base

# -------------------------------------------------------------------
# Base
# -------------------------------------------------------------------
Base = declarative_base()

# -------------------------------------------------------------------
# ENV e URLs
# -------------------------------------------------------------------
DATABASE_URL_RAW = (os.getenv("DATABASE_URL", "") or "").strip()
if not DATABASE_URL_RAW:
    raise RuntimeError("DATABASE_URL não configurada.")

# Garante driver asyncpg
ASYNC_DATABASE_URL = re.sub(
    r"^postgresql\+asyncpg://|^postgresql://",
    "postgresql+asyncpg://",
    DATABASE_URL_RAW,
    count=1,
)

DB_SSLMODE = (os.getenv("DB_SSLMODE", "disable") or "disable").strip().lower()
DB_SSLROOTCERT = (os.getenv("DB_SSLROOTCERT", "") or "").strip()
DB_SSLROOTCERT_B64 = (os.getenv("DB_SSLROOTCERT_B64", "") or "").strip()
SQL_ECHO = (os.getenv("SQLALCHEMY_ECHO", "0") or "0").strip() in {"1", "true", "True"}

# -------------------------------------------------------------------
# SSL helpers
# -------------------------------------------------------------------
def _load_ca_from_env() -> Optional[str]:
    """Retorna caminho de um .pem vindo de DB_SSLROOTCERT_B64 ou DB_SSLROOTCERT."""
    if DB_SSLROOTCERT_B64:
        try:
            data = base64.b64decode(DB_SSLROOTCERT_B64)
            tmp = tempfile.NamedTemporaryFile(prefix="db_ca_", suffix=".pem", delete=False)
            tmp.write(data)
            tmp.flush()
            tmp.close()
            return tmp.name
        except Exception as exc:
            print(f"[db] falha ao decodificar DB_SSLROOTCERT_B64: {exc!r}")
            return None
    if DB_SSLROOTCERT and os.path.exists(DB_SSLROOTCERT):
        return DB_SSLROOTCERT
    return None


def _ssl_context_for_mode(mode: str) -> Optional[ssl.SSLContext]:
    """
    Cria um SSLContext apropriado para asyncpg.
    - disable/off -> None (sem TLS)
    - require/prefer/allow -> TLS sem verificação (CERT_NONE, check_hostname=False)
    - verify-ca/verify-full -> TLS com verificação via CA (ou store do sistema)
    """
    m = (mode or "").strip().lower()

    if m in {"disable", "off", "false", "0"}:
        return None

    if m in {"require", "prefer", "allow"}:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    if m in {"verify-ca", "verify_full", "verify-full"}:
        cafile = _load_ca_from_env()
        try:
            ctx = ssl.create_default_context(cafile=cafile)
        except Exception:
            ctx = ssl.create_default_context()
        ctx.check_hostname = m in {"verify_full", "verify-full"}
        ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx

    # fallback: cifra sem verificação
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# -------------------------------------------------------------------
# Engine & Session
# -------------------------------------------------------------------
def _build_engine() -> AsyncEngine:
    connect_args = {}
    ssl_ctx = _ssl_context_for_mode(DB_SSLMODE)
    if ssl_ctx is not None:
        connect_args["ssl"] = ssl_ctx

    eng = create_async_engine(
        ASYNC_DATABASE_URL,
        echo=SQL_ECHO,
        pool_pre_ping=True,
        future=True,
        connect_args=connect_args,
    )
    return eng


engine: AsyncEngine = _build_engine()
SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)

# -------------------------------------------------------------------
# FastAPI dependency (SEM @asynccontextmanager)
# -------------------------------------------------------------------
async def get_db() -> AsyncIterator[AsyncSession]:
    """Dependência para FastAPI: injeta AsyncSession e garante fechamento."""
    async with SessionLocal() as session:
        yield session

# -------------------------------------------------------------------
# Init / Shutdown
# -------------------------------------------------------------------
async def init_models() -> None:
    """Cria tabelas se não existirem."""
    # Import tardio para registrar os modelos
    from .models import db_models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    _safe_url = re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", DATABASE_URL_RAW)
    print(f"[startup] DB url(safe)={_safe_url}  DB_SSLMODE={DB_SSLMODE}")


async def dispose_engine() -> None:
    """Fecha o engine (útil em eventos de shutdown)."""
    await engine.dispose()
