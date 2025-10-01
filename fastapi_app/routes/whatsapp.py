"""
Webhook e endpoints para WhatsApp (Uazapi).

Fluxo resumido:
- Encaminha texto do lead para a IA (Assistant) COM contexto de estado (caixinha/vídeo) + phone do lead.
- Envia CAIXINHA/VÍDEO quando a IA solicitar (tool-hints por texto ou por tag #tools()).
- Atalho local: após a caixinha, interpreta SIM/NÃO sem chamar a IA (responde na hora).
- Fallback opcional: vídeo após SIM na caixinha (configurável).
- Handoff: agora é por CONSENTIMENTO — só notifica consultores após o lead aceitar.
- Deduplicação do inbound (mesmo conteúdo ≤ 5s) e anti-duplicação de ações + lock por usuário.
"""

from __future__ import annotations

import asyncio
import os
import re
import unicodedata
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple, List

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from starlette.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db, SessionLocal
from ..models.db_models import Message, User
from ..services.openai_service import ask_assistant, get_or_create_thread
from ..services.uazapi_service import send_whatsapp_message, send_menu_interesse

router = APIRouter(tags=["whatsapp-webhook"])

# =========================== ENV & helpers ===========================

def _env_str(key: str, default: str = "") -> str:
    # remove aspas e espaços extras vindos do painel (Railway etc.)
    return (os.getenv(key, default) or "").strip().strip('"').strip("'")

def _env_template(key: str, default: str = "") -> str:
    """
    Lê um template de ENV e normaliza:
      - remove prefixo acidental "<KEY>=" se o usuário colou junto
      - retira aspas de borda
      - converte literais \n, \r, \t em quebras reais
    """
    raw = os.getenv(key, default) or ""
    raw = raw.strip()
    raw = re.sub(rf'^\s*{re.escape(key)}\s*=\s*', '', raw, flags=re.I)
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        raw = raw[1:-1]
    raw = raw.replace("\\n", "\n").replace("\\r", "\r").replace("\\t", "\t")
    return raw

LUNA_MENU_YES     = _env_str("LUNA_MENU_YES", "Sim, pode continuar")
LUNA_MENU_NO      = _env_str("LUNA_MENU_NO", "Não, encerrar contato")
LUNA_MENU_TEXT    = _env_str("LUNA_MENU_TEXT", "")
LUNA_MENU_FOOTER  = _env_str("LUNA_MENU_FOOTER", "Escolha uma das opções abaixo")

LUNA_VIDEO_URL        = _env_str("LUNA_VIDEO_URL", "")
LUNA_VIDEO_CAPTION    = _env_str("LUNA_VIDEO_CAPTION", "")
LUNA_VIDEO_AFTER_TEXT = _env_str("LUNA_VIDEO_AFTER_TEXT", "")
LUNA_END_TEXT         = _env_str("LUNA_END_TEXT", "")

# Se "true", NÃO usa fallback automático para vídeo após caixinha SIM (100% IA decide)
LUNA_STRICT_ASSISTANT = _env_str("LUNA_STRICT_ASSISTANT", "false").lower() == "true"

# Notificação externa (handoff)
HANDOFF_NOTIFY_NUMBERS = _env_str("HANDOFF_NOTIFY_NUMBERS", "")  # "5531999999999,5531888888888"
HANDOFF_NOTIFY_TEMPLATE = _env_template(
    "HANDOFF_NOTIFY_TEMPLATE",
    "Novo lead aguardando contato (Luna — Verbo Vídeo)\n"
    "Nome: {name}\nTelefone: +{digits}\nÚltima mensagem: {last}\nOrigem: WhatsApp\n"
    "Link: {wa_link}"
)

# Mensagens do convite/consentimento de handoff
HANDOFF_CONSULTOR_NAME   = _env_str("HANDOFF_CONSULTOR_NAME", "nosso consultor criativo")
HANDOFF_OFFER_TEMPLATE   = _env_template(
    "HANDOFF_OFFER_TEMPLATE",
    "Perfeito, anotei: *{formato}*. "
    "Posso te passar para {consultor}, que pode mostrar formatos e orçamentos sob medida? "
    "Prefere que ele fale agora ou mais tarde?"
)
HANDOFF_CONFIRM_TEMPLATE = _env_template(
    "HANDOFF_CONFIRM_TEMPLATE",
    "Perfeito! Estou te passando para {consultor} agora. Ele vai te chamar neste número em instantes. 👍"
)
HANDOFF_LATER_TEMPLATE   = _env_template(
    "HANDOFF_LATER_TEMPLATE",
    "Combinado! Aviso {consultor}. Quando quiser falar **agora**, diga “agora” aqui que eu aciono."
)

# >>> Coleta de nome (quando ausente)
ASK_NAME_TEMPLATE   = _env_template(
    "ASK_NAME_TEMPLATE",
    "Para concluir o agendamento: qual nome coloco aqui?"
)
NAME_SAVED_TEMPLATE = _env_template(
    "NAME_SAVED_TEMPLATE",
    "Obrigado, {name}! Vou te passar para {consultor} agora. 👍"
)
NAME_RETRY_TEMPLATE = _env_template(
    "NAME_RETRY_TEMPLATE",
    "Desculpe, não entendi. Pode me enviar só o *primeiro nome*?"
)

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
    # Baileys-like / variados
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
    # respostas de botões/listas (variações)
    "messages.0.message.templateButtonReplyMessage.selectedDisplayText",
    "messages.0.message.buttonsResponseMessage.selectedButtonId",
    "messages.0.message.listResponseMessage.title",
    "data.data.messages.0.message.buttonsResponseMessage.selectedButtonId",
    "data.data.messages.0.message.templateButtonReplyMessage.selectedDisplayText",
)

def _only_digits(s: str) -> str:
    return "".join(ch for ch in str(s) if ch.isdigit())

def _strip_accents(s: str) -> str:
    if not s:
        return ""
    nk = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in nk if not unicodedata.combining(ch))

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
    keys = {"text", "message", "body", "content", "caption", "conversation", "title"}
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

# --------------------------- Fluxo helpers ---------------------------

_POSITIVE_WORDS = {
    "sim", "ok", "okay", "claro", "perfeito", "pode", "pode sim", "pode continuar",
    "vamos", "bora", "manda", "mande", "envia", "enviar", "segue", "segue sim",
    "quero", "tenho interesse", "interessa", "top", "show", "positivo", "agora",
    "mais tarde", "sim pode", "pode mandar", "pode enviar", "pode mostrar",
}
_NEGATIVE_WORDS = {"nao", "não", "nao obrigado", "não obrigado", "pode encerrar", "parar", "cancelar", "encerre"}
_POSITIVE_EMOJIS = {"👍", "👌", "✅", "✔️", "✌️", "🤝"}

async def _has_recent_generic(session: AsyncSession, user_id: int, media_type: str, minutes: int) -> bool:
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
        return (now - last_at) <= timedelta(minutes=minutes)
    except Exception as exc:
        print(f"[state] erro ao consultar {media_type} recente: {exc!r}")
        return False

async def _has_recent_menu(session: AsyncSession, user_id: int, minutes: int = 30) -> bool:
    return await _has_recent_generic(session, user_id, "menu", minutes)

async def _has_recent_video(session: AsyncSession, user_id: int, minutes: int = 30) -> bool:
    return await _has_recent_generic(session, user_id, "video", minutes)

async def _has_recent_handoff(session: AsyncSession, user_id: int, minutes: int = 30) -> bool:
    return await _has_recent_generic(session, user_id, "handoff", minutes)

async def _has_recent_handoff_offer(session: AsyncSession, user_id: int, minutes: int = 30) -> bool:
    return await _has_recent_generic(session, user_id, "handoff_offer", minutes)

# estado "name_request" (quando pedimos o nome)
async def _has_recent_name_request(session: AsyncSession, user_id: int, minutes: int = 30) -> bool:
    return await _has_recent_generic(session, user_id, "name_request", minutes)

def _is_positive_reply(text: Optional[str]) -> bool:
    if not text:
        return False
    t = _normalize(text)
    if t in {_normalize(LUNA_MENU_YES), "sim"}:
        return True
    if t in _POSITIVE_WORDS:
        return True
    if any(e in text for e in _POSITIVE_EMOJIS):
        return True
    if "video" in t or "vídeo" in t:
        return True
    if t in {"0", "1"}:
        return t == "0"
    return False

def _is_negative_reply(text: Optional[str]) -> bool:
    if not text:
        return False
    t = _normalize(text)
    if t in {_normalize(LUNA_MENU_NO), "nao", "não"}:
        return True
    if t in _NEGATIVE_WORDS:
        return True
    if t in {"1"}:
        return True
    return False

# Handoff: distinção entre "agora" e "mais tarde"
def _wants_now(text: Optional[str]) -> bool:
    if not text:
        return False
    t = _normalize(text)
    return ("agora" in t) or (t in {"sim", "ok", "okay", "claro", "perfeito", "pode", "pode sim"})

def _wants_later(text: Optional[str]) -> bool:
    if not text:
        return False
    t = _normalize(text)
    return any(p in t for p in ("mais tarde", "depois", "amanha", "amanhã"))

_INVITE_PATTERNS = (
    "quer ver em 30", "quer ver em 30s", "quer ver em 30 s",
    "30 seg", "30seg", "30 segundos", "trinta segundos",
    "posso te mostrar", "posso apresentar um case", "exemplo objetivo",
    "te mostro em 30", "posso enviar um exemplo", "quer ver um exemplo",
    "apresentar um case curto"
)

def _looks_like_invite(reply_text: str) -> bool:
    if not reply_text:
        return False
    t = _normalize(reply_text)
    return any(p in t for p in _INVITE_PATTERNS)

# --------- NLU leve: formato (evitar pergunta duplicada) ---------

_FORMAT_PATTERNS = {
    "3d/ia": [
        r"\b3\s*[-/]?\s*d\b",
        r"\b3d\s*ia\b", r"\bia\s*3d\b",
        r"animac(?:ao|ão)\s*3\s*[-/]?\s*d",
    ],
    "institucional": [r"\binstitucional\b", r"\binstitu(?:cional|cional)\b"],
    "produto":       [r"\bproduto(?:s)?\b", r"video\s*de\s*produto"],
    "educativo":     [r"\beducativo\b", r"\baula(?:s)?\b", r"\btutorial(?:es)?\b", r"\btreinamento\b"],
    "convite":       [r"\bconvite(?:s)?\b"],
    "homenagem":     [r"\bhomenagem(?:s)?\b", r"\btributo\b"],
}
_PREFIX_NOISE = r"(?:era|eh|é|foi|quero|queria|pode\s*ser|seria|talvez|acho\s*que)\s+"

def _extract_formato(texto: Optional[str]) -> Optional[str]:
    if not texto:
        return None
    t = _normalize(texto)
    t = re.sub(rf"^{_PREFIX_NOISE}", "", t)
    for can, patterns in _FORMAT_PATTERNS.items():
        for rx in patterns:
            if re.search(rx, t, flags=re.IGNORECASE):
                return can
    return None

def _looks_like_format_question(texto: Optional[str]) -> bool:
    if not texto:
        return False
    t = _normalize(texto)
    if "qual formato" in t or "formato te interessa" in t or "formato voce" in t or "formato você" in t:
        return True
    if "3d" in t or "institucional" in t or "educativo" in t or "produto" in t or "convite" in t or "homenagem" in t:
        if "formato" in t:
            return True
    return False

# --------- IA tool-hints (tags e linguagem natural) ---------

_TOOL_TAG_RE = re.compile(r"#tools?\s*\(\s*([^)]+)\)", re.I)

def _strip_tool_tags(text: Optional[str]) -> str:
    if not text:
        return ""
    return _TOOL_TAG_RE.sub("", text).strip()

def _parse_tools_from_tags(reply_text: str) -> set:
    tools: set = set()
    if not reply_text:
        return tools
    m = _TOOL_TAG_RE.search(reply_text)
    if not m:
        return tools
    content = m.group(1) or ""
    parts = re.split(r"[,\s]+", content)
    for p in parts:
        p = p.strip().lower()
        if not p:
            continue
        if p in {"enviar_caixinha_interesse", "menu", "caixinha"}:
            tools.add("menu")
        elif p in {"enviar_video", "video", "vídeo"}:
            tools.add("video")
        elif p in {"enviar_msg", "handoff", "transfer"}:
            tools.add("handoff")
    return tools

def _looks_like_handoff(reply_text: str) -> bool:
    if not reply_text:
        return False
    t = _normalize(reply_text)
    patterns = (
        "vou te colocar em contato",
        "vou te conectar",
        "vou te passar para",
        "encaminharei voce ao nosso consultor",
        "encaminharei você ao nosso consultor",
        "encaminhar voce ao consultor",
        "encaminhar você ao consultor",
        "colocar voce com um consultor",
        "colocar você com um consultor",
        "te coloco com nosso consultor",
        "vou conectar voce com um especialista",
        "vou conectar você com um especialista",
        "encaminhando para o consultor",
        "enviar_msg",
    )
    return any(p in t for p in patterns)

def _parse_tool_hints(reply_text: str) -> Tuple[bool, bool, bool]:
    tags = _parse_tools_from_tags(reply_text)
    wants_menu = ("menu" in tags) or ("enviar_caixinha_interesse" in (reply_text or "").lower()) or _looks_like_invite(reply_text)
    wants_video = ("video" in tags) or ("enviar_video" in (reply_text or "").lower())
    wants_handoff = ("handoff" in tags) or _looks_like_handoff(reply_text)
    return (wants_menu, wants_video, wants_handoff)

# --------------------- Locks por usuário (evita corridas) ---------------------

_USER_LOCKS: Dict[str, asyncio.Lock] = {}

def _get_user_lock(phone: str) -> asyncio.Lock:
    key = _only_digits(phone or "")
    lock = _USER_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _USER_LOCKS[key] = lock
    return lock

# --------- Handoff helpers ---------

def _parse_notify_numbers(raw: str) -> List[str]:
    nums: List[str] = []
    for token in re.split(r"[,\s;]+", raw or ""):
        digits = _only_digits(token)
        if digits:
            nums.append(digits)
    return nums

async def _get_last_user_text(session: AsyncSession, user_id: int) -> Optional[str]:
    try:
        q = (
            select(Message)
            .where(Message.user_id == user_id, Message.sender == "user", Message.media_type == "text")
            .order_by(desc(Message.created_at))
            .limit(1)
        )
        res = await session.execute(q)
        last = res.scalar_one_or_none()
        return (last.content or "").strip() if last else None
    except Exception:
        return None

def _build_handoff_text(user: User, phone: str, last_msg: Optional[str]) -> str:
    digits = _only_digits(phone)
    name = (user.name or "").strip() or "—"
    last = (last_msg or "").strip() or "—"
    wa_link = f"https://wa.me/{digits}" if digits else ""

    tpl = HANDOFF_NOTIFY_TEMPLATE
    try:
        return tpl.format(name=name, digits=digits, last=last, wa_link=wa_link)
    except Exception as exc:
        print(f"[handoff] template format error: {exc!r}; using fallback.")
        return (
            "Novo lead aguardando contato (Luna — Verbo Vídeo)\n"
            f"Nome: {name}\nTelefone: +{digits}\nÚltima mensagem: {last}\nOrigem: WhatsApp\n"
            f"Link: {wa_link}"
        )

async def _notify_consultants(session: AsyncSession, *, user: User, phone: str, user_text: Optional[str]) -> None:
    targets = _parse_notify_numbers(HANDOFF_NOTIFY_NUMBERS)
    if not targets:
        print("[handoff] HANDOFF_NOTIFY_NUMBERS vazio ou inválido; nenhuma notificação enviada.")
        return
    last = (user_text or "").strip() or await _get_last_user_text(session, user.id)
    alert = _build_handoff_text(user, phone, last)
    for t in targets:
        try:
            await send_whatsapp_message(phone=t, content=alert, type_="text")
        except Exception as e:
            print(f"[handoff] falha ao notificar {t}: {e!r}")
    session.add(Message(user_id=user.id, sender="assistant", content=alert, media_type="handoff"))
    await session.commit()

def _offer_text(formato: Optional[str]) -> str:
    fmt = (formato or "o formato desejado")
    return HANDOFF_OFFER_TEMPLATE.format(formato=fmt, consultor=HANDOFF_CONSULTOR_NAME)

async def _send_handoff_offer(session: AsyncSession, *, phone: str, user: User, formato: Optional[str]) -> None:
    text = _offer_text(formato)
    try:
        await send_whatsapp_message(phone=phone, content=text, type_="text")
    except Exception as e:
        print(f"[handoff] falha ao enviar oferta: {e!r}")
    session.add(Message(user_id=user.id, sender="assistant", content=text, media_type="handoff_offer"))
    await session.commit()

# --------- Extração/Saneamento de NOME (revisado) ---------

_STOPWORDS_GREET = {
    "oi","olá","ola","blz","beleza","eai","opa","oie","boa","bom","tarde","noite","dia",
    "tudo","bem","td","tmj","valeu","vlw","obg","obrigado","obrigada","kkk","haha","rs",
    "agora","depois","mais","tarde","sim","nao","não","okay","ok","okey"
}

# padrão explícito no TEXTO original (não normalizado)
_NAME_EXPLICIT_RE = re.compile(
    r"(?:meu\s+nome\s*(?:é|e)|nome\s*:|sou|me\s+chamo|aqui\s*(?:é|e))\s+([A-Za-zÀ-ÖØ-öø-ÿ'´`^~\- ]{2,})",
    re.IGNORECASE
)

def _tokenize_words(s: str) -> List[str]:
    return re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", s or "")

def _sanitize_name(raw: str) -> Optional[str]:
    if not raw:
        return None
    tokens = [t for t in _tokenize_words(raw) if t]
    if not tokens:
        return None
    # remove saudações comuns no início/fim
    tokens = [t for t in tokens if _normalize(t) not in _STOPWORDS_GREET]
    if not tokens:
        return None
    # limita a 2 palavras
    tokens = tokens[:2]
    name = " ".join(tokens)
    try:
        name = name.title()
    except Exception:
        pass
    # descartes básicos
    if len(name) < 3 or any(ch.isdigit() for ch in name) or any(ch in "?!@" for ch in name):
        return None
    return name

def _extract_name_from_text(text: Optional[str], *, in_request: bool = False) -> Optional[str]:
    """Só aceita padrão explícito; se 'in_request'=True, aceita resposta curta tipo 'Matheus'."""
    if not text:
        return None
    # 1) padrão explícito
    m = _NAME_EXPLICIT_RE.search(text)
    if m:
        return _sanitize_name(m.group(1))
    # 2) se estamos pedindo o nome agora, aceitar resposta curta (ex.: 'Matheus', 'Ana Paula')
    if in_request:
        return _sanitize_name(text)
    return None

def _pushname_candidate(push_name: Optional[str]) -> Optional[str]:
    """Valida pushName do WhatsApp para evitar salvar 'Oi Blz', 'Atendimento', etc."""
    name = _sanitize_name(push_name or "")
    if not name:
        return None
    toks = [_normalize(t) for t in _tokenize_words(name)]
    if not toks:
        return None
    # se conter saudação óbvia, descarta
    if any(t in _STOPWORDS_GREET for t in toks):
        return None
    # nomes de 1 letra ou siglas curtas são ruins
    if len(toks) == 1 and len(toks[0]) <= 2:
        return None
    return name

# --------- Ações de saída ---------

async def _enviar_menu(session: AsyncSession, phone: str, user: User) -> None:
    if not LUNA_MENU_TEXT:
        print("[menu] LUNA_MENU_TEXT não definido; caixinha foi pulada.")
        return
    try:
        await send_menu_interesse(
            phone=phone,
            text=LUNA_MENU_TEXT,
            yes_label=LUNA_MENU_YES or "Sim",
            no_label=LUNA_MENU_NO or "Não",
            footer_text=LUNA_MENU_FOOTER or None,
        )
        session.add(Message(user_id=user.id, sender="assistant", content=LUNA_MENU_TEXT, media_type="menu"))
        await session.commit()
        print("[menu] enviado com sucesso.")
    except Exception as exc:
        print(f"[menu] falha ao enviar menu: {exc!r}")

async def _enviar_video(session: AsyncSession, phone: str, user: User) -> None:
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
        print("[video] enviado com sucesso.")
        if LUNA_VIDEO_AFTER_TEXT:
            await send_whatsapp_message(phone=phone, content=LUNA_VIDEO_AFTER_TEXT, type_="text")
            session.add(Message(user_id=user.id, sender="assistant", content=LUNA_VIDEO_AFTER_TEXT, media_type="text"))
            await session.commit()
    except Exception as exc:
        print(f"[video] falha ao enviar vídeo nativo: {exc!r} — enviando link em texto.")
        fallback_text = (LUNA_VIDEO_CAPTION + "\n" if LUNA_VIDEO_CAPTION else "") + f"{LUNA_VIDEO_URL}"
        try:
            await send_whatsapp_message(phone=phone, content=fallback_text, type_="text")
        except Exception as e2:
            print(f"[video] fallback textual também falhou: {e2!r}")
        session.add(Message(user_id=user.id, sender="assistant", content=fallback_text, media_type="text"))
        await session.commit()
        if LUNA_VIDEO_AFTER_TEXT:
            try:
                await send_whatsapp_message(phone=phone, content=LUNA_VIDEO_AFTER_TEXT, type_="text")
            except Exception:
                pass
            session.add(Message(user_id=user.id, sender="assistant", content=LUNA_VIDEO_AFTER_TEXT, media_type="text"))
            await session.commit()

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
    # SERIALIZA por usuário para evitar corridas de duplicatas
    async with _get_user_lock(phone):
        try:
            async with SessionLocal() as session:
                res = await session.execute(select(User).where(User.phone == phone))
                user = res.scalar_one_or_none()
                if not user:
                    # tenta aproveitar pushName SOB VALIDAÇÃO
                    pn = _pushname_candidate(push_name)
                    user = User(phone=phone, name=pn or None)
                    session.add(user)
                    await session.commit()
                    await session.refresh(user)
                else:
                    # atualiza com pushName válido se ainda não houver nome
                    if (not (user.name or "").strip()):
                        pn = _pushname_candidate(push_name)
                        if pn:
                            user.name = pn
                            await session.commit()

                menu_recent          = await _has_recent_menu(session, user.id, minutes=30)
                video_recent         = await _has_recent_video(session, user.id, minutes=30)
                handoff_recent       = await _has_recent_handoff(session, user.id, minutes=30)
                handoff_offer_recent = await _has_recent_handoff_offer(session, user.id, minutes=30)
                name_request_recent  = await _has_recent_name_request(session, user.id, minutes=30)

                # 0) Se houve caixinha recente, trate SIM/NÃO localmente (sem IA).
                if msg_type == "text" and text and menu_recent:
                    # NEGATIVO: encerra na hora
                    if _is_negative_reply(text):
                        end_text = LUNA_END_TEXT or "Tudo bem! Se precisar depois, estou por aqui. 🌟"
                        try:
                            await send_whatsapp_message(phone=phone, content=end_text, type_="text")
                        except Exception as e:
                            print(f"[uazapi] send end_text failed (bg): {e!r}")
                        session.add(Message(user_id=user.id, sender="assistant", content=end_text, media_type="text"))
                        await session.commit()
                        return
                    # POSITIVO: apenas se ainda não enviamos vídeo recentemente
                    if _is_positive_reply(text) and not video_recent:
                        await _enviar_video(session, phone, user)
                        return

                # 0.1) Estado aguardando NOME
                if msg_type == "text" and text and name_request_recent and not handoff_recent:
                    cand = _extract_name_from_text(text, in_request=True)
                    if cand:
                        user.name = cand
                        await session.commit()
                        try:
                            ack = NAME_SAVED_TEMPLATE.format(name=cand, consultor=HANDOFF_CONSULTOR_NAME)
                        except Exception:
                            ack = f"Obrigado, {cand}! Vou te passar para {HANDOFF_CONSULTOR_NAME} agora."
                        try:
                            await send_whatsapp_message(phone=phone, content=ack, type_="text")
                        except Exception as e:
                            print(f"[name] falha ao enviar confirmação de nome: {e!r}")
                        session.add(Message(user_id=user.id, sender="assistant", content=ack, media_type="text"))
                        await session.commit()
                        await _notify_consultants(session, user=user, phone=phone, user_text=text)
                        return
                    else:
                        try:
                            await send_whatsapp_message(phone=phone, content=NAME_RETRY_TEMPLATE, type_="text")
                        except Exception:
                            pass
                        session.add(Message(user_id=user.id, sender="assistant", content=NAME_RETRY_TEMPLATE, media_type="name_request"))
                        await session.commit()
                        return

                # 0.2) Auto-extrai nome somente com padrão explícito (fora do name_request)
                if msg_type == "text" and text and not (user.name or "").strip():
                    cand = _extract_name_from_text(text, in_request=False)
                    if cand:
                        user.name = cand
                        await session.commit()
                        session.add(Message(user_id=user.id, sender="assistant", content=f"[name_captured:{cand}]", media_type="name_captured"))
                        await session.commit()

                # 0.3) Respostas à OFERTA de handoff (consentimento)
                if msg_type == "text" and text and handoff_offer_recent and not handoff_recent:
                    if _wants_now(text):
                        # Se não temos nome, pedir antes de notificar
                        if not (user.name or "").strip():
                            try:
                                await send_whatsapp_message(phone=phone, content=ASK_NAME_TEMPLATE, type_="text")
                            except Exception:
                                pass
                            session.add(Message(user_id=user.id, sender="assistant", content=ASK_NAME_TEMPLATE, media_type="name_request"))
                            await session.commit()
                            return
                        # Já temos nome → confirma e notifica
                        try:
                            ack = HANDOFF_CONFIRM_TEMPLATE.format(consultor=HANDOFF_CONSULTOR_NAME)
                        except Exception:
                            ack = f"Perfeito! Estou te passando para {HANDOFF_CONSULTOR_NAME} agora."
                        try:
                            await send_whatsapp_message(phone=phone, content=ack, type_="text")
                        except Exception as e:
                            print(f"[handoff] falha ao enviar confirmação: {e!r}")
                        session.add(Message(user_id=user.id, sender="assistant", content=ack, media_type="text"))
                        await session.commit()
                        await _notify_consultants(session, user=user, phone=phone, user_text=text)
                        return
                    if _wants_later(text):
                        msg = HANDOFF_LATER_TEMPLATE.format(consultor=HANDOFF_CONSULTOR_NAME)
                        try:
                            await send_whatsapp_message(phone=phone, content=msg, type_="text")
                        except Exception:
                            pass
                        session.add(Message(user_id=user.id, sender="assistant", content=msg, media_type="text"))
                        await session.commit()
                        return
                    # Se não ficou claro → segue para IA.

                # 1) Texto -> consulta IA (com CONTEXTO do estado + PHONE)
                if msg_type == "text" and text:
                    thread_id = await get_or_create_thread(session, user)

                    digits_phone = _only_digits(phone or "")
                    meta_phone = f"(meta: phone_do_lead:+{digits_phone}. Ao chamar tools use este valor no parâmetro 'phone'.) "

                    # NLU leve: detectar formato informado pelo usuário para dirigir o próximo passo
                    user_formato = _extract_formato(text or "")

                    prefix = ""
                    if video_recent:
                        prefix = (
                            "Contexto: o lead acabou de receber o vídeo demonstrativo da Verbo Vídeo. "
                            "Prossiga com a etapa seguinte do fluxo (pós-vídeo). Mensagem do lead: "
                        )
                    elif menu_recent:
                        tnorm = _normalize(text)
                        if tnorm in {_normalize(LUNA_MENU_YES), "sim"} or "sim" in tnorm:
                            prefix = "Contexto: o lead respondeu SIM na caixinha de interesse. Mensagem do lead: "
                        elif tnorm in {_normalize(LUNA_MENU_NO), "nao", "não"} or "nao" in tnorm or "não" in tnorm:
                            prefix = "Contexto: o lead respondeu NÃO na caixinha de interesse. Mensagem do lead: "
                        else:
                            prefix = "Contexto: foi enviada uma caixinha de interesse ao lead. Mensagem do lead: "

                    ai_input = (meta_phone + prefix + (text or "")).strip()

                    # Sugestão para IA: se já temos formato, não perguntar de novo
                    if user_formato:
                        ai_input += f" [contexto_formato: o lead já indicou o formato '{user_formato}'. Não repita a pergunta de formato; confirme e avance.]"

                    reply_text = await ask_assistant(thread_id, ai_input) or ""
                    raw_reply_for_tools = reply_text

                    # Se a IA insistir em perguntar formato mas nós já temos, substitui por confirmação
                    if user_formato and _looks_like_format_question(reply_text):
                        reply_text = f"Perfeito, anotei: **{user_formato}**. Vamos avançar para os próximos passos?"

                    wants_menu, wants_video, wants_handoff = _parse_tool_hints(raw_reply_for_tools)

                    # 1.a) Se a IA pediu caixinha/convite
                    if (wants_menu or _looks_like_invite(raw_reply_for_tools)) and not menu_recent:
                        await _enviar_menu(session, phone, user)
                        return
                    if (wants_menu or _looks_like_invite(raw_reply_for_tools)) and menu_recent:
                        print("[guard] IA/convite pediu caixinha, mas já existe recente; ignorando convite.")

                    # 1.b) Se a IA pediu VÍDEO
                    if wants_video and not video_recent:
                        await _enviar_video(session, phone, user)
                        return
                    if wants_video and video_recent:
                        print("[guard] IA pediu vídeo, mas já enviamos recentemente; ignorando.")

                    # 1.c) Handoff por CONSENTIMENTO:
                    if (wants_handoff or user_formato) and not (handoff_recent or handoff_offer_recent):
                        await _send_handoff_offer(session, phone=phone, user=user, formato=user_formato)
                        return

                    # 1.d) Fallback opcional — vídeo após SIM na caixinha (se IA não mandou)
                    if not LUNA_STRICT_ASSISTANT and _is_positive_reply(text) and menu_recent and not video_recent:
                        await _enviar_video(session, phone, user)
                        return

                    # 1.e) Anti-eco (convite) após caixinha
                    if menu_recent and _looks_like_invite(raw_reply_for_tools):
                        print("[guard] menu enviado há pouco; suprimindo texto convite duplicado.")
                        return

                    # 1.f) Texto normal para o lead (SEM as tags #tools(...))
                    clean_text = _strip_tool_tags(reply_text) or "Certo!"
                    try:
                        await send_whatsapp_message(phone=phone, content=clean_text, type_="text")
                    except Exception as e:
                        print(f"[uazapi] send failed (bg): {e!r}")

                    session.add(Message(user_id=user.id, sender="assistant", content=clean_text, media_type="text"))
                    await session.commit()
                    return

                # 2) Mensagens não-texto
                ack = "Arquivo recebido com sucesso. Já estou processando! ✅"
                try:
                    await send_whatsapp_message(phone=phone, content=ack, type_="text")
                except Exception as e:
                    print(f"[uazapi] send failed (bg): {e!r}")
                session.add(Message(user_id=user.id, sender="assistant", content=ack, media_type="text"))
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

    # Garante que o usuário exista e, se faltar, atualiza nome com pushName válido
    res = await db.execute(select(User).where(User.phone == phone))
    user = res.scalar_one_or_none()
    if not user:
        pn = _pushname_candidate(push_name)
        user = User(phone=phone, name=pn or None)
        db.add(user)
        await db.commit()
        await db.refresh(user)
    else:
        if (not (user.name or "").strip()):
            pn = _pushname_candidate(push_name)
            if pn:
                user.name = pn
                await db.commit()

    # ------ DEDUP INBOUND ------
    if await _is_probably_duplicate(db, user.id, text if msg_type == "text" else None, msg_type, window_seconds=5):
        print("[dedup] inbound duplicado detectado; ignorando processamento.")
        return JSONResponse({"received": True, "note": "duplicate_dropped"}, status_code=200)

    # Persiste inbound
    in_msg = Message(
        user_id=user.id,
        sender="user",
        content=text if msg_type == "text" else None,
        media_type=msg_type or "unknown",
        media_url=None,
    )
    db.add(in_msg)
    await db.commit()

    asyncio.create_task(_process_message_async(phone=phone, msg_type=msg_type, text=text, push_name=push_name))
    return JSONResponse({"received": True}, status_code=200)

# --------------------------- dedup inbound helper ---------------------------
async def _is_probably_duplicate(db: AsyncSession, user_id: int, text: Optional[str], msg_type: str, window_seconds: int = 5) -> bool:
    """Dedup simples: mesmo conteúdo+tipo do último inbound dentro da janela."""
    try:
        q = (
            select(Message)
            .where(Message.user_id == user_id, Message.sender == "user")
            .order_by(desc(Message.created_at))
            .limit(1)
        )
        res = await db.execute(q)
        last = res.scalar_one_or_none()
        if not last:
            return False
        last_text = last.content or ""
        now = datetime.utcnow()
        last_at = last.created_at
        if getattr(last_at, "tzinfo", None) is not None:
            last_at = last_at.replace(tzinfo=None)
        same_text = (last_text or "").strip() == (text or "").strip()
        same_type = (last.media_type or "") == (msg_type or "")
        recent = (now - last_at) <= timedelta(seconds=window_seconds)
        return bool(same_text and same_type and recent)
    except Exception as exc:
        print(f"[dedup] erro na checagem de duplicidade: {exc!r}")
        return False

def get_router() -> APIRouter:
    return router
