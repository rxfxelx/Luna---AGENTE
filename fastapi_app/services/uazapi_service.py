"""
Integração com Uazapi (WhatsApp) e upload opcional para Baserow.

Expõe:
- send_whatsapp_message(phone, content, type_="text", media_url=None, mime_type=None, caption=None)
- send_message(...) -> alias compatível (usa send_whatsapp_message)
- upload_file_to_baserow(media_url) -> Optional[dict]
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, Optional

import httpx

UAZAPI_BASE_URL = os.getenv("UAZAPI_BASE_URL", "").rstrip("/")
UAZAPI_TOKEN = os.getenv("UAZAPI_TOKEN", "")
UAZAPI_SEND_TEXT_PATH = os.getenv("UAZAPI_SEND_TEXT_PATH", "/send/text")
UAZAPI_SEND_MEDIA_PATH = os.getenv("UAZAPI_SEND_MEDIA_PATH", "/send/media")


def _headers() -> Dict[str, str]:
    return {
        "apikey": UAZAPI_TOKEN,
        "Authorization": f"Bearer {UAZAPI_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _ensure_leading_slash(path: str) -> str:
    return path if path.startswith("/") else f"/{path}"


def _only_digits(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())


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


def _chatid_variants(phone: str) -> Iterable[str]:
    """
    Gera variantes para diferentes instalações:
    - número cru
    - número@s.whatsapp.net
    - número@c.us
    """
    digits = _only_digits(phone) or phone
    seen = set()
    for v in (phone, digits, f"{digits}@s.whatsapp.net", f"{digits}@c.us"):
        if v and v not in seen:
            seen.add(v)
            yield v


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
    Envia mensagem via Uazapi tentando múltiplas formas de payload:

    Texto (tentativas):
      1) {"chatId": <chatid>, "text": <content>}
      2) {"phone" : <digits>,  "text": <content>}
      3) {"number": <digits>,  "message": <content>}

    Mídia (tentativas):
      1) {"chatId": <chatid>, "fileUrl": <url>, "mimeType": <mime>, "caption": <caption>}
      2) {"phone" : <digits>,  "url": <url>,      "mimetype": <mime>, "caption": <caption>}
      3) {"number": <digits>,  "fileUrl": <url>, "mimeType": <mime>, "caption": <caption>}
    """
    if not UAZAPI_BASE_URL:
        raise RuntimeError("UAZAPI_BASE_URL não configurada.")
    if not UAZAPI_TOKEN:
        raise RuntimeError("UAZAPI_TOKEN não configurado.")

    text_path = _ensure_leading_slash(UAZAPI_SEND_TEXT_PATH or "/send/text")
    media_path = _ensure_leading_slash(UAZAPI_SEND_MEDIA_PATH or "/send/media")

    async with httpx.AsyncClient(base_url=UAZAPI_BASE_URL, timeout=30.0) as client:
        if type_ == "text" or not media_url:
            for cid in _chatid_variants(phone):
                candidates = [
                    {"chatId": cid, "text": content},
                    {"phone": _only_digits(cid), "text": content},
                    {"number": _only_digits(cid), "message": content},
                ]
                for payload in candidates:
                    try:
                        resp = await client.post(text_path, json=payload, headers=_headers())
                        if resp.status_code < 400:
                            try:
                                return resp.json()
                            except Exception:
                                return {"status": "ok", "http_status": resp.status_code}
                    except Exception as exc:
                        last_exc = exc  # continua tentando próximo payload
            # Se chegou aqui, nenhuma tentativa funcionou
            raise RuntimeError(f"Uazapi text send failed for phone={phone}")
        else:
            mime = mime_type or _infer_mime_from_url(media_url)
            for cid in _chatid_variants(phone):
                candidates = [
                    {"chatId": cid, "fileUrl": media_url, "mimeType": mime, "caption": caption or content},
                    {"phone": _only_digits(cid), "url": media_url, "mimetype": mime, "caption": caption or content},
                    {"number": _only_digits(cid), "fileUrl": media_url, "mimeType": mime, "caption": caption or content},
                ]
                for payload in candidates:
                    try:
                        resp = await client.post(media_path, json=payload, headers=_headers())
                        if resp.status_code < 400:
                            try:
                                return resp.json()
                            except Exception:
                                return {"status": "ok", "http_status": resp.status_code}
                    except Exception as exc:
                        last_exc = exc
            raise RuntimeError(f"Uazapi media send failed for phone={phone}")


# Back-compat
async def send_message(
    *,
    phone: str,
    text: str,
    media_url: Optional[str] = None,
    mime_type: Optional[str] = None,
) -> Dict[str, Any]:
    if media_url:
        mime = mime_type or _infer_mime_from_url(media_url)
        return await send_whatsapp_message(
            phone=phone, content=text, type_="media", media_url=media_url, mime_type=mime, caption=text
        )
    return await send_whatsapp_message(phone=phone, content=text, type_="text")


# (mantive upload_file_to_baserow do seu arquivo anterior, caso esteja usando)
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
            # endpoint atual e fallback legado
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