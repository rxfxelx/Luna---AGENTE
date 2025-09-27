# fastapi_app/services/uazapi_service.py
"""
Integração com Uazapi (WhatsApp) e upload opcional para Baserow.

Expõe:
- send_whatsapp_message(phone, content, type_="text", media_url=None, mime_type=None, caption=None)
- send_menu_interesse(phone, text, yes_label, no_label, footer_text=None)
- send_message(...) -> alias compatível (usa send_whatsapp_message)
- upload_file_to_baserow(media_url) -> Optional[dict]
- normalize_number(phone) -> compatibilidade com módulos antigos

Melhorias:
- Tentativas automáticas (retries) com pequeno backoff
- Fallbacks de endpoints e formatos (JSON e FORM)
- Variações de payload para instalações diferentes do Uazapi
- Logs de depuração opcionais via UAZAPI_DEBUG=true
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, Iterable, Optional, Sequence

import httpx

# -------------------- Config --------------------
UAZAPI_BASE_URL = os.getenv("UAZAPI_BASE_URL", "").rstrip("/")
UAZAPI_TOKEN = os.getenv("UAZAPI_TOKEN", "")

# 'token' | 'apikey' | 'authorization_bearer'
UAZAPI_AUTH_HEADER_NAME = os.getenv("UAZAPI_AUTH_HEADER_NAME", "token").lower()

# Rotas vindas do ambiente (usadas, mas SEMPRE tentamos '/send/text' primeiro)
UAZAPI_SEND_TEXT_PATH = os.getenv("UAZAPI_SEND_TEXT_PATH", "/send/text")
UAZAPI_SEND_MEDIA_PATH = os.getenv("UAZAPI_SEND_MEDIA_PATH", "/send/media")
UAZAPI_SEND_MENU_PATH = os.getenv("UAZAPI_SEND_MENU_PATH", "/send/menu")

# Fallbacks observados em provedores
_TEXT_FALLBACKS = [
    "/send/message",
    "/api/sendText",
    "/sendText",
    "/messages/send",
    "/message/send",
]
_MEDIA_FALLBACKS = [
    "/send/file",
    "/api/sendFile",
    "/api/sendMedia",
]
_MENU_FALLBACKS = [
    "/api/sendMenu",
    "/send/button",
    "/send/buttons",
]

# Depuração opcional
UAZAPI_DEBUG = os.getenv("UAZAPI_DEBUG", "false").lower() == "true"

# Retentativas leves
MAX_TRIES = 3
RETRY_SLEEP_SECONDS = 0.8


# -------------------- Helpers --------------------
def _dbg(msg: str) -> None:
    if UAZAPI_DEBUG:
        print(f"[uazapi-debug] {msg}")


def _ensure_leading_slash(path: str) -> str:
    return path if path.startswith("/") else f"/{path}"


def _only_digits(s: str) -> str:
    return "".join(ch for ch in str(s) if ch.isdigit())


def normalize_number(s: str) -> str:  # retrocompat
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


def _headers_base() -> Dict[str, str]:
    if not UAZAPI_TOKEN:
        raise RuntimeError("UAZAPI_TOKEN não configurado.")
    base = {"Accept": "application/json"}
    if UAZAPI_AUTH_HEADER_NAME in {"token", "x-token"}:
        base["token"] = UAZAPI_TOKEN
    elif UAZAPI_AUTH_HEADER_NAME in {"apikey", "api-key", "api_key"}:
        base["apikey"] = UAZAPI_TOKEN
    elif UAZAPI_AUTH_HEADER_NAME in {"authorization_bearer", "authorization", "bearer"}:
        base["Authorization"] = f"Bearer {UAZAPI_TOKEN}"
    else:
        base["token"] = UAZAPI_TOKEN
    return base


def _headers_json() -> Dict[str, str]:
    h = _headers_base()
    h["Content-Type"] = "application/json"
    return h


def _headers_form() -> Dict[str, str]:
    h = _headers_base()
    # urlencoded é o mais aceito para "FORM" no Uazapi
    h["Content-Type"] = "application/x-www-form-urlencoded"
    return h


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


def _text_endpoints() -> list[str]:
    return _dedup(["/send/text", UAZAPI_SEND_TEXT_PATH] + _TEXT_FALLBACKS)


def _media_endpoints() -> list[str]:
    return _dedup([UAZAPI_SEND_MEDIA_PATH, "/send/media"] + _MEDIA_FALLBACKS)


def _menu_endpoints() -> list[str]:
    return _dedup([UAZAPI_SEND_MENU_PATH, "/send/menu"] + _MENU_FALLBACKS)


async def _post_with_retries(
    client: httpx.AsyncClient,
    endpoint: str,
    *,
    json_payload: Optional[Dict[str, Any]] = None,
    form_payload: Optional[Dict[str, Any]] = None,
) -> Optional[httpx.Response]:
    """
    Tenta enviar como JSON (se json_payload) e/ou como FORM (se form_payload) com retentativas leves.
    Retorna o primeiro httpx.Response com status < 400 ou None se todas falharem.
    """
    endpoint = _ensure_leading_slash(endpoint)

    # Tenta JSON
    if json_payload is not None:
        for attempt in range(1, MAX_TRIES + 1):
            try:
                _dbg(f"POST JSON {endpoint} try={attempt} keys={list(json_payload.keys())}")
                resp = await client.post(endpoint, json=json_payload, headers=_headers_json())
                if resp.status_code < 400:
                    return resp
                print(f"[uazapi] {endpoint} JSON {resp.status_code} body={resp.text[:200].replace(chr(10),' ')}")
            except Exception as exc:
                print(f"[uazapi] exception JSON {endpoint} keys={list(json_payload.keys())}: {exc}")
            await asyncio.sleep(RETRY_SLEEP_SECONDS)

    # Tenta FORM
    if form_payload is not None:
        for attempt in range(1, MAX_TRIES + 1):
            try:
                _dbg(f"POST FORM {endpoint} try={attempt} keys={list(form_payload.keys())}")
                resp = await client.post(endpoint, data=form_payload, headers=_headers_form())
                if resp.status_code < 400:
                    return resp
                print(f"[uazapi] {endpoint} FORM {resp.status_code} body={resp.text[:200].replace(chr(10),' ')}")
            except Exception as exc:
                print(f"[uazapi] exception FORM {endpoint} keys={list(form_payload.keys())}: {exc}")
            await asyncio.sleep(RETRY_SLEEP_SECONDS)

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
    Envia texto ou mídia.
    - Texto: tenta várias chaves (text/message/body) e destinos (number/phone/chatId/jid/to).
    - Mídia: cobre variações (url/fileUrl + mimeType/caption).
    """
    if not UAZAPI_BASE_URL:
        raise RuntimeError("UAZAPI_BASE_URL não configurada.")

    digits = _only_digits(phone) or phone

    async with httpx.AsyncClient(base_url=UAZAPI_BASE_URL, timeout=30.0) as client:
        # ---------------- TEXT ----------------
        if type_ == "text" or not media_url:
            for endpoint in _text_endpoints():
                # Candidatos JSON (ordem importa)
                json_candidates: Sequence[Dict[str, Any]] = [
                    {"number": digits, "text": content},
                    {"phone": digits, "text": content},
                    {"number": digits, "message": content},
                    {"number": digits, "body": content},
                ]
                # adiciona variantes com chatId/jid/to
                for cid in _chatid_variants(digits):
                    json_candidates += [
                        {"chatId": cid, "text": content},
                        {"jid": cid, "text": content},
                        {"to": cid, "text": content},
                    ]

                # FORM (alguns provedores aceitam apenas isto)
                form_candidates: Sequence[Dict[str, Any]] = [
                    {"number": digits, "text": content},
                    {"phone": digits, "text": content},
                    {"number": digits, "message": content},
                    {"number": digits, "body": content},
                ]

                for payload in json_candidates:
                    resp = await _post_with_retries(client, endpoint, json_payload=payload)
                    if resp is not None:
                        try:
                            return resp.json()
                        except Exception:
                            return {"status": "ok", "http_status": resp.status_code}

                for payload in form_candidates:
                    resp = await _post_with_retries(client, endpoint, form_payload=payload)
                    if resp is not None:
                        try:
                            return resp.json()
                        except Exception:
                            return {"status": "ok", "http_status": resp.status_code}

            raise RuntimeError(f"Uazapi text send failed for phone={phone}")

        # ---------------- MEDIA ----------------
        else:
            mime = mime_type or _infer_mime_from_url(media_url)
            cap = caption or content or ""

            for endpoint in _media_endpoints():
                # JSON candidates
                json_candidates: Sequence[Dict[str, Any]] = [
                    # variação "url"
                    {"number": digits, "url": media_url, "caption": cap},
                    {"phone": digits, "url": media_url, "caption": cap},
                    # variação "fileUrl" + mime
                    {"number": digits, "fileUrl": media_url, "mimeType": mime, "caption": cap},
                    {"phone": digits, "fileUrl": media_url, "mimeType": mime, "caption": cap},
                ]
                for cid in _chatid_variants(digits):
                    json_candidates += [
                        {"chatId": cid, "fileUrl": media_url, "mimeType": mime, "caption": cap},
                        {"jid": cid, "fileUrl": media_url, "mimeType": mime, "caption": cap},
                        {"to": cid, "fileUrl": media_url, "mimeType": mime, "caption": cap},
                    ]

                # FORM candidates (menos comum, mas ajuda)
                form_candidates: Sequence[Dict[str, Any]] = [
                    {"number": digits, "url": media_url, "caption": cap},
                    {"number": digits, "fileUrl": media_url, "mimeType": mime, "caption": cap},
                ]

                for payload in json_candidates:
                    resp = await _post_with_retries(client, endpoint, json_payload=payload)
                    if resp is not None:
                        try:
                            return resp.json()
                        except Exception:
                            return {"status": "ok", "http_status": resp.status_code}

                for payload in form_candidates:
                    resp = await _post_with_retries(client, endpoint, form_payload=payload)
                    if resp is not None:
                        try:
                            return resp.json()
                        except Exception:
                            return {"status": "ok", "http_status": resp.status_code}

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
    Envia menu/botões com variações de payload:
    - type=button + choices=[...]
    - buttons=[{id,text}, {id,text}]
    - usa JSON e FORM (urlencoded)
    """
    if not UAZAPI_BASE_URL:
        raise RuntimeError("UAZAPI_BASE_URL não configurada.")
    digits = _only_digits(phone) or phone

    async with httpx.AsyncClient(base_url=UAZAPI_BASE_URL, timeout=30.0) as client:
        for endpoint in _menu_endpoints():
            # ---------- JSON: modelo choices ----------
            json_variants: Sequence[Dict[str, Any]] = [
                {
                    "number": digits,
                    "type": "button",
                    "text": text,
                    "choices": [yes_label, no_label],
                    "footerText": footer_text or "",
                },
                {
                    "phone": digits,
                    "type": "button",
                    "text": text,
                    "choices": [yes_label, no_label],
                    "footerText": footer_text or "",
                },
                # ---------- JSON: modelo buttons ----------
                {
                    "number": digits,
                    "text": text,
                    "footerText": footer_text or "",
                    "buttons": [{"id": "yes", "text": yes_label}, {"id": "no", "text": no_label}],
                },
                {
                    "phone": digits,
                    "text": text,
                    "footer": footer_text or "",  # algumas distros usam 'footer'
                    "buttons": [{"id": "yes", "text": yes_label}, {"id": "no", "text": no_label}],
                },
            ]

            # ---------- FORM/urlencoded ----------
            form_variants: Sequence[Dict[str, Any]] = [
                {
                    "number": digits,
                    "type": "button",
                    "text": text,
                    "footerText": footer_text or "",
                    "choices[]": [yes_label, no_label],
                },
                {
                    "number": digits,
                    "text": text,
                    "footerText": footer_text or "",
                    # alguns aceitam botões indexados
                    "buttons[0][id]": "yes",
                    "buttons[0][text]": yes_label,
                    "buttons[1][id]": "no",
                    "buttons[1][text]": no_label,
                },
            ]

            # Tenta JSON
            for payload in json_variants:
                resp = await _post_with_retries(client, endpoint, json_payload=payload)
                if resp is not None:
                    try:
                        return resp.json()
                    except Exception:
                        return {"status": "ok", "http_status": resp.status_code}

            # Tenta FORM
            for payload in form_variants:
                resp = await _post_with_retries(client, endpoint, form_payload=payload)
                if resp is not None:
                    try:
                        return resp.json()
                    except Exception:
                        return {"status": "ok", "http_status": resp.status_code}

    raise RuntimeError("Uazapi menu send failed")


# Alias
async def send_message(
    *,
    phone: str,
    text: str,
    media_url: Optional[str] = None,
    mime_type: Optional[str] = None,
) -> Dict[str, Any]:
    if media_url:
        return await send_whatsapp_message(
            phone=phone,
            content=text,
            type_="media",
            media_url=media_url,
            mime_type=mime_type,
            caption=text,
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