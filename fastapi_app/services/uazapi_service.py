"""
Integração com Uazapi (WhatsApp) e upload opcional para Baserow.

Expõe:
- send_whatsapp_message(phone, content, type_="text", media_url=None, mime_type=None, caption=None)
- send_message(...) -> alias compatível (usa send_whatsapp_message)
- send_menu(phone, text, choices, footer) -> ENV seleciona o endpoint (padrão: /send/menu)
- upload_file_to_baserow(media_url) -> Optional[dict]

Observações:
- Para '/send/text', prioriza payload {'number': '<digits>', 'text': '...'} (evita 400 Missing required fields).
- Para '/send/media', prioriza {'number': '<digits>', 'url': '...','caption': '...'}.
- Headers flexíveis: 'token' (padrão), 'apikey' ou 'Authorization: Bearer'.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, Optional

import httpx

# -------------------- Config --------------------
UAZAPI_BASE_URL = os.getenv("UAZAPI_BASE_URL", "").rstrip("/")
UAZAPI_TOKEN = os.getenv("UAZAPI_TOKEN", "")

# 'token' | 'apikey' | 'authorization_bearer'
UAZAPI_AUTH_HEADER_NAME = os.getenv("UAZAPI_AUTH_HEADER_NAME", "token").lower()

UAZAPI_SEND_TEXT_PATH = os.getenv("UAZAPI_SEND_TEXT_PATH", "/send/text")
UAZAPI_SEND_MEDIA_PATH = os.getenv("UAZAPI_SEND_MEDIA_PATH", "/send/media")
UAZAPI_SEND_MENU_PATH = os.getenv("UAZAPI_SEND_MENU_PATH", "/send/menu")

# Fallbacks comuns observados em instalações diferentes
_TEXT_FALLBACKS = ["/send/message", "/api/sendText", "/sendText", "/messages/send", "/message/send"]
_MEDIA_FALLBACKS = ["/send/file", "/api/sendFile", "/api/sendMedia"]

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
    """Monta headers aceitando diferentes esquemas de autenticação."""
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
        # Fallback seguro
        base["token"] = UAZAPI_TOKEN
    return base

def _chatid_variants(phone: str):
    digits = _only_digits(phone) or phone
    for v in (f"{digits}@c.us", f"{digits}@s.whatsapp.net", digits):
        yield v

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
    - Texto: sempre tenta '/send/text' primeiro com {"number": "...", "text": "..."}.
    - Mídia: tenta '/send/media' (number/url/caption) e fallbacks compatíveis.
    """
    if not UAZAPI_BASE_URL:
        raise RuntimeError("UAZAPI_BASE_URL não configurada.")

    headers = _headers()
    digits = _only_digits(phone) or phone

    async with httpx.AsyncClient(base_url=UAZAPI_BASE_URL, timeout=30.0) as client:
        # --- TEXTO ---
        if type_ == "text" or not media_url:
            for endpoint in _dedup(["/send/text", UAZAPI_SEND_TEXT_PATH] + _TEXT_FALLBACKS):
                candidates = [
                    {"number": digits, "text": content},                         # preferido
                    {"phone": digits, "text": content},                          # variação
                ]
                # manter compatibilidade com chatId em instalações antigas
                candidates.extend({"chatId": cid, "text": content} for cid in _chatid_variants(digits))

                for payload in candidates:
                    try:
                        resp = await client.post(endpoint, json=payload, headers=headers)
                        if resp.status_code < 400:
                            return resp.json() if resp.content else {"status": "ok"}
                        else:
                            print(f"[uazapi] {endpoint} {resp.status_code} body={resp.text[:200].replace(chr(10),' ')}")
                    except Exception as exc:
                        print(f"[uazapi] exception {endpoint} payload={list(payload.keys())}: {exc}")
            raise RuntimeError(f"Uazapi text send failed for phone={phone}")

        # --- MÍDIA ---
        else:
            for endpoint in _dedup([UAZAPI_SEND_MEDIA_PATH, "/send/media"] + _MEDIA_FALLBACKS):
                candidates = [
                    {"number": digits, "url": media_url, "caption": caption or content},  # preferido
                    {"phone": digits, "url": media_url, "caption": caption or content},   # variação
                ]
                # fallback 'fileUrl' + 'mimeType' para variantes Baileys
                candidates.extend({
                    "chatId": cid,
                    "fileUrl": media_url,
                    "mimeType": mime_type or "application/octet-stream",
                    "caption": caption or content,
                } for cid in _chatid_variants(digits))

                for payload in candidates:
                    try:
                        resp = await client.post(endpoint, json=payload, headers=headers)
                        if resp.status_code < 400:
                            return resp.json() if resp.content else {"status": "ok"}
                        else:
                            print(f"[uazapi] {endpoint} {resp.status_code} body={resp.text[:200].replace(chr(10),' ')}")
                    except Exception as exc:
                        print(f"[uazapi] exception {endpoint} payload={list(payload.keys())}: {exc}")
            raise RuntimeError(f"Uazapi media send failed for phone={phone}")

async def send_menu(phone: str, *, text: str, choices: list[str], footer: str) -> Dict[str, Any]:
    """
    Envia menu (botões) interativo via Uazapi.
    Documentação: https://docs.uazapi.com/endpoint/post/send~menu
    """
    if not UAZAPI_BASE_URL:
        raise RuntimeError("UAZAPI_BASE_URL não configurada.")
    endpoint = _ensure_leading_slash(UAZAPI_SEND_MENU_PATH or "/send/menu")
    payload = {
        "number": _only_digits(phone) or phone,
        "type": "button",
        "text": text,
        "choices": choices,
        "footerText": footer,
    }
    async with httpx.AsyncClient(base_url=UAZAPI_BASE_URL, timeout=30.0) as client:
        resp = await client.post(endpoint, json=payload, headers=_headers())
        resp.raise_for_status()
        return resp.json() if resp.content else {"status": "ok"}

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
