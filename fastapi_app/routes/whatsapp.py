"""
Webhook e endpoints para WhatsApp (Uazapi).

- Montado sob WEBHOOK_PATH. Aceita '' e '/' para evitar 307.
- Autorização: WEBHOOK_VERIFY_TOKEN via query/header.
- Guarda trilhas no banco e processa a lógica em background para evitar 499.

Guard-rails:
- Se o usuário confirmar que é responsável (ex.: "sim, sou eu"),
  enviamos a CAIXINHA de interesse (menu) mesmo sem a IA chamar tool.
- Se o usuário clicar/digitar exatamente o rótulo do botão "Sim",
  enviamos o VÍDEO imediatamente.
- Se clicar "Não", encerramos com texto de despedida (LUNA_END_TEXT).
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
from ..services.uazapi_service import (
    send_whatsapp_message,
    send_menu,
    LUNA_MENU_YES, LUNA_MENU_NO,
    LUNA_VIDEO_URL, LUNA_VIDEO_CAPTION, LUNA_VIDEO_AFTER_TEXT,
    LUNA_END_TEXT,
)

router = APIRouter(tags=["whatsapp-webhook"])

# --------------------------- Auth helpers ---------------------------

def _env_token() -> str:
    token = os.getenv("WEBHOOK_VERIFY_TOKEN", "")
    if not token:
        print("[warn] WEBHOOK_VERIFY_TOKEN não está definido.")
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

# --------------------------- Payload helpers ---------------------------

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
    for path in ("data.data.messages.0.key.fromMe", "messages.0.key.fromMe", "fromMe", "data.fromMe", "message.fromMe"):
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

    if _deep_get(event, "data.data.messages.0.message.imageMessage") or event.get("image"):
        msg_type = "image"
    elif _deep_get(event, "data.data.messages.0.message.videoMessage") or event.get("video"):
        msg_type = "video"
    elif _deep_get(event, "data.data.messages.0.message.audioMessage") or event.get("audio"):
        msg_type = "audio"
    elif _deep_get(event, "data.data.messages.0.message.documentMessage") or event.get("document"):
        msg_type = "pdf"
    else:
        msg_type = "text" if _extract_text_generic(event) else "unknown"

    text = _extract_text_generic(event)

    if phone:
        digits = _only_digits(phone)
        if len(digits) < 10:
            print(f"[webhook] phone_too_short extracted={phone} sample={str(event)[:200]}")
            phone = None

    return {"phone": phone, "msg_type": msg_type, "text": text}

def _norm_text(s: Optional[str]) -> str:
    return (s or "").strip().lower()

# --------------------------- DB helpers ---------------------------

async def _get_or_create_user(session: AsyncSession, phone: str, name: Optional[str]) -> User:
    res = await session.execute(select(User).where(User.phone == phone))
    user = res.scalar_one_or_none()
    if not user:
        user = User(phone=phone, name=name or None)
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user

# ================== endpoints ('' e '/') para evitar 307 ==================

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

# --------------------------- Processamento assíncrono ---------------------------

async def _process_message_async(phone: str, msg_type: str, text: Optional[str], push_name: Optional[str]) -> None:
    """Processa a mensagem fora do ciclo do request para evitar timeouts/499."""
    try:
        async with SessionLocal() as session:
            user = await _get_or_create_user(session, phone=phone, name=push_name)

            if msg_type == "text" and text:
                t = _norm_text(text)

                # ------- GUARD-RAILS -------
                # Botão "Sim" => envia vídeo
                if LUNA_VIDEO_URL and t == _norm_text(LUNA_MENU_YES):
                    try:
                        await send_whatsapp_message(
                            phone=phone, content=LUNA_VIDEO_CAPTION or "", type_="media",
                            media_url=LUNA_VIDEO_URL, caption=LUNA_VIDEO_CAPTION or "",
                        )
                        if LUNA_VIDEO_AFTER_TEXT:
                            await send_whatsapp_message(phone=phone, content=LUNA_VIDEO_AFTER_TEXT, type_="text")
                        # registra
                        out1 = Message(user_id=user.id, sender="assistant", content=f"[vídeo] {LUNA_VIDEO_CAPTION}", media_type="video", media_url=LUNA_VIDEO_URL)
                        session.add(out1)
                        if LUNA_VIDEO_AFTER_TEXT:
                            session.add(Message(user_id=user.id, sender="assistant", content=LUNA_VIDEO_AFTER_TEXT, media_type="text"))
                        await session.commit()
                        return
                    except Exception as e:
                        print(f"[guard] falha ao enviar vídeo direto: {e!r}")

                # Botão "Não" => encerra
                if t == _norm_text(LUNA_MENU_NO):
                    if LUNA_END_TEXT:
                        try:
                            await send_whatsapp_message(phone=phone, content=LUNA_END_TEXT, type_="text")
                            session.add(Message(user_id=user.id, sender="assistant", content=LUNA_END_TEXT, media_type="text"))
                            await session.commit()
                        except Exception as e:
                            print(f"[guard] falha ao enviar encerramento: {e!r}")
                    return

                # Confirma responsável => envia caixinha
                RESP_SIM = {"sim", "sim.", "sim eu", "sou eu", "sim sou eu", "ok", "claro", "pode", "pode sim", "sim, sou eu"}
                if t in RESP_SIM or "sou eu" in t:
                    try:
                        await send_menu(phone=phone)
                        session.add(Message(user_id=user.id, sender="assistant", content=f"[menu] {LUNA_MENU_YES} | {LUNA_MENU_NO}", media_type="text"))
                        await session.commit()
                        return
                    except Exception as e:
                        print(f"[guard] falha ao enviar caixinha: {e!r}")
                # ------- FIM GUARD-RAILS -------

                # IA (Assistants/Chat) — com prompt injetado no run via serviço
                thread_id = await get_or_create_thread(session, user)
                reply_text = await ask_assistant(thread_id, text) or "Desculpe, não consegui processar sua mensagem agora."
            else:
                reply_text = "Arquivo recebido com sucesso. Já estou processando! ✅"

            # salva resposta da IA
            out_msg = Message(user_id=user.id, sender="assistant", content=reply_text, media_type="text")
            session.add(out_msg)
            await session.commit()

            # envia via Uazapi
            try:
                await send_whatsapp_message(phone=phone, content=reply_text, type_="text")
            except Exception as e:
                print(f"[uazapi] send failed (bg): {e!r}")

    except Exception as exc:
        print(f"[bg] unexpected error: {exc!r}")

# --------------------------- Webhook principal ---------------------------

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

    # Evita loops
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

    # Log de entrada rápido no banco
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

    # Processamento em background + ACK imediato
    asyncio.create_task(_process_message_async(phone=phone, msg_type=msg_type, text=text, push_name=push_name))
    return JSONResponse({"received": True}, status_code=200)

def get_router() -> APIRouter:
    return router