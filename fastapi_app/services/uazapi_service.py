# fastapi_app/services/uazapi_service.py
"""
Integração com Uazapi (WhatsApp) e upload opcional para Baserow.

Expõe:
- send_whatsapp_message(phone, content, type_="text", media_url=None, mime_type=None, caption=None)
- send_menu_interesse(phone, text, yes_label, no_label, footer_text=None)
- send_message(...) -> alias compatível (usa send_whatsapp_message)
- upload_file_to_baserow(media_url) -> Optional[dict]
- normalize_number(phone) -> compat com módulos antigos
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, Optional, Tuple

import httpx

# -------------------- Config --------------------
UAZAPI_BASE_URL = os.getenv("UAZAPI_BASE_URL", "").rstrip("/")
UAZAPI_TOKEN = os.getenv("UAZAPI_TOKEN", "")

# 'token' | 'apikey' | 'authorization_bearer'
UAZAPI_AUTH_HEADER_NAME = os.getenv("UAZAPI_AUTH_HEADER_NAME", "token").lower()

# Rotas vindas do ambiente (usadas junto com fallbacks)
UAZAPI_SEND_TEXT_PATH = os.getenv("UAZAPI_SEND_TEXT_PATH", "/send/text")
UAZAPI_SEND_MEDIA_PATH = os.getenv("UAZAPI_SEND_MEDIA_PATH", "/send/media")
UAZAPI_SEND_MENU_PATH = os.getenv("UAZAPI_SEND_MENU_PATH", "/send/menu")

# Fallbacks comuns observados em instalações diferentes
_TEXT_FALLBACKS = ["/send/message", "/api/sendText", "/sendText", "/messages/send", "/message/send"]
_MEDIA_FALLBACKS = ["/send/file", "/api/sendFile", "/api/sendMedia", "/sendMedia"]
_MENU_FALLBACKS = ["/send/menu", "/api/sendMenu", "/send/button", "/send/buttons"]

# -------------------- Helpers --------------------
def _ensure_leading_slash(path: str) -> str:
    return path if path.startswith("/") else f"/{path}"

def _only_digits(s: str) -> str:
    return "".join(ch for ch in str(s) if ch.isdigit())

def normalize_number(s: str) -> str:  # noqa: N802
    """Compatibilidade com módulos antigos: remove tudo exceto dígitos."""
    return _only_digits(s)

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

def _headers_json() -> Dict[str, str]:
    if not UAZAPI_TOKEN:
        raise RuntimeError("UAZAPI_TOKEN não configurado.")
    base = {"Accept": "application/json", "Content-Type": "application/json"}
    if UAZAPI_AUTH_HEADER_NAME in {"token", "x-token"}:
        base["token"] = UAZAPI_TOKEN
    elif UAZAPI_AUTH_HEADER_NAME in {"apikey", "api-key", "api_key"}:
        base["apikey"] = UAZAPI_TOKEN
    elif UAZAPI_AUTH_HEADER_NAME in {"authorization_bearer", "authorization", "bearer"}:
        base["Authorization"] = f"Bearer {UAZAPI_TOKEN}"
    else:
        base["token"] = UAZAPI_TOKEN  # fallback seguro
    return base

def _headers_form() -> Dict[str, str]:
    h = _headers_json().copy()
    h["Content-Type"] = "application/x-www-form-urlencoded"
    return h

def _chatid_variants(phone: str) -> Iterable[str]:
    """Gera variantes: <digits>@c.us, <digits>@s.whatsapp.net, <digits>."""
    digits = _only_digits(phone) or phone
    seen = set()
    for v in (f"{digits}@c.us", f"{digits}@s.whatsapp.net", digits):
        if v and v not in seen:
            seen.add(v)
            yield v

def _infer_mime_from_url(url: str) -> str:
    l = (url or "").lower()
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
    # Ordem: env → padrão → fallbacks
    candidates = [UAZAPI_SEND_TEXT_PATH, "/send/text"] + _TEXT_FALLBACKS
    return _dedup(candidates)

def _media_endpoints() -> list[str]:
    candidates = [UAZAPI_SEND_MEDIA_PATH, "/send/media"] + _MEDIA_FALLBACKS
    return _dedup(candidates)

def _menu_endpoints() -> list[str]:
    candidates = [UAZAPI_SEND_MENU_PATH, "/send/menu"] + _MENU_FALLBACKS
    return _dedup(candidates)

async def _try_post(
    client: httpx.AsyncClient, endpoint: str, payload_json: Dict[str, Any], payload_form: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Tenta JSON e depois FORM no mesmo endpoint."""
    try:
        resp = await client.post(endpoint, json=payload_json, headers=_headers_json())
        if resp.status_code < 400:
            try:
                return resp.json()
            except Exception:
                return {"status": "ok", "http_status": resp.status_code}
        else:
            print(f"[uazapi] {endpoint} JSON {resp.status_code} body={resp.text[:200].replace(chr(10),' ')}")
    except Exception as exc:
        print(f"[uazapi] exception JSON {endpoint}: {exc}")

    # fallback: form urlencoded
    try:
        resp = await client.post(endpoint, data=payload_form, headers=_headers_form())
        if resp.status_code < 400:
            try:
                return resp.json()
            except Exception:
                return {"status": "ok", "http_status": resp.status_code}
        else:
            print(f"[uazapi] {endpoint} FORM {resp.status_code} body={resp.text[:200].replace(chr(10),' ')}")
    except Exception as exc:
        print(f"[uazapi] exception FORM {endpoint}: {exc}")

    return None

# -------------------- Senders --------------------
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
    - Texto: prioriza {"number","text"}, {"phone","text"} e variações com chatId.
    - Mídia: aceita variações {"file": url}, {"url": url}, {"fileUrl": url}, e "caption".
    """
    if not UAZAPI_BASE_URL:
        raise RuntimeError("UAZAPI_BASE_URL não configurada.")

    digits = _only_digits(phone) or phone

    async with httpx.AsyncClient(base_url=UAZAPI_BASE_URL, timeout=30.0) as client:
        # ---------------- TEXT ----------------
        if type_ == "text" or not media_url:
            for endpoint in _text_endpoints():
                json_payloads = [
                    {"number": digits, "text": content},
                    {"phone": digits, "text": content},
                ] + [{"chatId": cid, "text": content} for cid in _chatid_variants(digits)]

                for p in json_payloads:
                    got = await _try_post(client, endpoint, p, p)
                    if got is not None:
                        return got

            raise RuntimeError(f"Uazapi text send failed for phone={phone}")

        # ---------------- MEDIA ----------------
        mime = mime_type or _infer_mime_from_url(media_url)
        for endpoint in _media_endpoints():
            payloads: Tuple[Dict[str, Any], ...] = (
                {"number": digits, "type": "video", "file": media_url, "caption": caption or content},
                {"number": digits, "file": media_url, "caption": caption or content},
                {"phone": digits, "file": media_url, "caption": caption or content},
                {"number": digits, "url": media_url, "caption": caption or content},
                {"number": digits, "fileUrl": media_url, "mimeType": mime, "caption": caption or content},
                {"chatId": f"{digits}@c.us", "fileUrl": media_url, "mimeType": mime, "caption": caption or content},
            )

            for p in payloads:
                got = await _try_post(client, endpoint, p, p)
                if got is not None:
                    return got

        raise RuntimeError(f"Uazapi media send failed for phone={phone}")

async def send_menu_interesse(
    *,
    phone: str,
    text: str,
    yes_label: str,
    no_label: str,
    footer_text: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Envia caixinha (botões) usando /send/menu com múltiplos formatos:
      - {"number","type":"button","text","choices":[...],"footerText"}
      - {"number","type":"button","text","buttons":[{"id","title"},...]}
      - {"number","text","button1Label","button2Label"}
    """
    if not UAZAPI_BASE_URL:
        raise RuntimeError("UAZAPI_BASE_URL não configurada.")

    digits = _only_digits(phone) or phone

    # JSON e FORM: variações
    json_variants = [
        {
            "number": digits,
            "type": "button",
            "text": text,
            "choices": [yes_label, no_label],
            **({"footerText": footer_text} if footer_text else {}),
        },
        {
            "number": digits,
            "type": "button",
            "text": text,
            "buttons": [
                {"id": "yes", "title": yes_label},
                {"id": "no",  "title": no_label},
            ],
            **({"footerText": footer_text} if footer_text else {}),
        },
        # Fallback antigo
        {
            "number": digits,
            "text": text,
            "button1Label": yes_label,
            "button2Label": no_label,
        },
        # Variação "phone"
        {
            "phone": digits,
            "type": "button",
            "text": text,
            "choices": [yes_label, no_label],
            **({"footerText": footer_text} if footer_text else {}),
        },
    ]

    form_variants = json_variants  # os mesmos payloads servem como form

    async with httpx.AsyncClient(base_url=UAZAPI_BASE_URL, timeout=30.0) as client:
        for endpoint in _menu_endpoints():
            for j, f in zip(json_variants, form_variants):
                got = await _try_post(client, endpoint, j, f)
                if got is not None:
                    return got

    raise RuntimeError("Uazapi menu send failed")

# Alias antigo
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
