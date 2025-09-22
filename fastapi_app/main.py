"""
Entry point for the FastAPI application.

Includes startup diagnostics (sanitised) and mounts the WhatsApp webhook
router under a path defined by the WEBHOOK_PATH environment variable.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI

from .db import init_models
from .routes import get_whatsapp_router

app = FastAPI(title="Luna Backend (Uazapi + OpenAI)")


def _normalise_prefix(p: str) -> str:
    if not p:
        return "/webhook/whatsapp"
    p = p.strip()
    if not p.startswith("/"):
        p = "/" + p
    # remove trailing slash (we serve '/' inside the router)
    if len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    return p


@app.on_event("startup")
async def on_startup() -> None:
    # ---- Diagnostics (safe) ----
    db_url_raw = os.getenv("DATABASE_URL", "")
    sslmode = os.getenv("DB_SSLMODE", "require")
    try:
        parts = urlsplit(db_url_raw)
        netloc = parts.netloc
        if "@" in netloc and ":" in netloc.split("@")[0]:
            user, rest = netloc.split("@", 1)[0], netloc.split("@", 1)[1]
            user_name = user.split(":", 1)[0]
            netloc = f"{user_name}:***@{rest}"
        safe_db = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        safe_db = "<unparseable>"

    public_base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    webhook_path = _normalise_prefix(os.getenv("WEBHOOK_PATH", "/webhook/whatsapp"))
    print(f"[startup] DB url(safe)={safe_db}  DB_SSLMODE={sslmode}")
    if public_base:
        print(f"[startup] Webhook URL = {public_base}{webhook_path}")
    else:
        print(f"[startup] Webhook PATH = {webhook_path} (defina PUBLIC_BASE_URL para ver URL completa)")

    await init_models()


# ---- Routers (mount after startup helpers are defined) ----
webhook_prefix = _normalise_prefix(os.getenv("WEBHOOK_PATH", "/webhook/whatsapp"))
app.include_router(get_whatsapp_router(), prefix=webhook_prefix)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
