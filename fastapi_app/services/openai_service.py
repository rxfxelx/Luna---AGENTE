# fastapi_app/services/openai_service.py
"""
Integração com OpenAI Assistants API v2 — execução completa de tools.

Fluxo por mensagem:
  1) POST /threads/{id}/messages (role=user)
  2) POST /threads/{id}/runs (assistant_id)
  3) Poll até status final:
     - requires_action -> executar tools (Uazapi) -> submit_tool_outputs -> continuar polling
     - completed       -> listar mensagens (order=desc) e extrair resposta do assistant
     - failed/expired  -> fallback opcional a Chat Completions (último recurso)

As INSTRUÇÕES ficam somente no objeto Assistant (ASSISTANT_ID).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

# Dependências locais
from .uazapi_service import (
    normalize_number,
    send_whatsapp_message,
    send_menu_interesse,
)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ASSISTANT_ID = os.getenv("ASSISTANT_ID", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "") or "gpt-4o-mini"

# Conteúdos (menu/vídeo) vindos do ambiente
LUNA_MENU_YES = (os.getenv("LUNA_MENU_YES", "Sim, pode continuar") or "").strip()
LUNA_MENU_NO = (os.getenv("LUNA_MENU_NO", "Não, encerrar contato") or "").strip()
LUNA_MENU_TEXT = (os.getenv("LUNA_MENU_TEXT", "") or "").strip()
LUNA_MENU_FOOTER = (os.getenv("LUNA_MENU_FOOTER", "") or "").strip()

LUNA_VIDEO_URL = (os.getenv("LUNA_VIDEO_URL", "") or "").strip()
LUNA_VIDEO_CAPTION = (os.getenv("LUNA_VIDEO_CAPTION", "") or "").strip()
LUNA_VIDEO_AFTER_TEXT = (os.getenv("LUNA_VIDEO_AFTER_TEXT", "") or "").strip()
LUNA_END_TEXT = (os.getenv("LUNA_END_TEXT", "") or "").strip()

FALLBACK_SYSTEM_PTBR = (
    "Você é a Luna, uma assistente direta e profissional. "
    "Responda em português do Brasil de forma objetiva."
)

RUN_POLL_MAX = int(os.getenv("OPENAI_RUN_POLL_MAX", "120"))
RUN_POLL_INTERVAL = float(os.getenv("OPENAI_RUN_POLL_INTERVAL", "1.0"))

_BASE_URL = "https://api.openai.com/v1"


# -------------------- HTTP helpers --------------------
def _headers_assistants() -> Dict[str, str]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY não configurado.")
    if not ASSISTANT_ID:
        raise RuntimeError("ASSISTANT_ID não configurado.")
    return {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
        "OpenAI-Beta": "assistants=v2",
    }


def _headers_chat() -> Dict[str, str]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY não configurado.")
    return {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }


# -------------------- Threads --------------------
async def get_or_create_thread(session: AsyncSession, user) -> str:
    """Cria uma thread para o usuário ou retorna a existente."""
    th = getattr(user, "thread_id", None)
    if th:
        return th
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{_BASE_URL}/threads", headers=_headers_assistants(), json={})
        r.raise_for_status()
        thread_id = r.json()["id"]
    setattr(user, "thread_id", thread_id)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return thread_id


# -------------------- Fallback (último recurso) --------------------
async def _chat_fallback(user_message: str) -> Optional[str]:
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{_BASE_URL}/chat/completions",
                headers=_headers_chat(),
                json={
                    "model": OPENAI_MODEL,
                    "messages": [
                        {"role": "system", "content": FALLBACK_SYSTEM_PTBR},
                        {"role": "user", "content": user_message},
                    ],
                    "temperature": 0.6,
                },
            )
            r.raise_for_status()
            data = r.json()
            msg = (data.get("choices") or [{}])[0].get("message", {}).get("content")
            if isinstance(msg, str) and msg.strip():
                return msg.strip()
    except Exception as exc:
        print(f"[openai] chat fallback erro: {exc}")
    return None


# -------------------- Poll / status --------------------
def _extract_run_id_from_error(text: str) -> Optional[str]:
    m = re.search(r"(run_[A-Za-z0-9]+)", text or "")
    return m.group(1) if m else None


async def _poll_run(client: httpx.AsyncClient, thread_id: str, run_id: str) -> Dict[str, Any]:
    for _ in range(RUN_POLL_MAX):
        st = await client.get(f"{_BASE_URL}/threads/{thread_id}/runs/{run_id}", headers=_headers_assistants())
        st.raise_for_status()
        data = st.json()
        status = data.get("status")
        if status in {"completed", "failed", "expired", "cancelled", "requires_action"}:
            return data
        await asyncio.sleep(RUN_POLL_INTERVAL)
    return {"status": "timeout"}


# -------------------- Tools mapping --------------------
async def _tool_enviar_caixinha_interesse(number: str, args: Dict[str, Any]) -> str:
    number = normalize_number(number or "")
    if not LUNA_MENU_TEXT:
        return json.dumps({"ok": False, "reason": "no_menu_text"})
    await send_menu_interesse(
        phone=number,
        text=LUNA_MENU_TEXT,
        yes_label=LUNA_MENU_YES or "Sim",
        no_label=LUNA_MENU_NO or "Não",
        footer_text=LUNA_MENU_FOOTER or None,
    )
    return json.dumps({"ok": True, "sent": "menu"})


async def _tool_enviar_video(number: str, args: Dict[str, Any]) -> str:
    number = normalize_number(number or "")
    url = args.get("url") or LUNA_VIDEO_URL
    caption = args.get("caption") or LUNA_VIDEO_CAPTION
    if not url:
        return json.dumps({"ok": False, "reason": "no_video_url"})
    await send_whatsapp_message(
        phone=number,
        content=caption or "",
        type_="media",
        media_url=url,
        caption=caption or "",
    )
    if LUNA_VIDEO_AFTER_TEXT:
        await send_whatsapp_message(phone=number, content=LUNA_VIDEO_AFTER_TEXT, type_="text")
    return json.dumps({"ok": True, "sent": "video"})


async def _tool_enviar_msg(number: str, args: Dict[str, Any]) -> str:
    number = normalize_number(number or "")
    lead_nome = (args or {}).get("lead_nome") or ""
    # lead_area pode ser usado em futuras integrações (CRM etc.)
    _lead_area = (args or {}).get("lead_area") or ""
    text = f"Perfeito, {lead_nome}. Vou te colocar em contato com um consultor criativo da Verbo Vídeo."
    await send_whatsapp_message(phone=number, content=text.strip(), type_="text")
    if LUNA_END_TEXT:
        await send_whatsapp_message(phone=number, content=LUNA_END_TEXT, type_="text")
    return json.dumps({"ok": True, "lead_nome": lead_nome})


async def _tool_numero_novo(number: str, args: Dict[str, Any]) -> str:
    # Aqui você pode registrar no DB/CRM; por enquanto apenas confirma.
    return json.dumps({"ok": True, "novo_contato": args})


async def _tool_excluir_dados_lead(number: str, args: Dict[str, Any]) -> str:
    number = normalize_number(number or "")
    await send_whatsapp_message(phone=number, content="Ok, seus dados foram excluídos (LGPD).", type_="text")
    return json.dumps({"ok": True, "deleted": True})


_TOOL_MAP = {
    "enviar_caixinha_interesse": _tool_enviar_caixinha_interesse,
    "enviar_video": _tool_enviar_video,
    "enviar_msg": _tool_enviar_msg,
    "numero_novo": _tool_numero_novo,
    "excluir_dados_lead": _tool_excluir_dados_lead,
}


async def _handle_requires_action(
    client: httpx.AsyncClient,
    thread_id: str,
    run_id: str,
    requires_action: Dict[str, Any],
    number: Optional[str],
    lead_name: Optional[str],
) -> None:
    submit = (requires_action or {}).get("submit_tool_outputs") or {}
    tool_calls: List[Dict[str, Any]] = submit.get("tool_calls") or []
    outputs: List[Dict[str, str]] = []
    for tc in tool_calls:
        t_id = tc.get("id")
        fn = (tc.get("function") or {})
        name = fn.get("name")
        raw_args = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        except Exception:
            args = {}

        # injeta nome/número quando fizer sentido
        if name == "enviar_msg":
            args.setdefault("lead_nome", (lead_name or "").strip())

        handler = _TOOL_MAP.get(name)
        if not handler:
            outputs.append({"tool_call_id": t_id, "output": json.dumps({"ok": False, "unknown_tool": name})})
            continue

        try:
            out = await handler(number or "", args)
        except Exception as exc:
            out = json.dumps({"ok": False, "error": str(exc)})

        outputs.append({"tool_call_id": t_id, "output": out})

    r = await client.post(
        f"{_BASE_URL}/threads/{thread_id}/runs/{run_id}/submit_tool_outputs",
        headers=_headers_assistants(),
        json={"tool_outputs": outputs},
    )
    r.raise_for_status()


# -------------------- API principal --------------------
async def ask_assistant(
    thread_id: str,
    user_message: str,
    *,
    number: Optional[str] = None,
    lead_name: Optional[str] = None,
) -> Optional[str]:
    """
    Envia a mensagem do usuário ao Assistant, executa tools quando requerido
    e retorna o texto final do assistant (quando houver).
    """
    async with httpx.AsyncClient(timeout=60.0) as client:
        # 1) Posta a mensagem do usuário (resiliente a run ativa)
        for _ in range(2):
            mr = await client.post(
                f"{_BASE_URL}/threads/{thread_id}/messages",
                headers=_headers_assistants(),
                json={"role": "user", "content": [{"type": "text", "text": user_message}]},
            )
            if mr.status_code == 400 and "active run" in mr.text.lower():
                active_id = _extract_run_id_from_error(mr.text)
                if not active_id:
                    break
                run_data = await _poll_run(client, thread_id, active_id)
                if run_data.get("status") in {"completed", "failed", "expired", "cancelled"}:
                    continue
                await asyncio.sleep(RUN_POLL_INTERVAL)
                continue
            mr.raise_for_status()
            break

        # 2) Cria a run
        run_resp = await client.post(
            f"{_BASE_URL}/threads/{thread_id}/runs",
            headers=_headers_assistants(),
            json={"assistant_id": ASSISTANT_ID},
        )
        run_resp.raise_for_status()
        run_id = run_resp.json().get("id")
        if not run_id:
            return await _chat_fallback(user_message)

        # 3) Poll com suporte a requires_action
        loops = 0
        while True:
            loops += 1
            if loops > RUN_POLL_MAX:
                return await _chat_fallback(user_message)

            data = await _poll_run(client, thread_id, run_id)
            status = data.get("status")

            if status == "completed":
                break

            if status == "requires_action":
                try:
                    await _handle_requires_action(
                        client,
                        thread_id,
                        run_id,
                        data.get("required_action") or {},
                        number=number,
                        lead_name=lead_name,
                    )
                except Exception as exc:
                    print(f"[openai] erro em requires_action/tools: {exc}")
                    return await _chat_fallback(user_message)
                await asyncio.sleep(RUN_POLL_INTERVAL)
                continue

            if status in {"failed", "expired", "cancelled", "timeout"}:
                return await _chat_fallback(user_message)

            await asyncio.sleep(RUN_POLL_INTERVAL)

        # 4) Busca as mensagens (mais recentes primeiro)
        msgs = await client.get(
            f"{_BASE_URL}/threads/{thread_id}/messages",
            headers=_headers_assistants(),
            params={"limit": 20, "order": "desc"},
        )
        msgs.raise_for_status()

        for m in msgs.json().get("data", []):
            if m.get("role") == "assistant":
                for c in m.get("content", []):
                    if c.get("type") == "text":
                        v = (c.get("text") or {}).get("value")
                        if v:
                            return v
                if isinstance(m.get("content"), str):
                    return m["content"]

        return await _chat_fallback(user_message)
