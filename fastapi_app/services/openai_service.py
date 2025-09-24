# fastapi_app/services/openai_service.py
"""
Integração com OpenAI Assistants API v2.

Fluxo completo por mensagem:
  1) POST /threads/{id}/messages   (role=user)
  2) POST /threads/{id}/runs       (assistant_id)
  3) Poll até status final:
     - requires_action  -> executar tools (UAZAPI / ações internas)
                           -> POST /submit_tool_outputs
                           -> continuar polling
     - completed        -> listar mensagens (order=desc, limit=20) e extrair texto do assistant
     - failed/expired   -> fallback opcional a Completions (mantido como segurança)

Observação importante:
As INSTRUÇÕES ficam definidas dentro do Assistant no painel da OpenAI.
Não utilizamos mais ASSISTANT_RUN_INSTRUCTIONS via ambiente.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Optional, Dict, Any, List

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.db_models import User
from .uazapi_service import (
    normalize_number,
    send_whatsapp_text,
    send_whatsapp_menu,
    send_whatsapp_media,
)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ASSISTANT_ID = os.getenv("ASSISTANT_ID", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "") or "gpt-4o-mini"

# Mantemos um fallback para casos extremos (falha real da Assistants API).
FALLBACK_SYSTEM_PTBR = (
    "Você é a Luna, uma assistente direta e profissional. "
    "Responda em português do Brasil de forma objetiva."
)

RUN_POLL_MAX = int(os.getenv("OPENAI_RUN_POLL_MAX", "90"))
RUN_POLL_INTERVAL = float(os.getenv("OPENAI_RUN_POLL_INTERVAL", "1.0"))

_BASE_URL = "https://api.openai.com/v1"


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


async def get_or_create_thread(session: AsyncSession, user: User) -> str:
    """Cria um novo thread para o usuário ou retorna o existente."""
    if user.thread_id:
        return user.thread_id
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{_BASE_URL}/threads", headers=_headers_assistants(), json={})
        r.raise_for_status()
        user.thread_id = r.json()["id"]
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user.thread_id


async def _chat_fallback(user_message: str) -> Optional[str]:
    """Fallback para Chat Completions caso a Assistants API falhe."""
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


def _extract_run_id_from_error(text: str) -> Optional[str]:
    m = re.search(r"(run_[A-Za-z0-9]+)", text or "")
    return m.group(1) if m else None


async def _poll_run(client: httpx.AsyncClient, thread_id: str, run_id: str) -> Dict[str, Any]:
    """Retorna o JSON da run (status atual) a cada poll."""
    for _ in range(RUN_POLL_MAX):
        st = await client.get(
            f"{_BASE_URL}/threads/{thread_id}/runs/{run_id}",
            headers=_headers_assistants(),
        )
        st.raise_for_status()
        data = st.json()
        status = data.get("status")
        if status in {"completed", "failed", "expired", "cancelled", "requires_action"}:
            return data
        await asyncio.sleep(RUN_POLL_INTERVAL)
    return {"status": "timeout"}


# ---------- Mapeamento das TOOLS do Assistant ----------

async def _tool_enviar_caixinha_interesse(number: str, args: Dict[str, Any]) -> str:
    """Envia menu de interesse via UAZAPI."""
    number = normalize_number(number)
    menu_text = os.getenv("LUNA_MENU_TEXT", "").strip()
    yes = os.getenv("LUNA_MENU_YES", "Sim, pode continuar").strip()
    no = os.getenv("LUNA_MENU_NO", "Não, encerrar contato").strip()
    footer = os.getenv("LUNA_MENU_FOOTER", "Escolha uma das opções abaixo").strip()
    await send_whatsapp_menu(number, menu_text, [yes, no], footer)
    return json.dumps({"ok": True, "sent": "menu"})


async def _tool_enviar_video(number: str, args: Dict[str, Any]) -> str:
    """Envia vídeo demonstrativo + texto de seguimento."""
    number = normalize_number(number)
    url = os.getenv("LUNA_VIDEO_URL", "").strip()
    caption = os.getenv("LUNA_VIDEO_CAPTION", "").strip() or None
    after = os.getenv("LUNA_VIDEO_AFTER_TEXT", "").strip() or None
    if not url:
        # Sem URL definida, não falha a run; apenas devolve info
        return json.dumps({"ok": False, "reason": "no_video_url"})
    await send_whatsapp_media(number, media_type="video", file=url, caption=caption)
    if after:
        await send_whatsapp_text(number, after)
    return json.dumps({"ok": True, "sent": "video"})


async def _tool_enviar_msg(number: str, args: Dict[str, Any]) -> str:
    """Handoff: envia mensagem final para o lead (confirmação de repasse)."""
    number = normalize_number(number)
    lead_nome = (args or {}).get("lead_nome") or ""
    lead_area = (args or {}).get("lead_area") or ""
    # Mensagem final simples ao lead (o repasse interno/CRM pode ser adicionado aqui)
    text = f"Perfeito, {lead_nome}. Vou te colocar em contato com um consultor criativo da Verbo Vídeo."
    await send_whatsapp_text(number, text.strip())
    end_text = os.getenv("LUNA_END_TEXT", "").strip()
    if end_text:
        await send_whatsapp_text(number, end_text)
    return json.dumps({"ok": True, "lead_nome": lead_nome, "lead_area": lead_area})


async def _tool_numero_novo(number: str, args: Dict[str, Any]) -> str:
    """Registra internamente o novo contato (stub); aqui retornamos ack."""
    novo = (args or {}).get("contato") or (args or {}).get("numero") or ""
    # Aqui você pode persistir no seu DB/CRM; mantemos ack para o Assistant concluir a run.
    return json.dumps({"ok": True, "novo_contato": novo})


async def _tool_excluir_dados_lead(number: str, args: Dict[str, Any]) -> str:
    """LGPD: stub para exclusão de dados do lead (implemente se necessário)."""
    # TODO: apagar do seu DB. Por ora, apenas confirma.
    await send_whatsapp_text(normalize_number(number), "Ok, seus dados foram excluídos. (LGPD)")
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
) -> None:
    """Executa todas as tool_calls e faz submit_tool_outputs."""
    submit = ((requires_action or {}).get("submit_tool_outputs") or {})
    tool_calls: List[Dict[str, Any]] = submit.get("tool_calls") or []
    outputs = []
    for tc in tool_calls:
        t_id = tc.get("id")
        f = ((tc.get("function") or {}))
        name = f.get("name")
        raw_args = f.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        except Exception:
            args = {}

        handler = _TOOL_MAP.get(name)
        if not handler:
            # Não falha a run: retorna "no-op" para a tool desconhecida
            outputs.append({"tool_call_id": t_id, "output": json.dumps({"ok": False, "unknown_tool": name})})
            continue

        try:
            out = await handler(number or "", args)
        except Exception as exc:
            out = json.dumps({"ok": False, "error": str(exc)})

        outputs.append({"tool_call_id": t_id, "output": out})

    # Submete todos os outputs de uma vez
    r = await client.post(
        f"{_BASE_URL}/threads/{thread_id}/runs/{run_id}/submit_tool_outputs",
        headers=_headers_assistants(),
        json={"tool_outputs": outputs},
    )
    r.raise_for_status()


async def ask_assistant(
    thread_id: str,
    user_message: str,
    *,
    number: Optional[str] = None,
    lead_name: Optional[str] = None,
) -> Optional[str]:
    """
    Envia a mensagem do usuário ao Assistant e retorna a resposta em texto.
    Se a run exigir tools, elas são executadas (menu, vídeo, handoff etc.).
    """
    async with httpx.AsyncClient(timeout=60.0) as client:
        # 1) Posta a mensagem do usuário (evita colisão com run ativa)
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
                if run_data.get("status") != "completed":
                    # Não vamos cair no fallback aqui; apenas tentamos de novo.
                    continue
                # Após concluir, tentamos postar de novo
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

        # 3) Poll com suporte a requires_action (tools)
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
                    )
                except Exception as exc:
                    print(f"[openai] erro em requires_action/tools: {exc}")
                    return await _chat_fallback(user_message)
                # continua o loop para ver o próximo status
                await asyncio.sleep(RUN_POLL_INTERVAL)
                continue

            if status in {"failed", "expired", "cancelled", "timeout"}:
                # fallback (último recurso)
                return await _chat_fallback(user_message)

            await asyncio.sleep(RUN_POLL_INTERVAL)

        # 4) Busca as mensagens (mais recentes primeiro) e extrai texto do assistant
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
                # se vier conteúdo como string
                if isinstance(m.get("content"), str):
                    return m["content"]

        return await _chat_fallback(user_message)
