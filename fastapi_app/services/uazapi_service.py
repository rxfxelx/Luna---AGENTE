# fastapi_app/services/uazapi_service.py
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

# Timeout configurável (segundos) sem alterar o padrão anterior
UAZAPI_TIMEOUT = float(os.getenv("UAZAPI_TIMEOUT", "60"))

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


def _headers() -> Dict[str, str]:
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


def _text_endpoints() -> List[str]:
    return _dedup([UAZAPI_SEND_TEXT_PATH, "/send/text"] + _TEXT_FALLBACKS)


def _media_endpoints() -> List[str]:
    return _dedup([UAZAPI_SEND_MEDIA_PATH, "/send/media"] + _MEDIA_FALLBACKS)


def _menu_endpoints() -> List[str]:
    return _dedup([UAZAPI_SEND_MENU_PATH] + _MENU_FALLBACKS)


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
    return "application/octet-stream"


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

        async with httpx.AsyncClient(timeout=UAZAPI_TIMEOUT) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            file_bytes = resp.content

            # Tenta extrair filename do Content-Disposition (quando disponível)
            filename: Optional[str] = None
            cd = resp.headers.get("content-disposition") or resp.headers.get("Content-Disposition")
            if cd and "filename=" in cd:
                try:
                    # filename="..." ou filename=...
                    fname = cd.split("filename=", 1)[1].strip().strip('"').strip("'")
                    # remove path, se vier
                    filename = fname.split("/")[-1].split("\\")[-1]
                except Exception:
                    filename = None

            if not filename:
                filename = url.split("/")[-1].split("?")[0] or "file"

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
    - Para vídeo: usar type_="video" ou fornecer media_url terminando em .mp4 (MIME de vídeo).
    Retorna dict (JSON) em sucesso; levanta RuntimeError em falha.
    """
    if not UAZAPI_BASE_URL:
        raise RuntimeError("UAZAPI_BASE_URL não configurada.")

    headers = _headers()
    digits = _only_digits(phone) or phone
    plus_digits = digits if str(digits).startswith("+") else f"+{digits}"

    async with httpx.AsyncClient(base_url=UAZAPI_BASE_URL, timeout=UAZAPI_TIMEOUT) as client:
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

        # ============ MÍDIA (inclui VÍDEO) ============
        mime = (mime_type or _infer_mime_from_url(media_url or "")) if media_url else (mime_type or "")
        base_caption = (caption or content or "").strip()

        # 1) Tenta JSON via /send/media para VÍDEO com media_url público (recomendado)
        if type_ == "video" or (mime and mime.startswith("video/")):
            base_payload = {"mediatype": "video", "media_url": media_url, "caption": base_caption}
            candidate_payloads: List[Dict[str, Any]] = [
                {**base_payload, "number": digits},
                {**base_payload, "number": plus_digits},
                {**base_payload, "phone": digits},
                {**base_payload, "phone": plus_digits},
                {**base_payload, "jid": f"{digits}@s.whatsapp.net"},
                {**base_payload, "chatId": f"{digits}@c.us"},
            ]
            for endpoint in _media_endpoints():
                endpoint = _ensure_leading_slash(endpoint)
                # pula variantes estritamente de upload de arquivo
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

        # 2) Fallback: baixa arquivo e envia multipart (cobre imagem, doc, e vídeo se necessário)
        file_bytes, filename = await _download_bytes(media_url or "")
        if not file_bytes:
            raise RuntimeError("Falha ao baixar o arquivo de mídia para upload multipart.")
        files = {"file": (filename or "file", file_bytes, mime or "application/octet-stream")}
        async with httpx.AsyncClient(base_url=UAZAPI_BASE_URL, timeout=UAZAPI_TIMEOUT) as client2:
            for endpoint in _media_endpoints():
                endpoint = _ensure_leading_slash(endpoint)

                # (A) 'number' na query (muitas instâncias exigem)
                query_variants = [
                    {"number": digits},
                    {"number": plus_digits},
                    {"phone": digits},
                    {"phone": plus_digits},
                    {"chatId": f"{digits}@c.us"},
                    {"jid": f"{digits}@s.whatsapp.net"},
                ]
                for q in query_variants:
                    try:
                        resp = await client2.post(endpoint, params=q, data={"caption": base_caption}, files=files, headers=headers)
                        if resp.status_code < 400:
                            try:
                                return resp.json()
                            except Exception:
                                return {"status": "ok", "http_status": resp.status_code}
                        else:
                            print(f"[uazapi] {endpoint} QUERY{list(q.keys())} FORM {resp.status_code} body={resp.text[:200].replace(chr(10),' ')}")
                    except Exception as exc:
                        print(f"[uazapi] exception on {endpoint} QUERY{list(q.keys())} multipart: {exc}")

                # (B) 'number' no body (outras variantes)
                form_variants = [
                    {"number": digits, "caption": base_caption},
                    {"number": plus_digits, "caption": base_caption},
                    {"phone": digits, "caption": base_caption},
                    {"phone": plus_digits, "caption": base_caption},
                    {"chatId": f"{digits}@c.us", "caption": base_caption},
                    {"jid": f"{digits}@s.whatsapp.net", "caption": base_caption},
                ]
                for form in form_variants:
                    try:
                        resp = await client2.post(endpoint, data=form, files=files, headers=headers)
                        if resp.status_code < 400:
                            try:
                                return resp.json()
                            except Exception:
                                return {"status": "ok", "http_status": resp.status_code}
                        else:
                            print(f"[uazapi] {endpoint} FORM{list(form.keys())} {resp.status_code} body={resp.text[:200].replace(chr(10),' ')}")
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
    Envia um menu interativo de botões (Sim/Não) de acordo com o contrato oficial do
    endpoint ``/send/menu`` da UAZAPI. Conforme documentado, um payload válido para
    botões deve conter apenas os campos ``number``, ``type``, ``text``, ``choices`` e
    opcionalmente ``footerText``【284353139205670†screenshot】. O número deve estar no formato
    E.164 sem o sinal de ``+`` e cada opção em ``choices`` é uma string no formato
    ``"Título|ID"``【284353139205670†screenshot】.

    :param phone: número do destinatário (qualquer formato; apenas dígitos serão usados)
    :param text: mensagem principal exibida acima dos botões
    :param yes_label: texto do botão de confirmação
    :param no_label: texto do botão de recusa
    :param footer_text: texto opcional exibido abaixo dos botões
    :return: resposta JSON da UAZAPI se sucesso; gera RuntimeError em falha
    """
    if not UAZAPI_BASE_URL:
        raise RuntimeError("UAZAPI_BASE_URL não configurada.")
    digits = _only_digits(phone)
    if not digits:
        raise ValueError("Número de telefone inválido ou vazio.")
    # Constrói o payload conforme a documentação oficial da UAZAPI para mensagens interativas
    payload: Dict[str, Any] = {
        "number": digits,
        "type": "button",
        "text": text,
        "choices": [
            f"{yes_label}|YES",
            f"{no_label}|NO",
        ],
    }
    if footer_text:
        payload["footerText"] = footer_text
    endpoint = _ensure_leading_slash(UAZAPI_SEND_MENU_PATH or "/send/menu")
    async with httpx.AsyncClient(base_url=UAZAPI_BASE_URL, timeout=UAZAPI_TIMEOUT) as client:
        resp = await client.post(endpoint, json=payload, headers=_headers())
        body = resp.text
        if resp.status_code >= 400:
            # Log do erro para depuração; inclui parte do corpo retornado
            print(f"[uazapi] MENU {endpoint} status={resp.status_code} body={body[:300]}")
            raise RuntimeError(
                f"UAZAPI menu send failed for phone={phone} status={resp.status_code}"
            )
        # Retorna o JSON decodificado se possível, ou um dict genérico caso contrário
        try:
            return resp.json()
        except Exception:
            return {"status": "ok", "http_status": resp.status_code, "raw": body}


# -------------------- Alias retrocompat --------------------
async def send_message(
    *,
    phone: str,
    text: str,
    media_url: Optional[str] = None,
    mime_type: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Alias retrocompatível:
    - Se media_url for fornecida e for vídeo (por MIME ou extensão), envia como 'video'.
    - Caso contrário, envia como 'media' (fallback multipart cobre imagem/documento/etc).
    - Sem media_url -> envia como texto.
    """
    if media_url:
        inferred = mime_type or _infer_mime_from_url(media_url)
        is_video = inferred.lower().startswith("video/")
        return await send_whatsapp_message(
            phone=phone,
            content=text,
            type_="video" if is_video else "media",
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
        async with httpx.AsyncClient(base_url=BASEROW_BASE_URL, timeout=UAZAPI_TIMEOUT) as client:
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
