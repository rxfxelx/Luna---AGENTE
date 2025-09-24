# fastapi_app/services/openai_service.py
"""
Integração com OpenAI Assistants API v2.

Expõe:
- get_or_create_thread(session: AsyncSession, user: User) -> str
- ask_assistant(thread_id: str, user_message: str) -> Optional[str]

Melhorias:
- Evita 400 "active run" aguardando o run atual terminar antes de postar nova mensagem/criar outro run.
- Fallback robusto para Chat Completions quando Assistants não puder concluir.

Nota: As instruções do assistente são definidas diretamente no objeto Assistant
no painel da OpenAI. Não utilizamos mais ASSISTANT_RUN_INSTRUCTIONS via
variável de ambiente.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.db_models import User

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ASSISTANT_ID = os.getenv("ASSISTANT_ID", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "") or "gpt-4o-mini"

# Não carregamos mais instruções do assistente via env. Caso precise
# sobrepor temporariamente as instruções, defina esta variável manualmente em código.
ASSISTANT_RUN_INSTRUCTIONS: Optional[str] = None

RUN_POLL_MAX = int(os.getenv("OPENAI_RUN_POLL_MAX", "60"))          # tentativas de polling
RUN_POLL_INTERVAL = float(os.getenv("OPENAI_RUN_POLL_INTERVAL", "1.0"))  # segundos entre polls

_BASE_URL = "https://api.openai.com/v1"


def _headers_assistants() -> dict:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY não configurado.")
    if not ASSISTANT_ID:
        raise RuntimeError("ASSISTANT_ID não configurado.")
    return {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
        "OpenAI-Beta": "assistants=v2",
    }


def _headers_chat() -> dict:
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
        resp = await client.post(f"{_BASE_URL}/threads", headers=_headers_assistants(), json={})
        resp.raise_for_status()
        thread_id = resp.json()["id"]

    user.thread_id = thread_id
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return thread_id


async def _chat_fallback(user_message: str) -> Optional[str]:
    """Fallback para Chat Completions caso a API de Assistants falhe."""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{_BASE_URL}/chat/completions",
                headers=_headers_chat(),
                json={
                    "model": OPENAI_MODEL,
                    "messages": [
                        {
                            "role": "system",
                            "content": "Você é a Luna, uma assistente útil e direta. Responda em português do Brasil.",
                        },
                        {"role": "user", "content": user_message},
                    ],
                    "temperature": 0.7,
                },
            )
            r.raise_for_status()
            data = r.json()
            choice = (data.get("choices") or [{}])[0]
            msg = (choice.get("message") or {}).get("content")
            return msg.strip() if isinstance(msg, str) and msg.strip() else None
    except Exception as exc:
        print(f"[openai] chat fallback erro: {exc}")
        return None


async def _poll_run(client: httpx.AsyncClient, thread_id: str, run_id: str) -> str:
    """Aguarda a conclusão do run retornando seu status final."""
    for _ in range(RUN_POLL_MAX):
        st = await client.get(
            f"{_BASE_URL}/threads/{thread_id}/runs/{run_id}", headers=_headers_assistants()
        )
        st.raise_for_status()
        status = st.json().get("status")
        if status == "completed":
            return "completed"
        if status in {"failed", "expired", "cancelled"}:
            return status
        if status == "requires_action":
            # Se houver tools para rodar, retornamos falha para forçar o fallback
            return "failed"
        await asyncio.sleep(RUN_POLL_INTERVAL)
    return "timeout"


def _extract_run_id_from_error(text: str) -> Optional[str]:
    """Extrai run_id do corpo de erro (padrão run_ABC123)."""
    m = re.search(r"(run_[A-Za-z0-9]+)", text or "")
    return m.group(1) if m else None


async def ask_assistant(thread_id: str, user_message: str) -> Optional[str]:
    """Envia a mensagem do usuário ao Assistant e retorna a resposta em texto."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        # 1) Posta a mensagem do usuário; se houver run ativo, espera finalizar
        for attempt in range(2):
            mr = await client.post(
                f"{_BASE_URL}/threads/{thread_id}/messages",
                headers=_headers_assistants(),
                json={"role": "user", "content": [{"type": "text", "text": user_message}]},
            )
            if mr.status_code == 400 and "active run" in mr.text.lower():
                active_id = _extract_run_id_from_error(mr.text)
                if not active_id:
                    break
                status = await _poll_run(client, thread_id, active_id)
                if status != "completed":
                    print(f"[openai] active run terminou como {status}; usando fallback.")
                    return await _chat_fallback(user_message)
                continue  # tenta postar de novo
            try:
                mr.raise_for_status()
            except Exception as exc:
                print(f"[openai] postar mensagem erro: {exc} body={mr.text}")
            break

        # 2) Cria o run sem passar instruções adicionais; elas estão no assistente
        payload = {"assistant_id": ASSISTANT_ID}

        for attempt in range(2):
            run_resp = await client.post(
                f"{_BASE_URL}/threads/{thread_id}/runs",
                headers=_headers_assistants(),
                json=payload,
            )
            if run_resp.status_code == 400 and "active run" in run_resp.text.lower():
                active_id = _extract_run_id_from_error(run_resp.text)
                if not active_id:
                    break
                status = await _poll_run(client, thread_id, active_id)
                if status != "completed":
                    print(f"[openai] active run ao criar novo terminou como {status}; usando fallback.")
                    return await _chat_fallback(user_message)
                continue
            try:
                run_resp.raise_for_status()
            except Exception as exc:
                print(f"[openai] erro ao criar run: {exc} body={run_resp.text}")
                return await _chat_fallback(user_message)
            break

        run_id = run_resp.json().get("id")
        if not run_id:
            print("[openai] sem run_id; fallback.")
            return await _chat_fallback(user_message)

        # 3) Polling até concluir
        status = await _poll_run(client, thread_id, run_id)
        if status != "completed":
            print(f"[openai] run terminou como {status}; fallback.")
            return await _chat_fallback(user_message)

        # 4) Busca mensagens da thread e extrai a última resposta do assistente
        try:
            msgs = await client.get(
                f"{_BASE_URL}/threads/{thread_id}/messages",
                headers=_headers_assistants(),
                params={"limit": 20, "order": "desc"},
            )
            msgs.raise_for_status()
            data = msgs.json().get("data", [])
        except Exception as exc:
            print(f"[openai] erro ao buscar mensagens: {exc}")
            return await _chat_fallback(user_message)

        for m in data:
            if m.get("role") == "assistant":
                contents = m.get("content", [])
                if isinstance(contents, list):
                    for c in contents:
                        if c.get("type") == "text":
                            txt = (c.get("text") or {}).get("value")
                            if txt:
                                return txt
                elif isinstance(contents, str):
                    return contents
        # se não achou texto válido, usa o fallback
        return await _chat_fallback(user_message)
