"""
Webhook e endpoints para WhatsApp (Uazapi).

Fluxo chave desta versão:
- Se o usuário responder POSITIVO após a caixinha -> envia o VÍDEO diretamente (sem IA).
- Caixinha e Vídeo têm helpers explícitos e são registrados no histórico
  com media_type = "menu" / "video", para podermos detectar o "estado"
  via banco (última interação do assistente).
- Heurísticas adicionais: se o Passo 3 vier como TEXTO ("posso te mostrar?"),
  um "sim" do lead dispara o VÍDEO mesmo sem menu recente.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from starlette.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db, SessionLocal
from ..models.db_models import Message, User
from ..services.openai_service import ask_assistant, get_or_create_thread
from ..services.uazapi_service import send_whatsapp_message, send_menu_interesse

router = APIRouter(tags=["whatsapp-webhook"])

# --------------------------- ENV (Luna) ---------------------------

def _env_str(key: str, default: str = "") -> str:
    # remove espaços e aspas extras vindas do painel do Railway
    return (os.getenv(key, default) or "").strip().strip('"').strip("'")

LUNA_MENU_YES     = _env_str("LUNA_MENU_YES", "Sim, pode continuar")
LUNA_MENU_NO      = _env_str("LUNA_MENU_NO", "Não, encerrar contato")
LUNA_MENU_TEXT    = _env_str("LUNA_MENU_TEXT", "")
LUNA_MENU_FOOTER  = _env_str("LUNA_MENU_FOOTER", "Escolha uma das opções abaixo")

LUNA_VIDEO_URL        = _env_str("LUNA_VIDEO_URL", "")
LUNA_VIDEO_CAPTION    = _env_str("LUNA_VIDEO_CAPTION", "")
LUNA_VIDEO_AFTER_TEXT = _env_str("LUNA_VIDEO_AFTER_TEXT", "")
LUNA_END_TEXT         = _env_str("LUNA_END_TEXT", "")

# Padrões textuais do convite (Passo 3) para degradar direto ao vídeo se o menu não foi enviado
_INVITE_HINTS = {
    "posso te mostrar",
    "quer ver um exemplo",
    "quer ver em 30s",
    "quer ver em 30 segundos",
    "posso enviar",
    "posso apresentar um case",
    "te mostro em 30 segundos",
    "posso te mostrar rapidamente",
    "posso te mostrar um exemplo",
    "case curto",
}

# --------------------------- Auth helpers ---------------------------

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

def _strip_accents(s: str) -> str:
    tr = str.maketrans(
        "áàãâäÁÀÃÂÄéèêÉÈÊíìîÍÌÎóòõôöÓÒÕÔÖúùûüÚÙÛÜçÇ",
        "aaaaaAAAAAeeeEEEiiiIIIoooooOOOOUuuuUUUUcC",
    )
    return s.translate(tr)

def _normalize(s: str) -> str:
    return _strip_accents((s or "").strip().lower())

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
    """
    '<digits>@s.whatsapp.net' | '<digits>@c.us' | '<digits>' (>=10)
    Ignora grupos '@g.us'.
    """
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

    if phone:
        digits = _only_digits(phone)
        if len(digits) < 10:
            print(f"[webhook] phone_too_short extracted={phone} sample={str(event)[:200]}")
            phone = None

    return {"phone": phone, "msg_type": msg_type, "text": text}

# --------------------------- POS/Fluxo helpers ---------------------------

_POSITIVE_WORDS = {
    "sim", "ok", "okay", "claro", "perfeito", "pode", "pode sim", "pode continuar",
    "vamos", "bora", "manda", "mande", "envia", "enviar", "segue", "segue sim",
    "quero", "tenho interesse", "interessa", "top", "show", "positivo", "agora",
    "mais tarde", "sim pode", "pode mandar", "pode enviar", "pode mostrar",
}
_POSITIVE_EMOJIS = {"👍", "👌", "✅", "✔️", "✌️", "🤝"}

def _looks_like_invite(text: str) -> bool:
    t = _normalize(text or "")
    return any(h in t for h in _INVITE_HINTS)

async def _has_recent_menu(session: AsyncSession, user_id: int, minutes: int = 30) -> bool:
    try:
        q = (
            select(Message)
            .where(Message.user_id == user_id, Message.sender == "assistant", Message.media_type == "menu")
            .order_by(desc(Message.created_at))
            .limit(1)
        )
        res = await session.execute(q)
        last = res.scalar_one_or_none()
        if not last or not getattr(last, "created_at", None):
            return False
        now = datetime.utcnow()
        last_at = last.created_at
        if getattr(last_at, "tzinfo", None) is not None:
            last_at = last_at.replace(tzinfo=None)
        return (now - last_at) <= timedelta(minutes=minutes)
    except Exception as exc:
        print(f"[state] erro ao consultar menu recente: {exc!r}")
        return False

async def _sent_recently(session: AsyncSession, user_id: int, media_type: str, seconds: int = 120) -> bool:
    try:
        q = (
            select(Message)
            .where(Message.user_id == user_id, Message.sender == "assistant", Message.media_type == media_type)
            .order_by(desc(Message.created_at))
            .limit(1)
        )
        res = await session.execute(q)
        last = res.scalar_one_or_none()
        if not last or not getattr(last, "created_at", None):
            return False
        now = datetime.utcnow()
        last_at = last.created_at
        if getattr(last_at, "tzinfo", None) is not None:
            last_at = last_at.replace(tzinfo=None)
        return (now - last_at) <= timedelta(seconds=seconds)
    except Exception as exc:
        print(f"[state] erro ao checar envio recente ({media_type}): {exc!r}")
        return False

def _is_positive_reply(text: Optional[str]) -> bool:
    if not text:
        return False
    t = _normalize(text)
    if t in { _normalize(LUNA_MENU_YES), "sim" }:
        return True
    if any(e in text for e in _POSITIVE_EMOJIS):
        return True
    if t in _POSITIVE_WORDS:
        return True
    # padrões úteis
    if "pode" in t or "mostra" in t or "mostrar" in t or "envia" in t or "enviar" in t or "manda" in t:
        return True
    if "video" in t or "vídeo" in t:
        return True
    return False

async def _enviar_menu(session: AsyncSession, phone: str, user: User) -> None:
    if not LUNA_MENU_TEXT:
        return
    if await _sent_recently(session, user.id, "menu", seconds=120):
        return
    try:
        await send_menu_interesse(
            phone=phone,
            text=LUNA_MENU_TEXT,
            yes_label=LUNA_MENU_YES or "Sim",
            no_label=LUNA_MENU_NO or "Não",
            footer_text=LUNA_MENU_FOOTER or None,
        )
        out = Message(user_id=user.id, sender="assistant", content=LUNA_MENU_TEXT, media_type="menu")
        session.add(out)
        await session.commit()
    except Exception as exc:
        print(f"[menu] falha ao enviar menu: {exc!r}")

async def _enviar_video(session: AsyncSession, phone: str, user: User) -> None:
    if await _sent_recently(session, user.id, "video", seconds=120):
        return
    if not LUNA_VIDEO_URL:
        await send_whatsapp_message(phone=phone, content="Desculpe, não consigo mostrar vídeos no momento.", type_="text")
        return
    try:
        await send_whatsapp_message(
            phone=phone,
            content=LUNA_VIDEO_CAPTION or "",
            type_="media",
            media_url=LUNA_VIDEO_URL,
            caption=LUNA_VIDEO_CAPTION or "",
        )
        session.add(Message(user_id=user.id, sender="assistant", content=LUNA_VIDEO_URL, media_type="video"))
        await session.commit()
        if LUNA_VIDEO_AFTER_TEXT:
            await send_whatsapp_message(phone=phone, content=LUNA_VIDEO_AFTER_TEXT, type_="text")
            session.add(Message(user_id=user.id, sender="assistant", content=LUNA_VIDEO_AFTER_TEXT, media_type="text"))
            await session.commit()
    except Exception as exc:
        print(f"[video] falha ao enviar vídeo: {exc!r}")

def _parse_tool_hints(reply_text: str) -> Tuple[bool, bool]:
    if not reply_text:
        return (False, False)
    t = reply_text.lower()
    wants_menu = "enviar_caixinha_interesse" in t or ("caixinha" in t and "enviar" in t)
    wants_video = "enviar_video" in t or ("enviar" in t and "vídeo" in t) or ("mandar" in t and "vídeo" in t)
    return (wants_menu, wants_video)

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

# --------------------------- processamento assíncrono ---------------------------
async def _process_message_async(phone: str, msg_type: str, text: Optional[str], push_name: Optional[str]) -> None:
    """Processa a mensagem fora do ciclo do request para evitar timeouts/499."""
    try:
        async with SessionLocal() as session:
            res = await session.execute(select(User).where(User.phone == phone))
            user = res.scalar_one_or_none()
            if not user:
                user = User(phone=phone, name=push_name or None)
                session.add(user)
                await session.commit()
                await session.refresh(user)

            # 0) Snapshot do último texto do assistente
            q_last = (
                select(Message)
                .where(Message.user_id == user.id, Message.sender == "assistant")
                .order_by(desc(Message.created_at))
                .limit(1)
            )
            r_last = await session.execute(q_last)
            last_assistant: Optional[Message] = r_last.scalar_one_or_none()
            last_text = _normalize((last_assistant.content if last_assistant else "") or "")

            # 1) Guard-rail: se resposta POSITIVA após caixinha -> envia VÍDEO e encerra
            if msg_type == "text" and _is_positive_reply(text) and await _has_recent_menu(session, user.id, minutes=30):
                print("[flow] positivo + menu recente => enviar vídeo")
                await _enviar_video(session, phone, user)
                return

            # 1.1) Degradação elegante: se a última fala do assistente for CONVITE (passo 3),
            # e o usuário disser "sim", enviamos o VÍDEO mesmo sem menu.
            if msg_type == "text" and _is_positive_reply(text) and _looks_like_invite(last_text):
                print("[flow] positivo + convite textual => enviar vídeo (sem menu)")
                await _enviar_video(session, phone, user)
                return

            # 1.2) Fallback determinístico: positivo após passo 2 => envia CAIXINHA
            if msg_type == "text" and _is_positive_reply(text) and not await _has_recent_menu(session, user.id, minutes=30):
                step2_hints = [
                    "responsavel pelo marketing",
                    "parte de marketing/comunicacao",
                    "divulgacao da empresa",
                    "divulgacao e video",
                    "acoes de marketing",
                ]
                if any(h in last_text for h in step2_hints):
                    print("[flow] positivo + passo2 => enviar caixinha")
                    await _enviar_menu(session, phone, user)
                    return

            # 2) Caso contrário, IA
            if msg_type == "text" and text:
                thread_id = await get_or_create_thread(session, user)
                reply_text = await ask_assistant(thread_id, text)
                if not reply_text:
                    reply_text = "Desculpe, não consegui processar sua mensagem agora."

                send_menu_hint, send_video_hint = _parse_tool_hints(reply_text)

                # Se a IA pedir explicitamente
                if send_menu_hint:
                    print("[flow] IA pediu caixinha")
                    await _enviar_menu(session, phone, user)
                    return

                if send_video_hint:
                    print("[flow] IA pediu vídeo")
                    await _enviar_video(session, phone, user)
                    return

                # Heurística: se a própria resposta da IA for um convite, não mande texto – mande caixinha.
                if _looks_like_invite(reply_text):
                    print("[flow] resposta IA parece convite => enviar caixinha")
                    await _enviar_menu(session, phone, user)
                    return

                # Texto normal
                try:
                    await send_whatsapp_message(phone=phone, content=reply_text, type_="text")
                except Exception as e:
                    print(f"[uazapi] send failed (bg): {e!r}")

                out_msg = Message(user_id=user.id, sender="assistant", content=reply_text, media_type="text")
                session.add(out_msg)
                await session.commit()
                return

            # 3) Mensagens não-texto (áudio/imagem etc.)
            ack = "Arquivo recebido com sucesso. Já estou processando! ✅"
            try:
                await send_whatsapp_message(phone=phone, content=ack, type_="text")
            except Exception as e:
                print(f"[uazapi] send failed (bg): {e!r}")
            out_msg = Message(user_id=user.id, sender="assistant", content=ack, media_type="text")
            session.add(out_msg)
            await session.commit()

    except Exception as exc:
        print(f"[bg] unexpected error: {exc!r}")

# --------------------------- webhook ---------------------------
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

    # Persist inbound
    res = await db.execute(select(User).where(User.phone == phone))
    user = res.scalar_one_or_none()
    if not user:
        user = User(phone=phone, name=push_name or None)
        db.add(user)
        await db.commit()
        await db.refresh(user)

    in_msg = Message(
        user_id=user.id,
        sender="user",
        content=text if msg_type == "text" else None,
        media_type=msg_type or "unknown",
        media_url=None,
    )
    db.add(in_msg)
    await db.commit()

    # Processamento em segundo plano
    asyncio.create_task(_process_message_async(phone=phone, msg_type=msg_type, text=text, push_name=push_name))
    return JSONResponse({"received": True}, status_code=200)

def get_router() -> APIRouter:
    return router
