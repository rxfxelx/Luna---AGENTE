# fastapi_app/services/openai_service.py
"""
Integração com OpenAI Assistants v2, com polling de Run e execução de tools.

Expõe:
- get_or_create_thread(session, user) -> str
- ask_assistant(thread_id: str, user_text: str) -> str  (retorna o texto final do assistant)

Tools suportadas (usadas pelo Assistant):
- enviar_caixinha_interesse(phone?, text?, yes_label?, no_label?, footer_text?)
- enviar_video(phone?, url?, caption?, mime_type?)
- enviar_msg(phone, name?, last?)  -> notifica consultores (handoff)

Observações:
- Este módulo NÃO envia nada por conta própria ao usuário final, exceto quando uma tool do Assistant
  explicitamente pede (menu/vídeo/handoff). O texto final do Assistant é retornado para o `whatsapp.py`,
  que decide enviar (e já possui as proteções anti-eco/convite duplicado).
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.db_models import User
from .uazapi_service import (
    send_menu_interesse,
    send_whatsapp_message,
)

# ============================ ENV / Config ============================

OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
ASSISTANT_ID = (os.getenv("ASSISTANT_ID") or "").strip()

# Defaults (caso o Assistant não passe argumentos na tool)
LUNA_MENU_YES = (os.getenv("LUNA_MENU_YES") or "Sim, pode continuar").strip()
LUNA_MENU_NO = (os.getenv("LUNA_MENU_NO") or "Não, encerrar contato").strip()
LUNA_MENU_TEXT = (os.getenv("LUNA_MENU_TEXT") or "").strip()
LUNA_MENU_FOOTER = (os.getenv("LUNA_MENU_FOOTER") or "Escolha uma das opções abaixo").strip()

LUNA_VIDEO_URL = (os.getenv("LUNA_VIDEO_URL") or "").strip()
LUNA_VIDEO_CAPTION = (os.getenv("LUNA_VIDEO_CAPTION") or "").strip()

# Handoff (aviso para consultores)
def _env_template(key: str, default: str = "") -> str:
    raw = os.getenv(key, default) or ""
    raw = raw.strip()
    # Corrige casos do painel onde colaram "HANDOFF_NOTIFY_TEMPLATE=..."
    if raw.upper().startswith("HANDOFF_NOTIFY_TEMPLATE="):
        raw = raw.split("=", 1)[1]
    # Tira aspas de borda
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        raw = raw[1:-1]
    # Normaliza escapes \n, \r, \t
    return raw.replace("\\n", "\n").replace("\\r", "\r").replace("\\t", "\t")

HANDOFF_NOTIFY_NUMBERS = (os.getenv("HANDOFF_NOTIFY_NUMBERS") or "").strip()
HANDOFF_NOTIFY_TEMPLATE = _env_template(
    "HANDOFF_NOTIFY_TEMPLATE",
    "Novo lead aguardando contato (Luna — Verbo Vídeo)\n"
    "Nome: {name}\nTelefone: +{digits}\nÚltima mensagem: {last}\nOrigem: WhatsApp\n"
    "Link: {wa_link}"
)

# Quando True, NÃO faz fallback de vídeo após SIM na caixinha (100% a IA decide).
LUNA_STRICT_ASSISTANT = (os.getenv("LUNA_STRICT_ASSISTANT", "false") or "false").lower() == "true"

_HEADERS = {
    "Authorization": f"Bearer {OPENAI_API_KEY}",
    "OpenAI-Beta": "assistants=v2",
}

# ============================ Helpers OpenAI ============================

async def _openai_post(url: str, json_body: Dict[str, Any]) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, headers=_HEADERS, json=json_body)
        resp.raise_for_status()
        return resp.json()

async def _openai_get(url: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(url, headers=_HEADERS)
        resp.raise_for_status()
        return resp.json()

async def _create_thread() -> str:
    data = await _openai_post("https://api.openai.com/v1/threads", {})
    return data["id"]

async def _append_message(thread_id: str, role: str, text: str) -> None:
    await _openai_post(
        f"https://api.openai.com/v1/threads/{thread_id}/messages",
        {"role": role, "content": text},
    )

async def _create_run(thread_id: str, assistant_id: str) -> str:
    data = await _openai_post(
        f"https://api.openai.com/v1/threads/{thread_id}/runs",
        {"assistant_id": assistant_id},
    )
    return data["id"]

async def _retrieve_run(thread_id: str, run_id: str) -> Dict[str, Any]:
    return await _openai_get(
        f"https://api.openai.com/v1/threads/{thread_id}/runs/{run_id}"
    )

async def _submit_tool_outputs(thread_id: str, run_id: str, tool_outputs: List[Dict[str, Any]]) -> None:
    await _openai_post(
        f"https://api.openai.com/v1/threads/{thread_id}/runs/{run_id}/submit_tool_outputs",
        {"tool_outputs": tool_outputs},
    )

async def _list_messages(thread_id: str) -> List[Dict[str, Any]]:
    data = await _openai_get(
        f"https://api.openai.com/v1/threads/{thread_id}/messages"
    )
    return data.get("data", [])

# ============================ Thread por usuário ============================

async def get_or_create_thread(session: AsyncSession, user: User) -> str:
    """
    Retorna o thread_id do OpenAI para o usuário.
    Tenta usar qualquer atributo existente no modelo (openai_thread_id | thread_id | thread).
    Se não existir, cria e salva no primeiro atributo disponível.
    """
    for attr in ("openai_thread_id", "thread_id", "thread"):
        if hasattr(user, attr):
            tid = getattr(user, attr, None)
            if tid:
                return tid

    # cria se não existir
    tid = await _create_thread()

    # tenta persistir no primeiro atributo disponível
    saved = False
    for attr in ("openai_thread_id", "thread_id", "thread"):
        if hasattr(user, attr):
            setattr(user, attr, tid)
            saved = True
            break

    try:
        if saved:
            session.add(user)
            await session.commit()
    except Exception as exc:
        # Mesmo que não consiga salvar (campo ausente na tabela), ainda devolvemos o tid.
        print(f"[openai] não consegui persistir thread_id no modelo User: {exc!r}")

    return tid

# ============================ Tools (execução) ============================

def _only_digits(s: Any) -> str:
    return "".join(ch for ch in str(s) if ch.isdigit())

def _parse_args(arg_str_or_obj: Any) -> Dict[str, Any]:
    """Tool arguments podem vir como string JSON; converte com segurança."""
    if isinstance(arg_str_or_obj, dict):
        return arg_str_or_obj
    if not arg_str_or_obj:
        return {}
    try:
        return json.loads(arg_str_or_obj)
    except Exception:
        return {}

def _parse_notify_numbers(raw: str) -> List[str]:
    out: List[str] = []
    for token in raw.replace(";", ",").split(","):
        digits = _only_digits(token)
        if digits:
            out.append(digits)
    return out

async def _tool_enviar_caixinha_interesse(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Args esperados:
      phone (obrigatório), text?, yes_label?, no_label?, footer_text?
    Defaults vêm do ENV se ausentes.
    """
    phone = args.get("phone") or args.get("numero") or ""
    phone = _only_digits(phone)
    if not phone:
        return {"error": "phone ausente"}

    text = (args.get("text") or LUNA_MENU_TEXT or "").strip()
    yes_label = (args.get("yes_label") or LUNA_MENU_YES or "Sim").strip()
    no_label = (args.get("no_label") or LUNA_MENU_NO or "Não").strip()
    footer_text = (args.get("footer_text") or LUNA_MENU_FOOTER or None) or None

    try:
        await send_menu_interesse(
            phone=phone,
            text=text,
            yes_label=yes_label,
            no_label=no_label,
            footer_text=footer_text,
        )
        return {"status": "sent"}
    except Exception as exc:
        print(f"[tool] enviar_caixinha_interesse failed: {exc!r}")
        return {"error": str(exc)}

async def _tool_enviar_video(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Args esperados:
      phone (obrigatório), url?, caption?, mime_type?
    Se url/legend for omitida, usa os defaults LUNA_VIDEO_URL / LUNA_VIDEO_CAPTION.
    """
    phone = _only_digits(args.get("phone") or "")
    if not phone:
        return {"error": "phone ausente"}

    url = (args.get("url") or LUNA_VIDEO_URL or "").strip()
    if not url:
        return {"error": "url de vídeo ausente (e LUNA_VIDEO_URL vazio)"}

    caption = (args.get("caption") or LUNA_VIDEO_CAPTION or "").strip()
    mime_type = (args.get("mime_type") or None)

    try:
        await send_whatsapp_message(
            phone=phone,
            content=caption,
            type_="media",
            media_url=url,
            mime_type=mime_type,
            caption=caption,
        )
        return {"status": "sent"}
    except Exception as exc:
        print(f"[tool] enviar_video failed: {exc!r}")
        return {"error": str(exc)}

async def _tool_enviar_msg(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Notifica consultores (handoff). Espera:
      phone (do lead) e opcionalmente name, last.
    Usa HANDOFF_NOTIFY_NUMBERS/TEMPLATE do ENV.
    """
    lead_phone = _only_digits(args.get("phone") or "")
    name = (args.get("name") or "").strip() or "—"
    last = (args.get("last") or "").strip() or "—"
    wa_link = f"https://wa.me/{lead_phone}" if lead_phone else ""

    try:
        template = HANDOFF_NOTIFY_TEMPLATE
        try:
            body = template.format(name=name, digits=lead_phone, last=last, wa_link=wa_link)
        except Exception as exc:
            print(f"[handoff] template error {exc!r}; usando fallback")
            body = (
                "Novo lead aguardando contato (Luna — Verbo Vídeo)\n"
                f"Nome: {name}\nTelefone: +{lead_phone}\nÚltima mensagem: {last}\nOrigem: WhatsApp\n"
                f"Link: {wa_link}"
            )

        targets = _parse_notify_numbers(HANDOFF_NOTIFY_NUMBERS)
        if not targets:
            return {"error": "HANDOFF_NOTIFY_NUMBERS vazio"}

        for t in targets:
            try:
                await send_whatsapp_message(phone=t, content=body, type_="text")
            except Exception as exc:
                print(f"[handoff] falha ao notificar {t}: {exc!r}")

        return {"status": "sent", "targets": targets}
    except Exception as exc:
        print(f"[tool] enviar_msg failed: {exc!r}")
        return {"error": str(exc)}

async def _execute_tool_call(tool_call: Dict[str, Any]) -> Dict[str, Any]:
    """
    Executa UMA tool vinda do OpenAI (requires_action).
    Formato típico:
      {
        "id": "call_xxx",
        "type": "function",
        "function": {"name": "...", "arguments": "{\"phone\": \"5531...\"}"}
      }
    """
    tc_id = tool_call.get("id")
    fn = tool_call.get("function") or {}
    name = (fn.get("name") or "").strip().lower()
    args = _parse_args(fn.get("arguments"))

    if name in {"enviar_caixinha_interesse", "menu", "caixinha"}:
        result = await _tool_enviar_caixinha_interesse(args)
    elif name in {"enviar_video", "video", "vídeo"}:
        result = await _tool_enviar_video(args)
    elif name in {"enviar_msg", "handoff", "transfer"}:
        result = await _tool_enviar_msg(args)
    else:
        result = {"error": f"tool desconhecida: {name}"}

    # Tool outputs devem conter { "id": ..., "output": <string> } OU { "id": ..., "result": {...} }
    # A API aceita 'output' string. Serializamos o dict result.
    return {"id": tc_id, "output": json.dumps(result, ensure_ascii=False)}

# ============================ Perguntar à IA ============================

async def ask_assistant(thread_id: str, user_text: str) -> str:
    """
    1) Adiciona a mensagem do usuário ao thread.
    2) Cria um Run e faz polling até concluir.
    3) Se 'requires_action', executa tools e envia tool_outputs.
    4) Quando 'completed', retorna o último texto do assistant.
    5) Em caso de erro/timeout, retorna um fallback amigável.

    OBS: Este método NÃO envia nada para o usuário por conta própria (exceto
    quando a tool do Assistant explicitamente pede menu/vídeo/handoff).
    """
    if not OPENAI_API_KEY or not ASSISTANT_ID:
        return "Configuração de IA ausente. Por favor, tente novamente em instantes."

    # 1) adiciona mensagem do usuário
    try:
        await _append_message(thread_id, "user", user_text or "")
    except Exception as exc:
        print(f"[openai] append_message error: {exc!r}")
        return "Não consegui falar com a IA agora. Podemos tentar de novo?"

    # 2) cria run
    try:
        run_id = await _create_run(thread_id, ASSISTANT_ID)
    except Exception as exc:
        print(f"[openai] create_run error: {exc!r}")
        return "Tive um problema ao iniciar a IA. Vamos tentar novamente já já."

    # 3) polling
    MAX_WAIT_SECONDS = 120  # timeout de segurança
    waited = 0
    SLEEP = 4

    while True:
        try:
            run = await _retrieve_run(thread_id, run_id)
        except Exception as exc:
            print(f"[openai] retrieve_run error: {exc!r}")
            return "A IA ficou indisponível no momento. Posso tentar outra vez?"

        status = (run.get("status") or "").lower()
        if status in ("queued", "in_progress"):
            await asyncio.sleep(SLEEP)
            waited += SLEEP
            if waited >= MAX_WAIT_SECONDS:
                return "Demorou um pouco mais do que o normal. Posso continuar por aqui?"
            continue

        if status == "requires_action":
            ra = run.get("required_action", {})
            submit = ra.get("submit_tool_outputs", {})
            tool_calls = submit.get("tool_calls", []) or []
            outputs: List[Dict[str, Any]] = []

            for call in tool_calls:
                try:
                    out = await _execute_tool_call(call)
                    outputs.append(out)
                except Exception as exc:
                    print(f"[openai] tool execution error: {exc!r}")
                    outputs.append({"id": call.get("id"), "output": json.dumps({"error": str(exc)})})

            # envia tool_outputs e volta para o polling
            try:
                await _submit_tool_outputs(thread_id, run_id, outputs)
            except Exception as exc:
                print(f"[openai] submit_tool_outputs error: {exc!r}")
                return "Não consegui concluir uma ação solicitada pela IA. Podemos tentar de novo?"

            await asyncio.sleep(1)
            continue

        if status == "completed":
            # retorna o último texto do assistant
            try:
                msgs = await _list_messages(thread_id)
                for m in msgs:  # data vem em ordem decrescente (normalmente)
                    if (m.get("role") == "assistant") and m.get("content"):
                        # conteúdo pode ser múltiplo (text, image, etc.). Pegamos o primeiro text.
                        for c in m["content"]:
                            if c.get("type") == "text":
                                val = c["text"].get("value") if isinstance(c["text"], dict) else None
                                if val:
                                    return val.strip()
                return "Certo!"
            except Exception as exc:
                print(f"[openai] list_messages parse error: {exc!r}")
                return "Tudo certo por aqui!"

        if status in ("failed", "expired", "canceled", "cancelled"):
            return "A IA não conseguiu concluir agora. Podemos continuar por aqui?"

        # status inesperado
        await asyncio.sleep(SLEEP)
        waited += SLEEP
        if waited >= MAX_WAIT_SECONDS:
            return "Demorou um pouco mais do que o normal. Posso continuar por aqui?"
