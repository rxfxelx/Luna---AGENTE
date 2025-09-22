"""
Entry point for the FastAPI application.

Includes simple startup diagnostics (sanitised) to help detect DB/SSL issues
without exposing secrets.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI

from .db import init_models
from .routes import whatsapp as whatsapp_router

app = FastAPI(title="Luna Backend (Uazapi + OpenAI)")

# Routers
app.include_router(whatsapp_router)


@app.on_event("startup")
async def on_startup() -> None:
    # Logs de diagnóstico (sem vazar senha)
    db_url_raw = os.getenv("DATABASE_URL", "")
    sslmode = os.getenv("DB_SSLMODE", "require")
    try:
        parts = urlsplit(db_url_raw)
        # ofusca senha
        netloc = parts.netloc
        if "@" in netloc and ":" in netloc.split("@")[0]:
            user, rest = netloc.split("@", 1)[0], netloc.split("@", 1)[1]
            user_name = user.split(":", 1)[0]
            netloc = f"{user_name}:***@{rest}"
        safe = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        safe = "<unparseable>"
    print(f"[startup] DB url(safe)={safe}  DB_SSLMODE={sslmode}")
    await init_models()


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
