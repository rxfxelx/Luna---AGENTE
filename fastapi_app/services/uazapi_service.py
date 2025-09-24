"""
Integração com Uazapi (WhatsApp) e upload opcional para Baserow.

Expõe:
- send_whatsapp_message(phone, content, type_="text", media_url=None, mime_type=None, caption=None)
- send_message(...) -> alias compatível (usa send_whatsapp_message)
- send_menu(phone, text=None, yes_label=None, no_label=None, footer=None)
- upload_file_to_baserow(media_url) -> Optional[dict]

Ajustes:
- Para '/send/text', prioriza payload {'number': '<digits>', 'text': '...'} (v2 da Uazapi).
- Para '/send/media', prioriza {'number': '<digits>', 'url': '...','caption': '...'}.
- Fallbacks automáticos para instalações com rotas antigas.
- Cabeçalho de auth configurável (token/apikey/bearer) via UAZAPI_AUTH_HEADER_NAME.
- Variáveis LUNA_* para menu e vídeo.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, Optional

import httpx

# -------------------- Config Uazapi --------------------
UAZAPI_BASE_URL = os.getenv("UAZAPI_BASE_URL", "").rstrip("/")
UAZAPI_TOKEN = os.getenv("UAZAPI_TOKEN", "")

# 'token' | 'apikey' | 'authorization_bearer'
UAZAPI_AUTH_HEADER_NAME = os.getenv("UAZAPI_AUTH_HEADER_NAME", "token").strip().lower()

# Rotas vindas do ambiente (usadas, mas SEMPRE tentamos '/send/text' primeiro)
UAZAPI_SEND_TEXT_PATH = os.getenv("UAZAPI_SEND_TEXT_PATH", "/send/text")
UAZAPI_SEND_MEDIA_PATH = os.getenv("UAZAPI_SEND_MEDIA_PATH", "/send/media")

# Fallbacks comuns em instalações diferentes
_TEXT_FALLBACKS = ["/send/message", "/api/sendText", "/sendText", "/messages/send", "/message/send"]
_MEDIA_FALLBACKS = ["/send/file", "/api/sendFile", "/api/sendMedia"]
_MENU_ENDPOINTS   = ["/send/menu", "/menu/send", "/send/buttons", "/sendButton"]

# -------------------- Config Luna (menu/vídeo) --------------------
LUNA_MENU_TEXT   = os.getenv("LUNA_MENU_TEXT", "").strip()
LUNA_MENU_FOOTER = os.getenv("LUNA_MENU_FOOTER", "").strip()
LUNA_MENU_YES    = os.getenv("LUNA_MENU_YES", "Sim, pode continuar").strip()
LUNA_MENU_NO     = os.getenv("LUNA_MENU_NO", "Não, encerrar contato").strip()

LUNA_VIDEO_URL        = os.getenv("LUNA_VIDEO_URL", "").strip()
LUNA_VIDEO_CAPTION    = os.getenv("LUNA_VIDEO_CAPTION", "").strip()
LUNA_VIDEO_AFTER_TEXT = os.getenv("LUNA_VIDEO_AFTER_TEXT", "").strip()
LUNA_END_TEXT         = os.getenv("LUNA_END_TEXT", "").strip()

# -------------------- Helpers --------------------
def _ensure_leading_slash(path: str) -> str:
    return path if path.startswith("/") else f"/{path}"

def _only_digits(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())

def _dedup(seq: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for x in seq:
        if not x:
            continue
        x = _ensure_leading_slash(x)
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out

def _headers() -> Dict[str, str]:
    """
    Constrói headers aceitando variações de autenticação.
    Padrão: header 'token'.
    """
    if not UAZAPI_TOKEN:
        raise RuntimeError("UAZAPI_TOKEN não configurado.")
    base = {"Accept": "application/json", "Content-Type": "application/json"}
    name = UAZAPI_AUTH_HEADER_NAME
    if name in {"token", "x-token"}:
        base["token"] = UAZAPI_TOKEN
    elif name in {"apikey", "api-key", "api_key"}:
        base["apikey"] = UAZAPI_TOKEN
    elif name in {"authorization_bearer", "authorization", "bearer"}:
        base["Authorization"] = f"Bearer {UAZAPI_TOKEN}"
    else:
        base["token"] = UAZAPI_TOKEN  # fallback seguro
    return base

def _chatid_variants(phone: str) -> Iterable[str]:
    """
    Gera variantes:
      1) <digits>@c.us
      2) <digits>@s.whatsapp.net
      3) <digits>
    """
    digits = _only_digits(phone) or phone
    seen = set()
    for v in (f"{digits}@c.us", f"{digits}@s.whatsapp.net", digits):
        if v and v not in seen:
            seen.add(v)
            yield v

def _infer_mime_from_url(url: str) -> str:
    l = url.lower()
    if l.endswith(".jpg") or l.endswith(".jpeg"):
        return "image/jpeg"
    if l.endswith(".png"):
        return "image/png"
    if l.endswith(".gif"):
        return "image/gif"
    if l.endswith(".mp4"):
        return "video/mp4"
    if l.endswith(".pdf"):
        return "application/pdf"
    if l.endswith(".mp3"):
        return "audio/mpeg"
    if l.endswith(".ogg") or l.endswith(".opus"):
        return "audio/ogg"
    return "application/octet-stream"

def _text_endpoints() -> list[str]:
    candidates = ["/send/text", UAZAPI_SEND_TEXT_PATH] + _TEXT_FALLBACKS
    return _dedup(candidates)

def _media_endpoints() -> list[str]:
    candidates = [UAZAPI_SEND_MEDIA_PATH, "/send/media"] + _MEDIA_FALLBACKS
    return _dedup(candidates)

def _menu_endpoints() -> list[str]:
    return _dedup(_MENU_ENDPOINTS)

# -------------------- Envio de mensagens --------------------
async def send_whatsapp_message(
    phone: str,
    content: str,
    *,
    type_: str = "text",
    media_url: Optional[str] = None,
    mime_type: Optional[str] = None,
    caption: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Envia mensagem via Uazapi com múltiplas tentativas (endpoints/payloads).
    - Para '/send/text': prioriza {"number": "<digits>", "text": content}.
    - Para '/send/media': prioriza {"number": "<digits>", "url": media_url, "caption": ...}.
    """
    if not UAZAPI_BASE_URL:
        raise RuntimeError("UAZAPI_BASE_URL não configurada.")

    headers = _headers()
    digits = _only_digits(phone) or phone

    async with httpx.AsyncClient(base_url=UAZAPI_BASE_URL, timeout=30.0) as client:
        if type_ == "text" or not media_url:
            for endpoint in _text_endpoints():
                if endpoint == "/send/text":
                    candidates = [
                        {"number": digits, "text": content},           # preferido
                        {"phone": digits, "text": content},
                        {"chatId": f"{digits}@c.us", "text": content},
                    ]
                else:
                    candidates = []
                    for cid in _chatid_variants(digits):
                        candidates.append({"chatId": cid, "text": content})
                    candidates.append({"phone": digits, "text": content})
                    candidates.append({"number": digits, "text": content})

                for payload in candidates:
                    try:
                        resp = await client.post(endpoint, json=payload, headers=headers)
                        if resp.status_code < 400:
                            try:
                                return resp.json()
                            except Exception:
                                return {"status": "ok", "http_status": resp.status_code}
                        else:
                            print(f"[uazapi] {endpoint} {resp.status_code} body={resp.text[:200].replace(chr(10),' ')}")
                    except Exception as exc:
                        print(f"[uazapi] exception on {endpoint} payload={list(payload.keys())}: {exc}")
            raise RuntimeError(f"Uazapi text send failed for phone={phone}")

        else:
            mime = mime_type or _infer_mime_from_url(media_url)
            for endpoint in _media_endpoints():
                if endpoint == "/send/media":
                    candidates = [
                        {"number": digits, "url": media_url, "caption": caption or content},  # preferido
                        {"phone": digits, "url": media_url, "caption": caption or content},
                        {"chatId": f"{digits}@c.us", "fileUrl": media_url, "mimeType": mime, "caption": caption or content},
                    ]
                else:
                    candidates = [
                        {"chatId": f"{digits}@c.us", "fileUrl": media_url, "mimeType": mime, "caption": caption or content},
                        {"phone": digits, "url": media_url, "caption": caption or content},
                        {"number": digits, "url": media_url, "caption": caption or content},
                    ]

                for payload in candidates:
                    try:
                        resp = await client.post(endpoint, json=payload, headers=headers)
                        if resp.status_code < 400:
                            try:
                                return resp.json()
                            except Exception:
                                return {"status": "ok", "http_status": resp.status_code}
                        else:
                            print(f"[uazapi] {endpoint} {resp.status_code} body={resp.text[:200].replace(chr(10),' ')}")
                    except Exception as exc:
                        print(f"[uazapi] exception on {endpoint} payload={list(payload.keys())}: {exc}")
            raise RuntimeError(f"Uazapi media send failed for phone={phone}")

# Alias de retrocompatibilidade
async def send_message(
    *,
    phone: str,
    text: str,
    media_url: Optional[str] = None,
    mime_type: Optional[str] = None,
) -> Dict[str, Any]:
    if media_url:
        return await send_whatsapp_message(
            phone=phone, content=text, type_="media", media_url=media_url, mime_type=mime_type, caption=text
        )
    return await send_whatsapp_message(phone=phone, content=text, type_="text")

# -------------------- Menu (botões) --------------------
async def send_menu(
    phone: str,
    *,
    text: Optional[str] = None,
    yes_label: Optional[str] = None,
    no_label: Optional[str] = None,
    footer: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Envia menu de botões conforme documentação Uazapi: POST /send/menu
    Payload: {"number": "...", "type": "button", "text": "...", "choices": ["A","B"], "footerText": "..."}
    """
    if not UAZAPI_BASE_URL:
        raise RuntimeError("UAZAPI_BASE_URL não configurada.")
    headers = _headers()
    digits = _only_digits(phone) or phone

    body = {
        "number": digits,
        "type": "button",
        "text": text or LUNA_MENU_TEXT or "Escolha uma das opções:",
        "choices": [yes_label or LUNA_MENU_YES, no_label or LUNA_MENU_NO],
        "footerText": footer or LUNA_MENU_FOOTER or "",
    }

    async with httpx.AsyncClient(base_url=UAZAPI_BASE_URL, timeout=30.0) as client:
        for endpoint in _menu_endpoints():
            try:
                resp = await client.post(endpoint, json=body, headers=headers)
                if resp.status_code < 400:
                    try:
                        return resp.json()
                    except Exception:
                        return {"status": "ok", "http_status": resp.status_code}
                else:
                    print(f"[uazapi] {endpoint} {resp.status_code} body={resp.text[:200].replace(chr(10),' ')}")
            except Exception as exc:
                print(f"[uazapi] exception on {endpoint}: {exc}")
    raise RuntimeError(f"Uazapi menu send failed for phone={phone}")

# -------------------- Baserow (opcional) --------------------
BASEROW_BASE_URL = os.getenv("BASEROW_BASE_URL", "").rstrip("/")
BASEROW_API_TOKEN = os.getenv("BASEROW_API_TOKEN", "")

async def upload_file_to_baserow(media_url: str) -> Optional[dict]:
    if not BASEROW_BASE_URL or not BASEROW_API_TOKEN:
        print("Baserow não configurado – pulando upload de arquivo.")
        return None
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            file_resp = await client.get(media_url)
            file_resp.raise_for_status()
            file_bytes = file_resp.content
            filename = media_url.split("/")[-1].split("?")[0] or "file"
            files = {"file": (filename, file_bytes)}
            headers = {"Authorization": f"Token {BASEROW_API_TOKEN}"}

            for url in (
                f"{BASEROW_BASE_URL}/api/user-files/upload-file/",
                f"{BASEROW_BASE_URL}/api/userfiles/upload_file/",
            ):
                try:
                    up = await client.post(url, headers=headers, files=files)
                    if up.status_code < 400:
                        return up.json()
                except Exception:
                    pass
            return None
        except Exception as exc:
            print(f"Erro ao baixar/enviar arquivo p/ Baserow: {exc}")
            return None