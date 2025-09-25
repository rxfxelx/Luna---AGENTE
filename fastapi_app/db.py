# fastapi_app/db.py
"""
Configuração do banco (SQLAlchemy + asyncpg) com suporte robusto a SSL na Railway.

Problema resolvido:
- Railway usa proxy com certificado autoassinado.
- Em asyncpg, mapear 'sslmode=require' diretamente para 'ssl=True' ativa
  verificação de certificado -> quebra com SSLCertVerificationError.
- Aqui criamos um SSLContext que cifra a conexão, mas NÃO verifica por padrão
  (equivalente prático ao comportamento de 'require' do libpq).
- Opcionalmente, suportamos verificação com CA próprio (verify-ca / verify-full).

ENVs aceitos:
- DATABASE_URL                       (ex.: postgresql://user:pass@host:port/db)
- DB_SSLMODE=disable|require|prefer|allow|verify-ca|verify-full
- DB_SSLROOTCERT                     (caminho absoluto p/ arquivo .pem)
- DB_SSLROOTCERT_B64                 (conteúdo base64 do CA)
- SQLALCHEMY_ECHO=0|1                (logs SQL)
"""

from __future__ import annotations

import base64
import os
import re
import ssl
import tempfile
from contextlib import asynccontextmanager
from typing import Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import declarative_base

# -------------------------------------------------------------------
# Base de modelos (suas tabelas estão em fastapi_app/models/db_models.py)
# -------------------------------------------------------------------
Base = declarative_base()

# -------------------------------------------------------------------
# ENV e URLs
# -------------------------------------------------------------------
DATABASE_URL_RAW = (os.getenv("DATABASE_URL", "") or "").strip()
if not DATABASE_URL_RAW:
    raise RuntimeError("DATABASE_URL não configurada.")

# força driver asyncpg
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
    """
    Retorna caminho de um arquivo .pem com a cadeia de CA, vindo de:
      - DB_SSLROOTCERT_B64 (base64)  -> gravamos em /tmp/ca.pem
      - DB_SSLROOTCERT (path)
    """
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
    - require/prefer/allow -> TLS sem verificação (equivalente prático a 'require' do libpq)
    - verify-ca/verify-full -> TLS com verificação via CA fornecido (ou sistema)
    """
    m = (mode or "").strip().lower()

    if m in {"disable", "off", "false", "0"}:
        return None

    if m in {"require", "prefer", "allow"}:
        # Cifra a conexão SEM validar certificado/hostname
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    if m in {"verify-ca", "verify_full", "verify-full"}:
        cafile = _load_ca_from_env()
        try:
            # cria contexto que valida o servidor
            ctx = ssl.create_default_context(cafile=cafile)
        except Exception:
            # fallback se cafile inválido; usa store do sistema
            ctx = ssl.create_default_context()

        # 'verify-full' valida hostname; 'verify-ca' não exige hostname estrito
        ctx.check_hostname = m in {"verify_full", "verify-full"}
        ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx

    # fallback seguro: criptografa sem verificar (igual branch 'require')
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
# FastAPI deps
# -------------------------------------------------------------------
@asynccontextmanager
async def get_db():
    """
    Dependência para FastAPI: injeta AsyncSession e garante fechamento.
    """
    async with SessionLocal() as session:
        yield session


# -------------------------------------------------------------------
# Init / Migrations-like (cria tabelas se não existirem)
# -------------------------------------------------------------------
async def init_models() -> None:
    """
    Executa CREATE TABLE IF NOT EXISTS com base no metadata dos modelos.
    """
    # Import tardio para registrar os modelos no metadata
    from .models import db_models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Logs úteis de diagnóstico:
    _safe_url = re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", DATABASE_URL_RAW)
    print(f"[startup] DB url(safe)={_safe_url}  DB_SSLMODE={DB_SSLMODE}")


async def dispose_engine() -> None:
    """Fecha o engine (útil em shutdown events)."""
    await engine.dispose()
