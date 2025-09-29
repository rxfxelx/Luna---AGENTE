# fastapi_app/services/uazapi_service.py
"""
Integração com Uazapi (WhatsApp) e upload opcional para Baserow.

Expõe:
- send_whatsapp_message(phone, content, type_="text", media_url=None, mime_type=None, caption=None)
- send_menu_interesse(phone, text, yes_label, no_label, footer_text=None)
- send_message(...) -> alias compatível (usa send_whatsapp_message)
- upload_file_to_baserow(media_url) -> Optional[dict]
- normalize_number(phone) -> compatibilidade com módulos antigos
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, Optional, List, Tuple

import httpx

# -------------------- Config --------------------
UAZAPI_BASE_URL = os.getenv("UAZAPI_BASE_URL", "").rstrip("/")
UAZAPI_TOKEN = os.getenv("UAZAPI_TOKEN", "")

# 'token' | 'apikey' | 'authorization_bearer'
UAZAPI_AUTH_HEADER_NAME = os.getenv("UAZAPI_AUTH_HEADER_NAME", "token").lower()

# Rotas vindas do ambiente (usadas junto com fallbacks)
UAZAPI_SEND_TEXT_PATH  = os.getenv("UAZAPI_SEND_TEXT_PATH", "/send/text")
UAZAPI_SEND_MEDIA_PATH = os.getenv("UAZAPI_SEND_MEDIA_PATH", "/send/media")
UAZAPI_SEND_MENU_PATH  = os.getenv("UAZAPI_SEND_MENU_PATH", "/send/menu")

# Fallbacks de rotas (cobrem diferentes versões da API Uazapi)
_TEXT_FALLBACKS  = ["/sendMessage", "/api/sendText", "/api/sendMessage", "/api/send/message", "/send-message"]
_MEDIA_FALLBACKS = ["/send/file", "/api/sendFile", "/api/sendMedia", "/file/send"]
_MENU_FALLBACKS  = ["/send/menu", "/sendMenu", "/api/sendMenu", "/menus/send"]


def _headers() -> dict:
    """Retorna header de autenticação (com nome configurável)."""
    if not UAZAPI_TOKEN:
        raise RuntimeError("UAZAPI_TOKEN não configurado.")
    return {UAZAPI_AUTH_HEADER_NAME: UAZAPI_TOKEN}


def _ensure_leading_slash(path: str) -> str:
    return path if path.startswith("/") else f"/{path}"


def _dedup(seq: Iterable[str]) -> List[str]:
    """Remove duplicatas preservando ordem (case-sensitive)."""
    seen: List[str] = []
    for item in seq:
        if item not in seen:
            seen.append(item)
    return seen


def _text_endpoints() -> list[str]:
    return _dedup([UAZAPI_SEND_TEXT_PATH, "/send/text"] + _TEXT_FALLBACKS)


def _media_endpoints() -> list[str]:
    return _dedup([UAZAPI_SEND_MEDIA_PATH, "/send/media"] + _MEDIA_FALLBACKS)


def _menu_endpoints() -> list[str]:
    return _dedup([UAZAPI_SEND_MENU_PATH, "/send/menu"] + _MENU_FALLBACKS)


async def _download_bytes(url: str) -> Tuple[Optional[bytes], Optional[str]]:
    """
    Faz download de um arquivo para bytes. Usa filename do fim da URL como nome,
    ou infere um genérico "file" quando não disponível. Aceita URLs http/https e "data:".
    OBS: permite link Baserow `attachment_id` (neste caso, acessa o próprio Baserow).
    Retorna (bytes, filename) ou (None, None) em erro.
    """
    if not url:
        return None, None
    try:
        # Suporte a data URLs (base64)
        if url.lower().startswith("data:"):
            # data:[<mediatype>][;base64],<data>
            parts = url.split(",", 1)
            header = parts[0] if len(parts) > 1 else ""
            data = parts[1] if len(parts) > 1 else ""
            mime = ""
            # separa tipo e encoding
            if ";" in header:
                mime, enc = header[5:].split(";", 1)
            if "base64" in header:
                import base64
                return base64.b64decode(data), f"file.{mime.split('/')[-1] or 'bin'}"
            else:
                # percent-encoding
                from urllib.parse import unquote_to_bytes
                return unquote_to_bytes(data), f"file.{mime.split('/')[-1] or 'bin'}"

        # Se URL do Baserow for fornecida como "attachment_id", busca no próprio Baserow
        # (espera env BASEROW_BASE_URL e BASEROW_API_TOKEN configurados)
        if url.isdigit():
            from . import upload_file_to_baserow
            up = await upload_file_to_baserow(url)
            if up and "url" in up:
                url = up["url"]
            else:
                return None, None

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            file_bytes = resp.content
            # extrai nome do arquivo da URL (ou "file" se não houver)
            filename = url.split("/")[-1].split("?")[0] or "file"
            return file_bytes, filename
    except Exception as exc:
        print(f"Falha no download da mídia: {exc}")
        return None, None


def _infer_mime_from_url(url: str) -> str:
    """Inferência simples de MIME type a partir da extensão do arquivo na URL."""
    l = (url or "").lower()
    if l.endswith(".jpg") or l.endswith(".jpeg"):
        return "image/jpeg"
    if l.endswith(".png"):
        return "image/png"
    if l.endswith(".gif"):
        return "image/gif"
    if l.endswith(".webp"):
        return "image/webp"
    if l.endswith(".mp4"):
        return "video/mp4"
    if l.endswith(".mp3"):
        return "audio/mpeg"
    if l.endswith(".ogg"):
        return "audio/ogg"
    if l.endswith(".wav"):
        return "audio/wav"
    if l.endswith(".m4a"):
        return "audio/mp4"
    if l.endswith(".pdf"):
        return "application/pdf"
    if l.endswith(".csv"):
        return "text/csv"
    # Fallback genérico
    return "application/octet-stream"


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
    Envia mensagem via API do Uazapi (texto, mídia ou menu). Retorna dict de resposta em caso de sucesso,
    ou levanta RuntimeError em caso de falha.
    """
    if not UAZAPI_BASE_URL:
        raise RuntimeError("UAZAPI_BASE_URL não configurada.")

    headers = _headers()
    digits = _only_digits(phone) or phone

    async with httpx.AsyncClient(base_url=UAZAPI_BASE_URL, timeout=60.0) as client:
        # ===================== TEXT =====================
        if type_ == "text" or not media_url:
            for endpoint in _text_endpoints():
                if endpoint == "/send/text":
                    candidates: List[Dict[str, Any]] = [
                        {"number": digits, "text": content},
                        {"phone": digits, "text": content},
                        {"jid": f"{digits}@s.whatsapp.net", "text": content},
                        {"chatId": f"{digits}@c.us", "text": content},
                        {"number": digits, "message": content},
                        {"phone": digits, "message": content},
                    ]
                else:
                    candidates = []
                    for cid in _chatid_variants(digits):
                        candidates.append({"chatId": cid, "text": content})
                        candidates.append({"chatId": cid, "message": content})
                    candidates.append({"phone": digits, "text": content})
                    candidates.append({"number": digits, "text": content})
                    candidates.append({"phone": digits, "message": content})
                    candidates.append({"number": digits, "message": content})

                for payload in candidates:
                    try:
                        resp = await client.post(_ensure_leading_slash(endpoint), json=payload, headers=headers)
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

        # ===================== MEDIA =====================
        # Importante: a sua instância exige multipart + querystring 'number'
        # Determine MIME type from URL if not provided
        mime = mime_type or _infer_mime_from_url(media_url)
        base_caption = (caption or content or "").strip()
        # If the message type is 'video' or the URL indicates a video file, attempt JSON media send first
        if type_ == "video" or (mime and mime.startswith("video/")):
            candidate_payloads: List[Dict[str, Any]] = []
            base_payload = {"mediatype": "video", "media_url": media_url, "caption": base_caption}
            candidate_payloads.append({**base_payload, "number": digits})
            candidate_payloads.append({**base_payload, "phone": digits})
            candidate_payloads.append({**base_payload, "jid": f"{digits}@s.whatsapp.net"})
            candidate_payloads.append({**base_payload, "chatId": f"{digits}@c.us"})
            for endpoint in _media_endpoints():
                endpoint = _ensure_leading_slash(endpoint)
                if "sendFile" in endpoint or "/send/file" in endpoint or "/file/send" in endpoint:
                    continue
                for payload in candidate_payloads:
                    try:
                        resp = await client.post(endpoint, json=payload, headers=headers)
                        if resp.status_code < 400:
                            try:
                                return resp.json()
                            except Exception:
                                return {"status": "ok", "http_status": resp.status_code}
                        else:
                            print(f"[uazapi] {endpoint} JSON{list(payload.keys())} {resp.status_code} body={resp.text[:200].replace(chr(10),' ')}")
                    except Exception as exc:
                        print(f"[uazapi] exception on {endpoint} JSON{list(payload.keys())}: {exc}")
            # If no JSON attempt succeeded, proceed with multipart upload fallback
        file_bytes, filename = await _download_bytes(media_url or "")
        if not file_bytes:
            raise RuntimeError("Falha ao baixar o arquivo de mídia para upload multipart.")
        files = {"file": (filename or "file", file_bytes, mime)}
        # Attempt sending media via multipart form (supports images, videos, documents)
        for endpoint in _media_endpoints():
            endpoint = _ensure_leading_slash(endpoint)
            # --- (A) multipart com 'number' na QUERY (principal) ---
            query_variants = [
                {"number": digits},
                {"phone": digits},
                {"chatId": f"{digits}@c.us"},
                {"jid": f"{digits}@s.whatsapp.net"},
            ]
            for q in query_variants:
                try:
                    resp = await client.post(endpoint, params=q, data={"caption": base_caption}, files=files, headers=headers)
                    if resp.status_code < 400:
                        try:
                            return resp.json()
                        except Exception:
                            return {"status": "ok", "http_status": resp.status_code}
                    else:
                        print(f"[uazapi] {endpoint} QUERY{list(q.keys())} FORM {resp.status_code} body={resp.text[:200].replace(chr(10),' ')}")
                except Exception as exc:
                    print(f"[uazapi] exception on {endpoint} QUERY{list(q.keys())} multipart: {exc}")
            # --- (B) multipart com 'number' no BODY (algumas distros) ---
            form_variants = [
                {"number": digits, "caption": base_caption},
                {"phone": digits, "caption": base_caption},
                {"chatId": f"{digits}@c.us", "caption": base_caption},
                {"jid": f"{digits}@s.whatsapp.net", "caption": base_caption},
            ]
            for form in form_variants:
                try:
                    resp = await client.post(endpoint, data=form, files=files, headers=headers)
                    if resp.status_code < 400:
                        try:
                            return resp.json()
                        except Exception:
                            return {"status": "ok", "http_status": resp.status_code}
                    else:
                        print(f"[uazapi] {endpoint} FORM{list(form.keys())} {resp.status_code} body={resp.text[:200].replace(chr(10),' ')}")
                except Exception as exc:
                    print(f"[uazapi] exception on {endpoint} FORM{list(form.keys())}: {exc}")
        # Se nada funcionou:
        raise RuntimeError(f"Uazapi media send failed for phone={phone}")


# Auxiliar para gerar variantes de chatId (grupos, etc.)
def _chatid_variants(digits: str) -> List[str]:
    # Adiciona sufixos para atender distribuições que usem '@c.us' (padrão), '@s.whatsapp.net', etc.
    return [f"{digits}@c.us", f"{digits}@s.whatsapp.net"]


# -------------------- Menu --------------------
async def send_menu_interesse(
    phone: str,
    text: str,
    yes_label: str,
    no_label: str,
    footer_text: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Envia um menu interativo de "Sim/Não" via Uazapi.
    """
    if not UAZAPI_BASE_URL:
        raise RuntimeError("UAZAPI_BASE_URL não configurada.")
    headers = _headers()
    digits = _only_digits(phone) or phone
    payload = {
        "number": digits,
        "buttonText": text,
        "description": footer_text or "",
        "buttons": [
            {"id": "YES", "body": yes_label},
            {"id": "NO", "body": no_label},
        ],
    }
    async with httpx.AsyncClient(base_url=UAZAPI_BASE_URL, timeout=60.0) as client:
        for endpoint in _menu_endpoints():
            endpoint = _ensure_leading_slash(endpoint)
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
                print(f"[uazapi] exception on {endpoint}: {exc}")
        raise RuntimeError(f"Uazapi menu send failed for phone={phone}")


# Retrocompat: algumas integrações esperam "send_message" em vez de "send_whatsapp_message"
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


def _only_digits(s: str) -> str:
    return "".join(ch for ch in str(s) if ch.isdigit())


def normalize_number(s: str) -> str:  # retrocompat
    return _only_digits(s)
