"""
Integração com UAZAPI (WhatsApp) e utilitários para Baserow.

Expõe:
- send_whatsapp_message(phone, content, type_="text", media_url=None, mime_type=None, caption=None)
- send_menu_interesse(phone, text, yes_label, no_label, footer_text=None)
- send_message(...) -> alias compatível (usa send_whatsapp_message)
- upload_file_to_baserow(source) -> Optional[dict]   # envia arquivo (URL) p/ Baserow ou resolve metadados por ID
- normalize_number(phone)

Notas:
- Para enviar VÍDEO como mídia no WhatsApp, use type_="video" e forneça media_url .mp4 público.
"""

from __future__ import annotations

import os
import json
from typing import Any, Dict, Iterable, Optional, List, Tuple

import httpx


# -------------------- Config UAZAPI --------------------
UAZAPI_BASE_URL = os.getenv("UAZAPI_BASE_URL", "").rstrip("/")
UAZAPI_TOKEN = os.getenv("UAZAPI_TOKEN", "")

# Nome do header de auth na sua instância (ex.: "token", "apikey", "authorization", "authorization_bearer")
UAZAPI_AUTH_HEADER_NAME = os.getenv("UAZAPI_AUTH_HEADER_NAME", "token").lower()

# Rotas (permite override por ENV); inclui fallbacks para variações de instância
UAZAPI_SEND_TEXT_PATH = os.getenv("UAZAPI_SEND_TEXT_PATH", "/send/text")
UAZAPI_SEND_MEDIA_PATH = os.getenv("UAZAPI_SEND_MEDIA_PATH", "/send/media")
UAZAPI_SEND_MENU_PATH = os.getenv("UAZAPI_SEND_MENU_PATH", "/send/menu")

_TEXT_FALLBACKS = ["/sendMessage", "/api/sendText", "/api/sendMessage", "/api/send/message", "/send-message"]
_MEDIA_FALLBACKS = ["/send/file", "/api/sendFile", "/api/sendMedia", "/file/send"]
# menus: acrescentados fallbacks mais comuns em distros (buttons)
_MENU_FALLBACKS = [
    "/send/menu", "/sendMenu", "/api/sendMenu", "/menus/send",
    "/send/buttons", "/sendButtons", "/api/sendButtons", "/buttons/send"
]


# -------------------- Config BASEROW --------------------
BASEROW_BASE_URL = os.getenv("BASEROW_BASE_URL", "").rstrip("/")
BASEROW_API_TOKEN = os.getenv("BASEROW_API_TOKEN", "")


def _headers() -> dict:
    """Header de autenticação do UAZAPI."""
    if not UAZAPI_TOKEN:
        raise RuntimeError("UAZAPI_TOKEN não configurado.")
    # Suporta variantes simples; ajuste se sua instância exigir Bearer.
    if UAZAPI_AUTH_HEADER_NAME in {"authorization_bearer", "authorization"}:
        return {"Authorization": f"Bearer {UAZAPI_TOKEN}"}
    return {UAZAPI_AUTH_HEADER_NAME: UAZAPI_TOKEN}


def _ensure_leading_slash(path: str) -> str:
    return path if path.startswith("/") else f"/{path}"


def _dedup(seq: Iterable[str]) -> List[str]:
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
    return _dedup([UAZAPI_SEND_MENU_PATH] + _MENU_FALLBACKS)


def _path_without_query(url: str) -> str:
    if not url:
        return ""
    cut = url.split("#", 1)[0]
    cut = cut.split("?", 1)[0]
    return cut


def _infer_mime_from_url(url: str) -> str:
    """
    Inferência simples de MIME type a partir da extensão do arquivo na URL.
    Considera apenas o caminho (ignora ? e #), para funcionar com links assinados (S3, Baserow, etc).
    """
    path = _path_without_query((url or "").strip()).lower()
    if path.endswith(".jpg") or path.endswith(".jpeg"):
        return "image/jpeg"
    if path.endswith(".png"):
        return "image/png"
    if path.endswith(".gif"):
        return "image/gif"
    if path.endswith(".webp"):
        return "image/webp"
    if path.endswith(".mp4") or path.endswith(".m4v") or path.endswith(".mov") or path.endswith(".webm"):
        return "video/mp4"  # usa mp4 como padrão seguro; instâncias tratam corretamente
    if path.endswith(".mp3"):
        return "audio/mpeg"
    if path.endswith(".ogg"):
        return "audio/ogg"
    if path.endswith(".wav"):
        return "audio/wav"
    if path.endswith(".m4a"):
        return "audio/mp4"
    if path.endswith(".pdf"):
        return "application/pdf"
    if path.endswith(".csv"):
        return "text/csv"
    return "application/octet-stream"


def _guess_type_for_api(mime: str, explicit: Optional[str]) -> str:
    """
    Mapeia para os tipos aceitos pela UAZAPI: image | video | audio | document
    """
    t = (explicit or "").strip().lower()
    if t in {"image", "video", "audio", "document"}:
        return t
    m = (mime or "").lower()
    if m.startswith("image/"):
        return "image"
    if m.startswith("video/"):
        return "video"
    if m.startswith("audio/"):
        return "audio"
    # todo o resto vai como documento
    return "document"


async def _download_bytes(url: str) -> Tuple[Optional[bytes], Optional[str]]:
    """
    Faz download de um arquivo para bytes.
    Suporta:
    - data: URLs
    - http(s) público
    - ID numérico do Baserow (resolve via upload_file_to_baserow)
    Retorna (bytes, filename) ou (None, None).
    """
    if not url:
        return None, None
    try:
        # Suporte a data URLs (ex.: data:video/mp4;base64,...)
        if url.lower().startswith("data:"):
            parts = url.split(",", 1)
            header = parts[0] if len(parts) > 1 else ""
            data = parts[1] if len(parts) > 1 else ""
            mime = ""
            if ";" in header:
                mime, _enc = header[5:].split(";", 1)
            if "base64" in header:
                import base64
                return base64.b64decode(data), f"file.{(mime.split('/')[-1] or 'bin')}"
            else:
                from urllib.parse import unquote_to_bytes
                return unquote_to_bytes(data), f"file.{(mime.split('/')[-1] or 'bin')}"

        # Se receber um ID numérico de arquivo do Baserow, resolve para URL pública
        if url.isdigit():
            up = await upload_file_to_baserow(url)  # retorna metadados ou None
            if up and isinstance(up, dict) and up.get("url"):
                url = up["url"]
            else:
                return None, None

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            file_bytes = resp.content
            filename = _path_without_query(url).split("/")[-1] or "file"
            return file_bytes, filename
    except Exception as exc:
        print(f"Falha no download da mídia: {exc}")
        return None, None


# -------------------- Envio WhatsApp --------------------
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
    Envia mensagem via UAZAPI (texto, mídia, menu).

    Mídia (recomendado): envia para /send/media com JSON:
        {"number":"55..","type":"video|image|audio|document","file":"https://...","caption":"..."}
    Fallback: tenta multipart em endpoints alternativos (/send/file, /api/sendFile, ...).

    Retorna dict (JSON) em sucesso; levanta RuntimeError em falha.
    """
    if not UAZAPI_BASE_URL:
        raise RuntimeError("UAZAPI_BASE_URL não configurada.")

    headers = _headers()
    digits = _only_digits(phone) or phone
    plus_digits = digits if str(digits).startswith("+") else f"+{digits}"

    async with httpx.AsyncClient(base_url=UAZAPI_BASE_URL, timeout=60.0) as client:
        # ============ TEXTO ============
        if type_ == "text" or not media_url:
            for endpoint in _text_endpoints():
                endpoint = _ensure_leading_slash(endpoint)

                # Variações de destino e conteúdo para diferentes distros
                dest_variants: List[Dict[str, Any]] = [
                    {"number": digits}, {"number": plus_digits},
                    {"phone": digits},  {"phone": plus_digits},
                    {"to": digits},     {"to": plus_digits},
                    {"chatId": f"{digits}@c.us"},
                    {"jid": f"{digits}@s.whatsapp.net"},
                ]
                text_variants: List[Dict[str, Any]] = [
                    {"text": content},
                    {"message": content},
                    {"body": content},
                ]

                # (1) JSON attempts
                for d in dest_variants:
                    for t in text_variants:
                        payload = {**d, **t}
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

                # (2) form-urlencoded attempts
                for d in dest_variants:
                    for t in text_variants:
                        form = {**d, **t}
                        try:
                            resp = await client.post(endpoint, data=form, headers=headers)
                            if resp.status_code < 400:
                                try:
                                    return resp.json()
                                except Exception:
                                    return {"status": "ok", "http_status": resp.status_code}
                            else:
                                print(f"[uazapi] {endpoint} FORM{list(form.keys())} {resp.status_code} body={resp.text[:200].replace(chr(10),' ')}")
                        except Exception as exc:
                            print(f"[uazapi] exception on {endpoint} FORM{list(form.keys())}: {exc}")

                # (3) params + body attempts (alguns endpoints esperam number na query)
                for d in dest_variants:
                    try:
                        resp = await client.post(endpoint, params=d, data={"text": content}, headers=headers)
                        if resp.status_code < 400:
                            try:
                                return resp.json()
                            except Exception:
                                return {"status": "ok", "http_status": resp.status_code}
                        else:
                            print(f"[uazapi] {endpoint} PARAMS{list(d.keys())}+FORM[text] {resp.status_code} body={resp.text[:200].replace(chr(10),' ')}")
                        resp = await client.post(endpoint, params=d, data={"message": content}, headers=headers)
                        if resp.status_code < 400:
                            try:
                                return resp.json()
                            except Exception:
                                return {"status": "ok", "http_status": resp.status_code}
                        else:
                            print(f"[uazapi] {endpoint} PARAMS{list(d.keys())}+FORM[message] {resp.status_code} body={resp.text[:200].replace(chr(10),' ')}")
                    except Exception as exc:
                        print(f"[uazapi] exception on {endpoint} PARAMS{list(d.keys())}: {exc}")

            raise RuntimeError(f"UAZAPI text send failed for phone={phone}")

        # ============ MÍDIA (imagem / vídeo / áudio / documento) ============
        mime = mime_type or _infer_mime_from_url(media_url)
        api_media_type = _guess_type_for_api(mime, type_)
        base_caption = (caption or content or "").strip()

        # 1) Tenta JSON canônico via /send/media (o mais compatível com a doc)
        #    {"number":"...", "type":"video", "file":"https://...", "caption":"..."}
        dest_variants: List[Dict[str, Any]] = [
            {"number": digits}, {"number": plus_digits},
            {"phone": digits}, {"phone": plus_digits},
            {"to": digits}, {"to": plus_digits},
            {"chatId": f"{digits}@c.us"},
            {"jid": f"{digits}@s.whatsapp.net"},
        ]
        for endpoint in _media_endpoints():
            endpoint = _ensure_leading_slash(endpoint)
            if endpoint in {"/send/file", "/api/sendFile", "/file/send"}:
                # esses são mais adequados ao fallback multipart
                continue

            for d in dest_variants:
                # tente com "number"/"phone"/"to" etc, mas padronize como "number" quando possível
                payload = {**d, "type": api_media_type, "file": media_url, "caption": base_caption}
                try:
                    resp = await client.post(endpoint, json=payload, headers=headers)
                    if resp.status_code < 400:
                        try:
                            return resp.json()
                        except Exception:
                            return {"status": "ok", "http_status": resp.status_code}
                    else:
                        print(f"[uazapi] {endpoint} MEDIA-JSON{list(payload.keys())} {resp.status_code} body={resp.text[:200].replace(chr(10),' ')}")
                except Exception as exc:
                    print(f"[uazapi] exception on {endpoint} MEDIA-JSON{list(payload.keys())}: {exc}")

        # 2) Fallback: baixa arquivo e envia multipart (cobre variações)
        file_bytes, filename = await _download_bytes(media_url or "")
        if not file_bytes:
            raise RuntimeError("Falha ao baixar o arquivo de mídia para upload multipart.")
        files = {"file": (filename or "file", file_bytes, mime)}
        for endpoint in _media_endpoints():
            endpoint = _ensure_leading_slash(endpoint)

            # (A) 'number' na query (muitas instâncias exigem)
            query_variants = [
                {"number": digits, "type": api_media_type},
                {"number": plus_digits, "type": api_media_type},
                {"phone": digits, "type": api_media_type},
                {"phone": plus_digits, "type": api_media_type},
                {"to": digits, "type": api_media_type},
                {"to": plus_digits, "type": api_media_type},
                {"chatId": f"{digits}@c.us", "type": api_media_type},
                {"jid": f"{digits}@s.whatsapp.net", "type": api_media_type},
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
                        print(f"[uazapi] {endpoint} QUERY{list(q.keys())} MULTIPART {resp.status_code} body={resp.text[:200].replace(chr(10),' ')}")
                except Exception as exc:
                    print(f"[uazapi] exception on {endpoint} QUERY{list(q.keys())} multipart: {exc}")

            # (B) 'number' no body (outras variantes)
            form_variants = [
                {"number": digits, "caption": base_caption, "type": api_media_type},
                {"number": plus_digits, "caption": base_caption, "type": api_media_type},
                {"phone": digits, "caption": base_caption, "type": api_media_type},
                {"phone": plus_digits, "caption": base_caption, "type": api_media_type},
                {"to": digits, "caption": base_caption, "type": api_media_type},
                {"to": plus_digits, "caption": base_caption, "type": api_media_type},
                {"chatId": f"{digits}@c.us", "caption": base_caption, "type": api_media_type},
                {"jid": f"{digits}@s.whatsapp.net", "caption": base_caption, "type": api_media_type},
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
                        print(f"[uazapi] {endpoint} FORM{list(form.keys())} MULTIPART {resp.status_code} body={resp.text[:200].replace(chr(10),' ')}")
                except Exception as exc:
                    print(f"[uazapi] exception on {endpoint} FORM{list(form.keys())}: {exc}")

        raise RuntimeError(f"UAZAPI media send failed for phone={phone}")


def _chatid_variants(digits: str) -> List[str]:
    return [f"{digits}@c.us", f"{digits}@s.whatsapp.net"]


# -------------------- Menu Interativo --------------------
def _flatten_for_form(d: Dict[str, Any]) -> Dict[str, Any]:
    """Converte valores dict/list em JSON string para envio form-urlencoded."""
    out: Dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, (dict, list)):
            out[k] = json.dumps(v, ensure_ascii=False)
        else:
            out[k] = v
    return out


async def send_menu_interesse(
    phone: str,
    text: str,
    yes_label: str,
    no_label: str,
    footer_text: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Envia menu/botões 'Sim/Não' cobrindo variações comuns entre distros UAZAPI.
    Tenta múltiplos formatos (JSON, form-urlencoded, query+body) e múltiplas rotas.
    """
    if not UAZAPI_BASE_URL:
        raise RuntimeError("UAZAPI_BASE_URL não configurada.")
    headers = _headers()
    digits = _only_digits(phone) or phone
    plus_digits = digits if str(digits).startswith("+") else f"+{digits}"

    # Variações de destino aceitas em diferentes instâncias
    dest_variants: List[Dict[str, Any]] = [
        {"number": digits}, {"number": plus_digits},
        {"phone": digits}, {"phone": plus_digits},
        {"to": digits}, {"to": plus_digits},
        {"chatId": f"{digits}@c.us"},
        {"jid": f"{digits}@s.whatsapp.net"},
    ]

    # Estruturas de payload mais comuns para botões
    structures: List[Dict[str, Any]] = [
        # A) buttonText/description/buttons(id,body)
        {
            "buttonText": text,
            "description": footer_text or "",
            "buttons": [
                {"id": "YES", "body": yes_label},
                {"id": "NO", "body": no_label},
            ],
        },
        # B) message/footer/buttons(buttonId,buttonText.displayText,type) (OpenWA/whatsapp-web.js style)
        {
            "message": text,
            "footer": footer_text or "",
            "buttons": [
                {"buttonId": "YES", "buttonText": {"displayText": yes_label}, "type": 1},
                {"buttonId": "NO", "buttonText": {"displayText": no_label}, "type": 1},
            ],
            "headerType": 1,
        },
        # C) text/footer/buttons(id,body)
        {
            "text": text,
            "footer": footer_text or "",
            "buttons": [
                {"id": "YES", "body": yes_label},
                {"id": "NO", "body": no_label},
            ],
        },
        # D) text/footer/options (algumas distros usam 'options' como lista de strings)
        {
            "text": text,
            "footer": footer_text or "",
            "options": [yes_label, no_label],
        },
        # E) title/footer/buttons(lista de strings)
        {
            "title": text,
            "footer": footer_text or "",
            "buttons": [yes_label, no_label],
        },
        # F) text + button1/button2 campos planos
        {
            "text": text,
            "button1": yes_label,
            "button2": no_label,
            "footer": footer_text or "",
        },
    ]

    async with httpx.AsyncClient(base_url=UAZAPI_BASE_URL, timeout=60.0) as client:
        for endpoint in _menu_endpoints():
            endpoint = _ensure_leading_slash(endpoint)

            # 1) JSON attempts
            for d in dest_variants:
                for st in structures:
                    payload = {**d, **st}
                    try:
                        resp = await client.post(endpoint, json=payload, headers=headers)
                        if resp.status_code < 400:
                            try:
                                return resp.json()
                            except Exception:
                                return {"status": "ok", "http_status": resp.status_code}
                        else:
                            print(f"[uazapi] MENU {endpoint} JSON{list(payload.keys())} {resp.status_code} body={resp.text[:200].replace(chr(10),' ')}")
                    except Exception as exc:
                        print(f"[uazapi] MENU exception {endpoint} JSON{list(payload.keys())}: {exc}")

            # 2) form-urlencoded attempts (serializa estruturas em JSON strings quando necessário)
            for d in dest_variants:
                for st in structures:
                    form = _flatten_for_form({**d, **st})
                    try:
                        resp = await client.post(endpoint, data=form, headers=headers)
                        if resp.status_code < 400:
                            try:
                                return resp.json()
                            except Exception:
                                return {"status": "ok", "http_status": resp.status_code}
                        else:
                            print(f"[uazapi] MENU {endpoint} FORM{list(form.keys())} {resp.status_code} body={resp.text[:200].replace(chr(10),' ')}")
                    except Exception as exc:
                        print(f"[uazapi] MENU exception {endpoint} FORM{list(form.keys())}: {exc}")

            # 3) params + body attempts (destino em query, corpo simples)
            for d in dest_variants:
                body_variants = [
                    {"text": text, "footer": footer_text or "", "buttons": json.dumps([yes_label, no_label], ensure_ascii=False)},
                    {"message": text, "footer": footer_text or "", "buttons": json.dumps([yes_label, no_label], ensure_ascii=False)},
                    {"buttonText": text, "description": footer_text or "", "buttons": json.dumps([
                        {"id": "YES", "body": yes_label},
                        {"id": "NO", "body": no_label}
                    ], ensure_ascii=False)},
                ]
                for b in body_variants:
                    try:
                        resp = await client.post(endpoint, params=d, data=b, headers=headers)
                        if resp.status_code < 400:
                            try:
                                return resp.json()
                            except Exception:
                                return {"status": "ok", "http_status": resp.status_code}
                        else:
                            print(f"[uazapi] MENU {endpoint} PARAMS{list(d.keys())}+FORM{list(b.keys())} {resp.status_code} body={resp.text[:200].replace(chr(10),' ')}")
                    except Exception as exc:
                        print(f"[uazapi] MENU exception {endpoint} PARAMS{list(d.keys())}: {exc}")

    raise RuntimeError(f"UAZAPI menu send failed for phone={phone}")


# -------------------- Alias retrocompat --------------------
def _is_video_url(u: str) -> bool:
    path = _path_without_query((u or "").strip()).lower()
    return path.endswith((".mp4", ".m4v", ".mov", ".webm"))


def _is_image_url(u: str) -> bool:
    path = _path_without_query((u or "").strip()).lower()
    return path.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp"))


def _is_audio_url(u: str) -> bool:
    path = _path_without_query((u or "").strip()).lower()
    return path.endswith((".mp3", ".m4a", ".wav", ".ogg"))


async def send_message(
    *,
    phone: str,
    text: str,
    media_url: Optional[str] = None,
    mime_type: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Compat: se media_url for informado, tenta inferir o tipo correto (video/image/audio/document)
    e envia pela rota de mídia. Caso contrário, envia texto.
    """
    if media_url:
        media_type = "document"
        if _is_video_url(media_url):
            media_type = "video"
        elif _is_image_url(media_url):
            media_type = "image"
        elif _is_audio_url(media_url):
            media_type = "audio"

        return await send_whatsapp_message(
            phone=phone,
            content=text,
            type_=media_type,
            media_url=media_url,
            mime_type=mime_type,
            caption=text,
        )
    return await send_whatsapp_message(phone=phone, content=text, type_="text")


def _only_digits(s: str) -> str:
    return "".join(ch for ch in str(s) if ch.isdigit())


def normalize_number(s: str) -> str:
    """Retrocompat."""
    return _only_digits(s)


# -------------------- Baserow helpers --------------------
async def upload_file_to_baserow(source: str) -> Optional[Dict[str, Any]]:
    """
    Faz upload de um arquivo para o Baserow (quando 'source' é uma URL http/https ou data:),
    ou resolve metadados/URL quando 'source' é um ID numérico de arquivo.

    Requer:
    - BASEROW_BASE_URL
    - BASEROW_API_TOKEN  (Authorization: Token <token>)

    Retorna dict (JSON) do Baserow (contendo ao menos 'url' e/ou 'name') ou None em falha.
    """
    if not BASEROW_BASE_URL or not BASEROW_API_TOKEN:
        print("[baserow] não configurado: BASEROW_BASE_URL/BASEROW_API_TOKEN ausentes")
        return None

    headers = {"Authorization": f"Token {BASEROW_API_TOKEN}"}

    try:
        async with httpx.AsyncClient(base_url=BASEROW_BASE_URL, timeout=60.0) as client:
            # Caso seja um ID numérico -> tenta resolver metadados/URL por endpoints comuns
            if str(source).isdigit():
                candidates = [
                    f"/api/database/files/{source}/",    # algumas instalações expõem este endpoint
                    f"/api/user-files/{source}/",        # variação
                    f"/api/user-files/file/{source}/",   # variação
                ]
                for path in candidates:
                    try:
                        resp = await client.get(path, headers=headers)
                        if resp.status_code < 400:
                            try:
                                return resp.json()
                            except Exception:
                                # pode ser um redirect/arquivo binário; nesse caso, fornece URL direta
                                return {"url": f"{BASEROW_BASE_URL}{path}"}
                        else:
                            print(f"[baserow] GET {path} -> {resp.status_code} {resp.text[:200].replace(chr(10),' ')}")
                    except Exception as exc:
                        print(f"[baserow] exception GET {path}: {exc}")
                return None

            # Caso contrário, trata 'source' como URL -> baixa e faz upload para user-files
            # Baixa bytes da URL (ou data:) reaproveitando o utilitário local
            file_bytes, filename = await _download_bytes(source)
            if not file_bytes:
                print("[baserow] falha ao baixar fonte para upload")
                return None

            files = {"file": (filename or "file", file_bytes)}
            upload_endpoints = [
                "/api/user-files/upload-file/",   # endpoint canônico (cloud/self-host)
                "/api/userfiles/upload_file/",    # variação legacy
            ]
            for upath in upload_endpoints:
                try:
                    up = await client.post(upath, files=files, headers=headers)
                    if up.status_code < 400:
                        try:
                            return up.json()
                        except Exception:
                            # Em cenários raros, retorna vazio com 200
                            return {"status": "ok", "http_status": up.status_code}
                    else:
                        print(f"[baserow] POST {upath} -> {up.status_code} {up.text[:240].replace(chr(10),' ')}")
                except Exception as exc:
                    print(f"[baserow] exception POST {upath}: {exc}")
            return None

    except Exception as exc:
        print(f"[baserow] erro inesperado: {exc}")
        return None
        
