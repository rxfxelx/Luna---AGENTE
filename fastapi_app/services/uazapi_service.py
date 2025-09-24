# fastapi_app/services/uazapi_service.py
"""
Integração com Uazapi (WhatsApp) e upload opcional para Baserow.

Expõe:
- send_whatsapp_message(phone, content, type_="text", media_url=None, mime_type=None, caption=None)
- send_menu(phone, text=None, choices=None, footer_text=None)
- send_message(...) -> alias compatível (usa send_whatsapp_message)
- upload_file_to_baserow(media_url) -> Optional[dict]

Ajustes:
- '/send/text' é sempre tentado primeiro, com payload {'number': '<digits>', 'text': '...'}.
- '/send/media' prioriza {'number': '<digits>', 'url': '...','caption': '...'}.
- Adicionamos '/send/menu' para botões ("type": "button").
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, Optional, List

import httpx

# -------------------- Config UAZAPI --------------------
UAZAPI_BASE_URL = os.getenv("UAZAPI_BASE_URL", "").rstrip("/")
UAZAPI_TOKEN = os.getenv("UAZAPI_TOKEN", "")

# 'token' | 'apikey' | 'authorization_bearer'
UAZAPI_AUTH_HEADER_NAME = os.getenv("UAZAPI_AUTH_HEADER_NAME", "token").lower()

# Rotas vindas do ambiente
UAZAPI_SEND_TEXT_PATH = os.getenv("UAZAPI_SEND_TEXT_PATH", "/send/text")
UAZAPI_SEND_MEDIA_PATH = os.getenv("UAZAPI_SEND_MEDIA_PATH", "/send/media")
UAZAPI_SEND_MENU_PATH = os.getenv("UAZAPI_SEND_MENU_PATH", "/send/menu")

# Fallbacks comuns observados em instalações diferentes
_TEXT_FALLBACKS = ["/send/message", "/api/sendText", "/sendText", "/messages/send", "/message/send"]
_MEDIA_FALLBACKS = ["/send/file", "/api/sendFile", "/api/sendMedia"]
_MENU_FALLBACKS = ["/send/menu", "/send/button", "/send/buttons"]

# -------------------- ENVs de negócio (menu/vídeo) --------------------
LUNA_MENU_TEXT = os.getenv(
    "LUNA_MENU_TEXT",
    "Aqui é a Luna da Verbo Vídeo. Imagine a sua empresa comunicando com impacto, através de vídeos profissionais que unem roteiro, edição criativa e animações realistas em 3D com IA.",
)
LUNA_MENU_YES = os.getenv("LUNA_MENU_YES", "Sim, pode continuar")
LUNA_MENU_NO = os.getenv("LUNA_MENU_NO", "Não, encerrar contato")
LUNA_MENU_FOOTER = os.getenv("LUNA_MENU_FOOTER", "Escolha uma das opções abaixo")

LUNA_VIDEO_URL = os.getenv("LUNA_VIDEO_URL", "")
LUNA_VIDEO_CAPTION = os.getenv("LUNA_VIDEO_CAPTION", "Apresentação Verbo Vídeo — exemplo rápido do nosso padrão de entrega.")
LUNA_VIDEO_AFTER_TEXT = os.getenv("LUNA_VIDEO_AFTER_TEXT", "")
LUNA_END_TEXT = os.getenv("LUNA_END_TEXT", "")

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

def _chatid_variants(phone: str) -> Iterable[str]:
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
    # '/send/text' sempre primeiro
    candidates = ["/send/text", UAZAPI_SEND_TEXT_PATH] + _TEXT_FALLBACKS
    return _dedup(candidates)

def _media_endpoints() -> list[str]:
    candidates = [UAZAPI_SEND_MEDIA_PATH, "/send/media"] + _MEDIA_FALLBACKS
    return _dedup(candidates)

def _menu_endpoints() -> list[str]:
    candidates = [UAZAPI_SEND_MENU_PATH, "/send/menu"] + _MENU_FALLBACKS
    return _dedup(candidates)

# -------------------- Envio de TEXTO / MÍDIA --------------------
async def send_whatsapp_message(
    phone: str,
    content: str,
    *,
    type_: str = "text",
    media_url: Optional[str] = None,
    mime_type: Optional[str] = None,
    caption: Optional[str] = None,
) -> Dict[str, Any]:
    if not UAZAPI_BASE_URL:
        raise RuntimeError("UAZAPI_BASE_URL não configurada.")

    headers = _headers()
    digits = _only_digits(phone) or phone

    async with httpx.AsyncClient(base_url=UAZAPI_BASE_URL, timeout=30.0) as client:
        if type_ == "text" or not media_url:
            for endpoint in _text_endpoints():
                if endpoint == "/send/text":
                    candidates = [
                        {"number": digits, "text": content},
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
                            txt = resp.text[:200].replace("\n", " ")
                            print(f"[uazapi] {endpoint} {resp.status_code} body={txt}")
                    except Exception as exc:
                        print(f"[uazapi] exception on {endpoint} payload={list(payload.keys())}: {exc}")
            raise RuntimeError(f"Uazapi text send failed for phone={phone}")

        # mídia
        mime = mime_type or (media_url and _infer_mime_from_url(media_url)) or "application/octet-stream"
        for endpoint in _media_endpoints():
            if endpoint == "/send/media":
                candidates = [
                    {"number": digits, "url": media_url, "caption": caption or content},
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
                        txt = resp.text[:200].replace("\n", " ")
                        print(f"[uazapi] {endpoint} {resp.status_code} body={txt}")
                except Exception as exc:
                    print(f"[uazapi] exception on {endpoint} payload={list(payload.keys())}: {exc}")
        raise RuntimeError(f"Uazapi media send failed for phone={phone}")

# -------------------- Envio de MENU (botões) --------------------
async def send_menu(
    *,
    phone: str,
    text: Optional[str] = None,
    choices: Optional[List[str]] = None,
    footer_text: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Envia menu do tipo "button" (Uazapi /send/menu).
    Payload preferido:
      {"number": "<digits>", "type":"button", "text": "...", "choices": ["Sim","Não"], "footerText": "..."}
    """
    if not UAZAPI_BASE_URL:
        raise RuntimeError("UAZAPI_BASE_URL não configurada.")

    headers = _headers()
    digits = _only_digits(phone) or phone

    txt = (text or LUNA_MENU_TEXT).strip()
    ch = choices or [LUNA_MENU_YES, LUNA_MENU_NO]
    ft = (footer_text or LUNA_MENU_FOOTER).strip()

    payload_pref = {
        "number": digits,
        "type": "button",
        "text": txt,
        "choices": ch,
        "footerText": ft,
    }

    async with httpx.AsyncClient(base_url=UAZAPI_BASE_URL, timeout=30.0) as client:
        for endpoint in _menu_endpoints():
            candidates = [
                payload_pref,
                {**payload_pref, "phone": digits},  # variação
                {**payload_pref, "chatId": f"{digits}@c.us"},  # variação
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
                        txtb = resp.text[:200].replace("\n", " ")
                        print(f"[uazapi] {endpoint} {resp.status_code} body={txtb}")
                except Exception as exc:
                    print(f"[uazapi] exception on {endpoint} payload={list(payload.keys())}: {exc}")

    raise RuntimeError(f"Uazapi menu send failed for phone={phone}")

# -------------------- Alias compatível --------------------
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