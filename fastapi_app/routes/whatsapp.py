"""
Webhook and API endpoints for WhatsApp interactions (Uazapi).

Mounted under WEBHOOK_PATH. Accepts both `/prefix` and `/prefix/`
(no redirect) for HEAD/GET/POST.

Auth:
- Shared secret via env WEBHOOK_VERIFY_TOKEN, accepted as:
  * Query:  ?token=YOUR_TOKEN
  * Header: X-Webhook-Token: YOUR_TOKEN
  * Meta-style (GET only): hub.verify_token=YOUR_TOKEN
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from starlette.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models.db_models import Message, User
from ..services.openai_service import ask_assistant, get_or_create_thread
from ..services.uazapi_service import send_whatsapp_message

router = APIRouter(tags=["whatsapp-webhook"])


def _env_token() -> str:
    token = os.getenv("WEBHOOK_VERIFY_TOKEN", "")
    if not token:
        print("[warn] WEBHOOK_VERIFY_TOKEN is not set; webhook will accept any request if token not enforced.")
    return token


def _extract_token_from_request(request: Request, header_token: Optional[str]) -> Optional[str]:
    if header_token:
        return header_token
    q_token = request.query_params.get("token")
    if q_token:
        return q_token
    q_hub = request.query_params.get("hub.verify_token")
    if q_hub:
        return q_hub
    return None


def _ensure_authorised(request: Request, header_token: Optional[str]) -> None:
    expected = _env_token()
    provided = _extract_token_from_request(request, header_token)
    if expected and provided != expected:
        raise HTTPException(status_code=403, detail="Invalid webhook token")


def _deep_get(dct: Dict[str, Any], path: str, default=None):
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


_phone_regex = re.compile(r"(?:^|\D)(\+?\d{10,15})(?:\D|$)")


def _only_digits(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())


def _scan_for_phone(obj: Any) -> Optional[str]:
    """
    Varre o payload procurando algo que pareça telefone.
    Preferência: strings com '@s.whatsapp.net' ou '@c.us'.
    Fallback: primeira sequência de 10-15 dígitos.
    """
    found_plain: Optional[str] = None

    def walk(x: Any):
        nonlocal found_plain
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, str):
            if "@s.whatsapp.net" in x or "@c.us" in x:
                return x.split("@")[0]
            if not found_plain:
                m = _phone_regex.search(x)
                if m:
                    found_plain = m.group(1)

    walk(obj)
    if found_plain:
        return _only_digits(found_plain)
    return None


def _extract_sender_and_type(event: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """
    Retorna: {"phone": str|None, "msg_type": str, "text": str|None}
    Cobre formatos Baileys e variações simples do Uazapi.
    """
    # ---- Baileys-like deep message ----
    msg = _deep_get(event, "data.data.messages.0") or _deep_get(event, "messages.0") or {}
    message_obj = msg.get("message", {}) if isinstance(msg, dict) else {}

    # JID / phone (Baileys)
    jid = _deep_get(msg, "key.remoteJid") or msg.get("remoteJid")
    phone = None
    if isinstance(jid, str):
        phone = jid.split("@")[0] if "@s.whatsapp.net" in jid or "@c.us" in jid else _only_digits(jid)

    # Text variants (Baileys)
    text: Optional[str] = None
    if isinstance(message_obj, dict):
        if "conversation" in message_obj:
            text = message_obj.get("conversation")
            msg_type = "text"
        elif "extendedTextMessage" in message_obj:
            text = message_obj["extendedTextMessage"].get("text")
            msg_type = "text"
        elif "textMessage" in message_obj:
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
        elif (
            "contactMessage" in message_obj
            or "contactsArrayMessage" in message_obj
            or "contacts" in message_obj
        ):
            msg_type = "vcard"
        else:
            msg_type = "unknown"
    else:
        msg_type = "unknown"

    # ---- Uazapi shapes: tenta chaves comuns no topo e dentro de 'chat' ----
    # Texto em chaves comuns
    if not text and isinstance(event.get("text"), str):
        text = event.get("text")
        msg_type = "text"
    if not text and isinstance(event.get("message"), str):
        text = event["message"]
        msg_type = "text"
    if not text and isinstance(_deep_get(event, "chat.lastText"), str):
        text = _deep_get(event, "chat.lastText")
        msg_type = "text"

    # Número em chaves comuns
    for key in ("phone", "number", "from", "chatId"):
        if not phone and isinstance(event.get(key), str):
            v = event[key]
            phone = v.split("@")[0] if "@s.whatsapp.net" in v or "@c.us" in v else _only_digits(v)

    # Número dentro de 'chat'
    for path in ("chat.chatId", "chat.remoteJid", "chat.jid", "chat.id", "chat.from"):
        if not phone:
            v = _deep_get(event, path)
            if isinstance(v, str):
                phone = v.split("@")[0] if "@s.whatsapp.net" in v or "@c.us" in v else _only_digits(v)

    # Fallback final: varre tudo
    if not phone:
        phone = _scan_for_phone(event)

    if not msg_type:
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


# ========== Accept both '' and '/' (avoid redirects) ==========
@router.head("")
@router.head("/")
async def head_check(
    request: Request,
    x_webhook_token: Optional[str] = Header(default=None, alias="X-Webhook-Token"),
) -> Response:
    _ensure_authorised(request, x_webhook_token)
    return Response(status_code=200)


@router.get("")
@router.get("/")
async def get_verify(
    request: Request,
    x_webhook_token: Optional[str] = Header(default=None, alias="X-Webhook-Token"),
) -> Response:
    _ensure_authorised(request, x_webhook_token)
    hub_challenge = request.query_params.get("hub.challenge")
    if hub_challenge is not None:
        return PlainTextResponse(hub_challenge, status_code=200, media_type="text/plain")
    return JSONResponse({"ok": True})


@router.post("")
@router.post("/")
async def webhook_post(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_webhook_token: Optional[str] = Header(default=None, alias="X-Webhook-Token"),
) -> Response:
    _ensure_authorised(request, x_webhook_token)

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"received": False, "reason": "invalid JSON"}, status_code=200)

    info = _extract_sender_and_type(payload)
    phone = info.get("phone")
    msg_type = info.get("msg_type")
    text = info.get("text")

    if not phone:
        sample = str(payload)[:400]
        print(f"[webhook] no phone extracted; sample={sample}")
        return JSONResponse({"received": True, "note": "no phone"}, status_code=200)

    push_name = _deep_get(payload, "data.data.messages.0.pushName") or _deep_get(payload, "messages.0.pushName")
    user = await _get_or_create_user(db, phone=phone, name=push_name)

    in_msg = Message(
        user_id=user.id,
        sender="user",
        content=text if msg_type == "text" else None,
        media_type=msg_type or "unknown",
        media_url=None,
    )
    db.add(in_msg)
    await db.commit()

    if msg_type == "text" and text:
        thread_id = await get_or_create_thread(user, db)
        reply_text = await ask_assistant(thread_id, text) or "Desculpe, não consegui processar sua mensagem agora."
    else:
        reply_text = "Arquivo recebido com sucesso. Já estou processando! ✅"

    out_msg = Message(user_id=user.id, sender="assistant", content=reply_text, media_type="text")
    db.add(out_msg)
    await db.commit()

    try:
        # Envio tolerante a variações do Uazapi
        await send_whatsapp_message(phone=phone, content=reply_text, type_="text")
    except Exception as e:
        print(f"[uazapi] send failed: {e!r}")

    return JSONResponse({"received": True}, status_code=200)


def get_router() -> APIRouter:
    return router