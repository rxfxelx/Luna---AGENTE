"""
Webhook e endpoints para integrações WhatsApp (Uazapi).

- Montado sob WEBHOOK_PATH.
- HEAD/GET: verificação (com token).
- POST: recebe webhook, persiste mensagem, processa em background e responde 200 rápido.

Anti‑loop: ignora mensagens 'fromMe'.
Extração de telefone: JIDs + varredura.
Menu opcional: se habilitado via ENV, envia /send/menu quando detectar confirmação curta.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from starlette.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db, SessionLocal
from ..models.db_models import Message, User
from ..services.openai_service import ask_assistant, get_or_create_thread
from ..services.uazapi_service import send_whatsapp_message, send_menu

router = APIRouter(tags=["whatsapp-webhook"])

# ---------------- ENV de menu/botões ----------------
AUTO_MENU_IF_POSITIVE = os.getenv("UAZAPI_AUTO_MENU_IF_POSITIVE", "true").lower() == "true"
MENU_TEXT = os.getenv("UAZAPI_MENU_TEXT", "Posso te mostrar rapidamente um exemplo objetivo do que fazemos?")
MENU_CHOICES = [opt.strip() for opt in os.getenv(
    "UAZAPI_MENU_CHOICES", "Sim, pode continuar|Não, encerrar contato"
).split("|") if opt.strip()]
MENU_FOOTER = os.getenv("UAZAPI_MENU_FOOTER", "Escolha uma das opções abaixo")
MENU_POSITIVE_WORDS = [w.strip().lower() for w in os.getenv(
    "UAZAPI_MENU_POSITIVE_WORDS", "sim|ok|claro|pode|pode continuar|quero|sou eu"
).split("|") if w.strip()]

# ---------------- Auth helpers ----------------
def _env_token() -> str:
    token = os.getenv("WEBHOOK_VERIFY_TOKEN", "")
    if not token:
        print("[warn] WEBHOOK_VERIFY_TOKEN não definido.")
    return token

def _extract_token_from_request(request: Request, header_token: Optional[str]) -> Optional[str]:
    if header_token:
        return header_token
    return request.query_params.get("token") or request.query_params.get("hub.verify_token")

def _ensure_authorised(request: Request, header_token: Optional[str]) -> None:
    expected = _env_token()
    provided = _extract_token_from_request(request, header_token)
    if expected and provided != expected:
        raise HTTPException(status_code=403, detail="Invalid webhook token")

# ---------------- Payload helpers ----------------
_phone_regex = re.compile(r"(?:^|\D)(\+?\d{10,15})(?:\D|$)")

TEXT_KEYS_PRIORITY = (
    # Baileys-like
    "data.data.messages.0.message.conversation",
    "data.data.messages.0.message.extendedTextMessage.text",
    "messages.0.message.conversation",
    "messages.0.message.extendedTextMessage.text",
    # Uazapi simples
    "messages.0.text",
    "data.text",
    "data.message",
    "data.body",
    "text",
    "message",
    "body",
    "content",
    "caption",
)

def _only_digits(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())

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

def _norm_phone_from_jid(value: Any) -> Optional[str]:
    """Extrai <digits> de '<digits>@s.whatsapp.net' | '<digits>@c.us' | '<digits>' (>=10). Ignora grupos."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if "@g.us" in v:
        return None
    if "@s.whatsapp.net" in v or "@c.us" in v:
        return v.split("@")[0]
    digits = _only_digits(v)
    return digits if len(digits) >= 10 else None

def _scan_for_phone(obj: Any) -> Optional[str]:
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
                p = x.split("@")[0]
                if len(_only_digits(p)) >= 10:
                    found_plain = p
                    return
            if not found_plain:
                m = _phone_regex.search(x)
                if m:
                    found_plain = m.group(1)
    walk(obj)
    return _only_digits(found_plain) if found_plain else None

def _extract_text_generic(event: Dict[str, Any]) -> Optional[str]:
    for path in TEXT_KEYS_PRIORITY:
        val = _deep_get(event, path)
        if isinstance(val, str) and val.strip():
            return val.strip()
    keys = {"text", "message", "body", "content", "caption", "conversation"}
    found: Optional[str] = None
    def walk(x: Any):
        nonlocal found
        if found:
            return
        if isinstance(x, dict):
            for k, v in x.items():
                if isinstance(v, str) and k.lower() in keys and v.strip():
                    found = v.strip()
                    return
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(event)
    return found

def _is_from_me(event: Dict[str, Any]) -> bool:
    for path in (
        "data.data.messages.0.key.fromMe",
        "messages.0.key.fromMe",
        "fromMe",
        "data.fromMe",
        "message.fromMe",
    ):
        v = _deep_get(event, path)
        if isinstance(v, bool) and v:
            return True
    return False

def _extract_sender_and_type(event: Dict[str, Any]) -> Dict[str, Optional[str]]:
    msg = _deep_get(event, "data.data.messages.0") or _deep_get(event, "messages.0") or {}
    message_obj = msg.get("message", {}) if isinstance(msg, dict) else {}

    phone: Optional[str] = None
    for p in (
        _deep_get(msg, "key.remoteJid"),
        msg.get("remoteJid") if isinstance(msg, dict) else None,
        _deep_get(event, "chat.chatId"),
        _deep_get(event, "chat.remoteJid"),
        _deep_get(event, "key.remoteJid"),
    ):
        phone = _norm_phone_from_jid(p)
        if phone:
            break

    is_group = False
    for maybe in (_deep_get(msg, "key.remoteJid"), msg.get("remoteJid") if isinstance(msg, dict) else None):
        if isinstance(maybe, str) and "@g.us" in maybe:
            is_group = True
            break
    if not phone and is_group:
        for p in (_deep_get(msg, "key.participant"), _deep_get(event, "participant"), _deep_get(event, "author")):
            phone = _norm_phone_from_jid(p)
            if phone:
                break

    if not phone:
        for key in ("chatId", "from", "phone", "number"):
            p = event.get(key)
            cand = _norm_phone_from_jid(p)
            if not cand and isinstance(p, str):
                digits = _only_digits(p)
                cand = digits if len(digits) >= 10 else None
            if cand:
                phone = cand
                break

    if not phone:
        p = _deep_get(event, "chat.id")
        norm = _norm_phone_from_jid(p)
        if norm:
            phone = norm

    if not phone:
        phone = _scan_for_phone(event)

    # tipo/texto
    text = _extract_text_generic(event)
    if text:
        msg_type = "text"
    else:
        if _deep_get(event, "data.data.messages.0.message.imageMessage") or event.get("image"):
            msg_type = "image"
        elif _deep_get(event, "data.data.messages.0.message.videoMessage") or event.get("video"):
            msg_type = "video"
        elif _deep_get(event, "data.data.messages.0.message.audioMessage") or event.get("audio"):
            msg_type = "audio"
        elif _deep_get(event, "data.data.messages.0.message.documentMessage") or event.get("document"):
            msg_type = "pdf"
        else:
            msg_type = "unknown"

    if phone and len(_only_digits(phone)) < 10:
        print(f"[webhook] phone_too_short extracted={phone} sample={str(event)[:200]}")
        phone = None

    return {"phone": phone, "msg_type": msg_type, "text": text}

# ---------------- DB helpers ----------------
async def _get_or_create_user(session: AsyncSession, phone: str, name: Optional[str]) -> User:
    res = await session.execute(select(User).where(User.phone == phone))
    user = res.scalar_one_or_none()
    if not user:
        user = User(phone=phone, name=name or None)
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user

# ---------------- HEAD/GET sem redirect ----------------
@router.head("")
@router.head("/")
async def head_check(request: Request, x_webhook_token: Optional[str] = Header(default=None, alias="X-Webhook-Token")) -> Response:
    _ensure_authorised(request, x_webhook_token)
    return Response(status_code=200)

@router.get("")
@router.get("/")
async def get_verify(request: Request, x_webhook_token: Optional[str] = Header(default=None, alias="X-Webhook-Token")) -> Response:
    _ensure_authorised(request, x_webhook_token)
    hub_challenge = request.query_params.get("hub.challenge")
    if hub_challenge is not None:
        return PlainTextResponse(hub_challenge, status_code=200, media_type="text/plain")
    return JSONResponse({"ok": True})

# ---------------- Background worker ----------------
async def _process_message_async(phone: str, msg_type: str, text: Optional[str], push_name: Optional[str]) -> None:
    """Processa fora do request para evitar timeouts (499)."""
    try:
        async with SessionLocal() as session:
            user = await _get_or_create_user(session, phone=phone, name=push_name)

            # Menu automático por ENV para confirmações curtas
            sent_menu = False
            if AUTO_MENU_IF_POSITIVE and msg_type == "text" and text:
                low = text.strip().lower()
                if any(p in low for p in MENU_POSITIVE_WORDS):
                    try:
                        await send_menu(phone=phone, text=MENU_TEXT, choices=MENU_CHOICES, footer=MENU_FOOTER)
                        out_menu = Message(user_id=user.id, sender="assistant", content=MENU_TEXT, media_type="text")
                        session.add(out_menu)
                        await session.commit()
                        sent_menu = True
                    except Exception as e:
                        print(f"[uazapi] send_menu failed: {e!r}")

            if not sent_menu:
                if msg_type == "text" and text:
                    thread_id = await get_or_create_thread(session, user)  # (session, user)
                    reply_text = await ask_assistant(thread_id, text) or "Desculpe, não consegui processar sua mensagem agora."
                else:
                    reply_text = "Arquivo recebido com sucesso. Já estou processando! ✅"

                out_msg = Message(user_id=user.id, sender="assistant", content=reply_text, media_type="text")
                session.add(out_msg)
                await session.commit()

                try:
                    await send_whatsapp_message(phone=phone, content=reply_text, type_="text")
                except Exception as e:
                    print(f"[uazapi] send failed (bg): {e!r}")

    except Exception as exc:
        print(f"[bg] unexpected error: {exc!r}")

# ---------------- POST webhook ----------------
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

    # Evita loop
    if _is_from_me(payload):
        return JSONResponse({"received": True, "note": "from_me"}, status_code=200)

    info = _extract_sender_and_type(payload)
    phone = info.get("phone")
    msg_type = info.get("msg_type") or "unknown"
    text = info.get("text")

    print(f"[webhook] extracted phone={phone} type={msg_type} text_len={(len(text) if text else 0)}")

    if not phone:
        print(f"[webhook] no phone extracted; sample={str(payload)[:400]}")
        return JSONResponse({"received": True, "note": "no phone"}, status_code=200)

    push_name = _deep_get(payload, "data.data.messages.0.pushName") or _deep_get(payload, "messages.0.pushName")

    # registra entrada rapidamente
    user = await _get_or_create_user(db, phone=phone, name=push_name)
    in_msg = Message(
        user_id=user.id,
        sender="user",
        content=text if msg_type == "text" else None,
        media_type=msg_type,
        media_url=None,
    )
    db.add(in_msg)
    await db.commit()

    # processa em background e responde 200 imediato
    asyncio.create_task(_process_message_async(phone=phone, msg_type=msg_type, text=text, push_name=push_name))
    return JSONResponse({"received": True}, status_code=200)

def get_router() -> APIRouter:
    return router
