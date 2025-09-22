"""
Integração com Uazapi (WhatsApp) e upload opcional para Baserow.

Expõe:
- send_whatsapp_message(phone, content, type_="text", media_url=None, mime_type=None, caption=None) -> dict
- send_message(...) -> alias compatível (chama send_whatsapp_message)
- upload_file_to_baserow(media_url) -> Optional[dict]

ENVs esperadas:
- UAZAPI_BASE_URL        (ex.: https://sua-instancia.uazapi.com)
- UAZAPI_TOKEN           (token da INSTÂNCIA — não o admin)
- UAZAPI_SEND_TEXT_PATH  (default: /send/text)
- UAZAPI_SEND_MEDIA_PATH (default: /send/media)
- BASEROW_BASE_URL       (opcional, p/ upload de mídia)
- BASEROW_API_TOKEN      (opcional, p/ upload de mídia)
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import httpx

# ---- Uazapi config ----
UAZAPI_BASE_URL = os.getenv("UAZAPI_BASE_URL", "").rstrip("/")
UAZAPI_TOKEN = os.getenv("UAZAPI_TOKEN", "")
UAZAPI_SEND_TEXT_PATH = os.getenv("UAZAPI_SEND_TEXT_PATH", "/send/text")
UAZAPI_SEND_MEDIA_PATH = os.getenv("UAZAPI_SEND_MEDIA_PATH", "/send/media")

# ---- Baserow config (opcional) ----
BASEROW_BASE_URL = os.getenv("BASEROW_BASE_URL", "").rstrip("/")
BASEROW_API_TOKEN = os.getenv("BASEROW_API_TOKEN", "")


def _headers() -> Dict[str, str]:
    # Alguns setups aceitam 'apikey', outros 'Bearer'. Enviamos ambos.
    return {
        "apikey": UAZAPI_TOKEN,
        "Authorization": f"Bearer {UAZAPI_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _ensure_leading_slash(path: str) -> str:
    return path if path.startswith("/") else f"/{path}"


def _infer_mime_from_url(url: str) -> str:
    lower = url.lower()
    if lower.endswith(".jpg") or lower.endswith(".jpeg"):
        return "image/jpeg"
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".gif"):
        return "image/gif"
    if lower.endswith(".mp4"):
        return "video/mp4"
    if lower.endswith(".pdf"):
        return "application/pdf"
    if lower.endswith(".mp3"):
        return "audio/mpeg"
    if lower.endswith(".ogg") or lower.endswith(".opus"):
        return "audio/ogg"
    return "application/octet-stream"


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
    Envia mensagem via Uazapi.

    - Texto: payload = {"chatId": phone, "text": content}
    - Mídia: payload = {"chatId": phone, "fileUrl": media_url, "mimeType": mime, "caption": caption|content}
    """
    if not UAZAPI_BASE_URL:
        raise RuntimeError("UAZAPI_BASE_URL não configurada.")
    if not UAZAPI_TOKEN:
        raise RuntimeError("UAZAPI_TOKEN não configurado.")

    text_path = _ensure_leading_slash(UAZAPI_SEND_TEXT_PATH or "/send/text")
    media_path = _ensure_leading_slash(UAZAPI_SEND_MEDIA_PATH or "/send/media")

    async with httpx.AsyncClient(base_url=UAZAPI_BASE_URL, timeout=30.0) as client:
        if type_ == "text" or not media_url:
            endpoint = text_path
            payload = {"chatId": phone, "text": content}
        else:
            endpoint = media_path
            mime = mime_type or _infer_mime_from_url(media_url)
            payload = {
                "chatId": phone,
                "fileUrl": media_url,
                "mimeType": mime,
                "caption": caption if caption is not None else content,
            }

        resp = await client.post(endpoint, json=payload, headers=_headers())
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"status": "ok", "http_status": resp.status_code}


# Back-compat: sua versão anterior chamava 'send_message'.
async def send_message(
    *,
    phone: str,
    text: str,
    media_url: Optional[str] = None,
    mime_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Wrapper compatível que delega a send_whatsapp_message."""
    if media_url:
        mime = mime_type or _infer_mime_from_url(media_url)
        return await send_whatsapp_message(
            phone=phone, content=text, type_="media", media_url=media_url, mime_type=mime, caption=text
        )
    return await send_whatsapp_message(phone=phone, content=text, type_="text")


async def upload_file_to_baserow(media_url: str) -> Optional[dict]:
    """
    Faz download do arquivo em media_url e tenta subir como "user file" no Baserow.

    Endpoints tentados (nessa ordem):
      1) /api/user-files/upload-file/  (padrão atual)
      2) /api/userfiles/upload_file/   (fallback legado)
    """
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

            endpoints = [
                f"{BASEROW_BASE_URL}/api/user-files/upload-file/",
                f"{BASEROW_BASE_URL}/api/userfiles/upload_file/",
            ]

            last_exc: Optional[Exception] = None
            for upload_url in endpoints:
                try:
                    upload_resp = await client.post(upload_url, headers=headers, files=files)
                    if upload_resp.status_code < 400:
                        return upload_resp.json()
                except Exception as exc:  # tente próximo endpoint
                    last_exc = exc

            if last_exc:
                print(f"Erro no upload Baserow (tentativas esgotadas): {last_exc}")
            else:
                print("Upload Baserow falhou: status HTTP não OK nos endpoints testados.")
            return None

        except Exception as exc:
            print(f"Erro ao baixar/enviar arquivo p/ Baserow: {exc}")
            return None
