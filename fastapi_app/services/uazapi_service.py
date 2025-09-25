# fastapi_app/services/uazapi_service.py
from __future__ import annotations

import os
from typing import Any, Dict, Iterable, Optional, List

import httpx

# -------------------- Config --------------------
UAZAPI_BASE_URL = os.getenv("UAZAPI_BASE_URL", "").rstrip("/")
UAZAPI_TOKEN = os.getenv("UAZAPI_TOKEN", "")

# 'token' | 'apikey' | 'authorization_bearer'
UAZAPI_AUTH_HEADER_NAME = os.getenv("UAZAPI_AUTH_HEADER_NAME", "token").lower()

# Rotas vindas do ambiente (usadas, mas SEMPRE tentamos as padrão primeiro)
UAZAPI_SEND_TEXT_PATH = os.getenv("UAZAPI_SEND_TEXT_PATH", "/send/text")
UAZAPI_SEND_MEDIA_PATH = os.getenv("UAZAPI_SEND_MEDIA_PATH", "/send/media")
UAZAPI_SEND_MENU_PATH  = os.getenv("UAZAPI_SEND_MENU_PATH",  "/send/menu")

# Fallbacks comuns observados em instalações diferentes
_TEXT_FALLBACKS  = ["/send/message", "/api/sendText", "/sendText", "/messages/send", "/message/send"]
_MEDIA_FALLBACKS = ["/send/file", "/api/sendFile", "/api/sendMedia"]
_MENU_FALLBACKS  = [
    "/send/menu",
    "/send/buttons",
    "/send/button",
    "/send/interactive",
    "/api/sendMenu",
    "/messages/buttons",
]

# -------------------- Helpers --------------------
def _ensure_leading_slash(path: str) -> str:
    return path if path.startswith("/") else f"/{path}"

def _only_digits(s: str) -> str:
    return "".join(ch for ch in str(s) if ch.isdigit())

def normalize_number(s: str) -> str:  # compat legado
    return _only_digits(s)

def _dedup(seq: Iterable[str]) -> List[str]:
    out: List[str] = []
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

def _text_endpoints() -> List[str]:
    # ordem garantida: '/send/text' primeiro
    candidates = ["/send/text", UAZAPI_SEND_TEXT_PATH] + _TEXT_FALLBACKS
    return _dedup(candidates)

def _media_endpoints() -> List[str]:
    candidates = ["/send/media", UAZAPI_SEND_MEDIA_PATH] + _MEDIA_FALLBACKS
    return _dedup(candidates)

def _menu_endpoints() -> List[str]:
    candidates = ["/send/menu", UAZAPI_SEND_MENU_PATH] + _MENU_FALLBACKS
    return _dedup(candidates)

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
                        {"phone":  digits, "text": content},           # variação
                        {"chatId": f"{digits}@c.us", "text": content}, # fallback
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
                            print(f"[uazapi][text] OK {endpoint} payload_keys={list(payload.keys())}")
                            try:
                                return resp.json()
                            except Exception:
                                return {"status": "ok", "http_status": resp.status_code}
                        else:
                            print(f"[uazapi][text] {endpoint} {resp.status_code} body={resp.text[:200].replace(chr(10),' ')}")
                    except Exception as exc:
                        print(f"[uazapi][text] EXC {endpoint} payload={list(payload.keys())}: {exc}")
            raise RuntimeError(f"Uazapi text send failed for phone={phone}")

        # media
        mime = mime_type or _infer_mime_from_url(media_url)
        for endpoint in _media_endpoints():
            if endpoint == "/send/media":
                candidates = [
                    {"number": digits, "url": media_url, "caption": caption or content},
                    {"phone":  digits, "url": media_url, "caption": caption or content},
                    {
                        "chatId": f"{digits}@c.us",
                        "fileUrl": media_url,
                        "mimeType": mime,
                        "caption": caption or content,
                    },
                ]
            else:
                candidates = [
                    {
                        "chatId": f"{digits}@c.us",
                        "fileUrl": media_url,
                        "mimeType": mime,
                        "caption": caption or content,
                    },
                    {"phone":  digits, "url": media_url, "caption": caption or content},
                    {"number": digits, "url": media_url, "caption": caption or content},
                ]

            for payload in candidates:
                try:
                    resp = await client.post(endpoint, json=payload, headers=headers)
                    if resp.status_code < 400:
                        print(f"[uazapi][media] OK {endpoint} payload_keys={list(payload.keys())}")
                        try:
                            return resp.json()
                        except Exception:
                            return {"status": "ok", "http_status": resp.status_code}
                    else:
                        print(f"[uazapi][media] {endpoint} {resp.status_code} body={resp.text[:200].replace(chr(10),' ')}")
                except Exception as exc:
                    print(f"[uazapi][media] EXC {endpoint} payload={list(payload.keys())}: {exc}")
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
    Envia caixinha (menu/botões) tentando múltiplos formatos/rotas:
      - choices: [yes, no]
      - buttons: [{"text":...}, ...]  ou  [{"buttonText":...}, ...]
      - options: [yes, no]
      - number | phone | chatId
    """
    if not UAZAPI_BASE_URL:
        raise RuntimeError("UAZAPI_BASE_URL não configurada.")
    headers = _headers()
    digits = _only_digits(phone) or phone

    def _common(base: Dict[str, Any]) -> Dict[str, Any]:
        if footer_text:
            base["footerText"] = footer_text
        return base

    payload_variants: List[Dict[str, Any]] = []

    # 1) formato observado no seu n8n (choices)  
    payload_variants += [
        _common({"number": digits, "type": "button", "text": text, "choices": [yes_label, no_label]}),
        _common({"phone":  digits, "type": "button", "text": text, "choices": [yes_label, no_label]}),
    ]

    # 2) variações com "buttons" e "buttonText"
    buttons_simple = [{"text": yes_label}, {"text": no_label}]
    buttons_bt     = [{"buttonText": yes_label}, {"buttonText": no_label}]
    for id_field in ("number", "phone"):
        payload_variants += [
            _common({id_field: digits, "type": "button", "text": text, "buttons": buttons_simple}),
            _common({id_field: digits, "type": "button", "text": text, "buttons": buttons_bt}),
        ]

    # 3) usando chatId
    for cid in _chatid_variants(digits):
        payload_variants += [
            _common({"chatId": cid, "type": "button", "text": text, "choices": [yes_label, no_label]}),
            _common({"chatId": cid, "type": "button", "text": text, "buttons": buttons_simple}),
            _common({"chatId": cid, "type": "button", "text": text, "buttons": buttons_bt}),
        ]

    # 4) alguns aceitam "options"
    payload_variants += [
        _common({"number": digits, "type": "button", "text": text, "options": [yes_label, no_label]}),
        _common({"phone":  digits, "type": "button", "text": text, "options": [yes_label, no_label]}),
    ]

    endpoints = _menu_endpoints()

    async with httpx.AsyncClient(base_url=UAZAPI_BASE_URL, timeout=30.0) as client:
        for endpoint in endpoints:
            for payload in payload_variants:
                try:
                    resp = await client.post(endpoint, json=payload, headers=headers)
                    if resp.status_code < 400:
                        print(f"[uazapi][menu] OK {endpoint} payload_keys={list(payload.keys())}")
                        try:
                            return resp.json()
                        except Exception:
                            return {"status": "ok", "http_status": resp.status_code}
                    else:
                        print(f"[uazapi][menu] {endpoint} {resp.status_code} body={resp.text[:200].replace(chr(10),' ')}")
                except Exception as exc:
                    print(f"[uazapi][menu] EXC {endpoint} payload={list(payload.keys())}: {exc}")

    # Degrada com texto se tudo falhar
    print("[uazapi][menu] todos os formatos/rotas falharam; enviando texto de fallback.")
    await send_whatsapp_message(phone=digits, content=f"{text}\n\n1) {yes_label}\n2) {no_label}", type_="text")
    raise RuntimeError("Uazapi menu send failed")

# Alias antigo
async def send_message(
    *, phone: str, text: str, media_url: Optional[str] = None, mime_type: Optional[str] = None
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
