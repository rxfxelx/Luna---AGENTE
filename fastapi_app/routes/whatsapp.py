"""
Webhook and API endpoints for WhatsApp interactions (Uazapi).

This router is mounted under a configurable prefix (WEBHOOK_PATH),
and exposes:
  - HEAD   /   -> quick auth check (requires token)
  - GET    /   -> verify endpoint (supports ?token=..., and Meta-style hub.challenge)
  - POST   /   -> webhook receiver (requires token), processes messages

Auth:
- Provide a shared secret in env WEBHOOK_VERIFY_TOKEN.
- We accept it via:
    * Query:  ?token=YOUR_TOKEN
    * Header: X-Webhook-Token: YOUR_TOKEN
    * Meta-style: hub.verify_token=YOUR_TOKEN (GET only)
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from starlette.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models.db_models import Message, User
from ..services.openai_service import ask_assistant, get_or_create_thread
from ..services.uazapi_service import send_whatsapp_message

# Router without prefix; main.py mounts it with the WEBHOOK_PATH env
router = APIRouter(tags=["whatsapp-webhook"])


def _env_token() -> str:
    token = os.getenv("WEBHOOK_VERIFY_TOKEN", "")
    if not token:
        # Allow running without token in local dev, but warn loudly
        print("[warn] WEBHOOK_VERIFY_TOKEN is not set; webhook will accept any request if token not enforced.")
    return token


def _extract_token_from_request(request: Request, header_token: Optional[str]) -> Optional[str]:
    # 1) Header first
    if header_token:
        return header_token
    # 2) Query param ?token=...
    q_token = request.query_params.get("token")
    if q_token:
        return q_token
    # 3) Meta-style verify token (GET only): hub.verify_token
    q_hub = request.query_params.get("hub.verify_token")
    if q_hub:
        return q_hub
    return None


def _ensure_authorised(request: Request, header_token: Optional[str]) -> None:
    expected = _env_token()
    provided = _extract_token_from_request(request, header_token)
    # If expected is set, enforce match
    if expected and provided != expected:
        raise HTTPException(status_code=403, detail="Invalid webhook token")


@router.head("/")
async def head_check(
    request: Request,
    x_webhook_token: Optional[str] = Header(default=None, alias="X-Webhook-Token"),
) -> Response:
    _ensure_authorised(request, x_webhook_token)
    return Response(status_code=200)


@router.get("/")
async def get_verify(
    request: Request,
    x_webhook_token: Optional[str] = Header(default=None, alias="X-Webhook-Token"),
) -> Response:
    """
    Supports two verification styles:
    - Simple token check:  GET /?token=YOUR_TOKEN  -> {"ok": true}
    - Meta-style hub.challenge:
        GET /?hub.mode=subscribe&hub.verify_token=YOUR_TOKEN&hub.challenge=XYZ -> returns 'XYZ'
    """
    _ensure_authorised(request, x_webhook_token)

    # Meta-style handshake
    hub_challenge = request.query_params.get("hub.challenge")
    if hub_challenge is not None:
        return PlainTextResponse(hub_challenge, status_code=200, media_type="text/plain")

    return JSONResponse({"ok": True})


def _deep_get(dct: Dict[str, Any], path: str, default=None):
    """
    Safe nested key access using dot-path (e.g. "data.data.messages.0.message").
    """
    cur: Any = dct
    for part in path.split("."):
        if isinstance(cur, list):
            try:
                idx = int(part)
            except Exception:
                return default
            if idx < 0 or idx >= len(cur):
                return default
            cur = cur[idx]
        elif isinstance(cur, dict):
            if part not in cur:
                return default
            cur = cur[part]
        else:
            return default
    return cur


def _extract_sender_and_type(event: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """
    Try to be resilient to different shapes (Baileys/Uazapi variations).
    Returns dict with keys: phone, msg_type, text (when applicable)
    """
    msg = _deep_get(event, "data.data.messages.0") or _deep_get(event, "messages.0") or {}
    message_obj = msg.get("message", {}) if isinstance(msg, dict) else {}
    # phone (remoteJid style: "5511999999999@s.whatsapp.net")
    jid = _deep_get(msg, "key.remoteJid") or msg.get("remoteJid")
    phone = jid.split("@")[0] if isinstance(jid, str) and "@s.whatsapp.net" in jid else jid

    # text variants
    text: Optional[str] = None
    if "conversation" in message_obj:
        text = message_obj.get("conversation")
        msg_type = "text"
    elif "extendedTextMessage" in message_obj:
        text = message_obj["extendedTextMessage"].get("text")
        msg_type = "text"
    elif "textMessage" in message_obj:
        # alguns payloads podem colocar diretamente a string
        raw = message_obj.get("textMessage")
        text = raw if isinstance(raw, str) else (raw.get("text") if isinstance(raw, dict) else None)
        msg_type = "text"
    elif "imageMessage" in message_obj:
        msg_type = "image"
    elif "videoMessage" in message_obj:
        msg_type = "video"
    elif "audioMessage" in message_obj:
        msg_type = "audio"
    elif "documentMessage" in message_obj:
        msg_type = "pdf"
    elif "contactMessage" in message_obj or "contactsArrayMessage" in message_obj or "contacts" in message_obj:
        msg_type = "vcard"
    else:
        msg_type = "unknown"

    return {"phone": phone, "msg_type": msg_type, "text": text}


async def _get_or_create_user(session: AsyncSession, phone: str, name: Optional[str]) -> User:
    res = await session.execute(select(User).where(User.phone == phone))
    user = res.scalar_one_or_none()
    if not user:
        user = User(phone=phone, name=name or None)
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user


@router.post("/")
async def webhook_post(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_webhook_token: Optional[str] = Header(default=None, alias="X-Webhook-Token"),
) -> Response:
    """
    Receives WhatsApp events from Uazapi.
    - Auth via token (header/query).
    - Persists message.
    - For text messages: calls OpenAI and replies via Uazapi.
    """
    _ensure_authorised(request, x_webhook_token)

    try:
        payload = await request.json()
    except Exception:
        # Always ack to avoid retries from provider
        return JSONResponse({"received": False, "reason": "invalid JSON"}, status_code=200)

    info = _extract_sender_and_type(payload)
    phone = info.get("phone")
    msg_type = info.get("msg_type")
    text = info.get("text")

    if not phone:
        # still ack to avoid retries
        return JSONResponse({"received": True, "note": "no phone"}, status_code=200)

    push_name = _deep_get(payload, "data.data.messages.0.pushName") or _deep_get(payload, "messages.0.pushName")
    user = await _get_or_create_user(db, phone=phone, name=push_name)

    # Save incoming message
    in_msg = Message(
        user_id=user.id,
        sender="user",
        content=text if msg_type == "text" else None,
        media_type=msg_type or "unknown",
        media_url=None,  # pode ser preenchido se usarmos download/upload
    )
    db.add(in_msg)
    await db.commit()

    # Decide what to do
    if msg_type == "text" and text:
        # Ask OpenAI
        thread_id = await get_or_create_thread(user, db)  # persist thread_id no user
        reply_text = await ask_assistant(thread_id, text)
        if not reply_text:
            reply_text = "Desculpe, não consegui processar sua mensagem agora."
    else:
        # Acknowledge non-text
        reply_text = "Arquivo recebido com sucesso. Já estou processando! ✅"

    # Save assistant message
    out_msg = Message(user_id=user.id, sender="assistant", content=reply_text, media_type="text")
    db.add(out_msg)
    await db.commit()

    # Send back via Uazapi
    try:
        await send_whatsapp_message(phone=phone, content=reply_text, type_="text")
    except Exception as e:
        # Log only; never break webhook ack
        print(f"[uazapi] send failed: {e!r}")

    return JSONResponse({"received": True}, status_code=200)


def get_router() -> APIRouter:
    """Factory used by main.py to mount this router under WEBHOOK_PATH."""
    return router
