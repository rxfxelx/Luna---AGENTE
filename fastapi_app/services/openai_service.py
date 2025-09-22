"""
Integração com OpenAI Assistants API v2.

Expõe:
- get_or_create_thread(user: User, session: AsyncSession) -> str
- ask_assistant(thread_id: str, user_message: str) -> Optional[str]

Observações:
- Usa Assistants v2 (requer header: OpenAI-Beta: assistants=v2).
- Criação de thread correta: POST /v1/threads  (não é /assistants/{id}/threads).
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.db_models import User

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ASSISTANT_ID = os.getenv("ASSISTANT_ID", "")

_BASE_URL = "https://api.openai.com/v1"


def _headers() -> dict:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY não configurado.")
    if not ASSISTANT_ID:
        raise RuntimeError("ASSISTANT_ID não configurado.")
    return {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
        "OpenAI-Beta": "assistants=v2",
    }


async def get_or_create_thread(user: User, session: AsyncSession) -> str:
    """
    Retorna o thread_id do usuário; cria um novo se não existir e persiste no banco.
    """
    if user.thread_id:
        return user.thread_id

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(f"{_BASE_URL}/threads", headers=_headers(), json={})
        resp.raise_for_status()
        data = resp.json()
        thread_id = data["id"]

    user.thread_id = thread_id
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return thread_id


async def ask_assistant(thread_id: str, user_message: str) -> Optional[str]:
    """
    Publica a mensagem do usuário na thread, cria um run e aguarda a conclusão.
    Retorna o texto da resposta do assistente (se houver).
    """
    async with httpx.AsyncClient(timeout=60.0) as client:
        # 1) mensagem do usuário
        try:
            msg_resp = await client.post(
                f"{_BASE_URL}/threads/{thread_id}/messages",
                headers=_headers(),
                json={"role": "user", "content": user_message},
            )
            msg_resp.raise_for_status()
        except Exception as exc:
            print(f"[openai] erro ao postar mensagem do usuário: {exc}")
            return None

        # 2) cria o run
        try:
            run_resp = await client.post(
                f"{_BASE_URL}/threads/{thread_id}/runs",
                headers=_headers(),
                json={"assistant_id": ASSISTANT_ID},
            )
            run_resp.raise_for_status()
            run_id = run_resp.json()["id"]
        except Exception as exc:
            print(f"[openai] erro ao criar run: {exc}")
            return None

        # 3) poll até completar
        for _ in range(90):  # ~90s de timeout
            try:
                st = await client.get(
                    f"{_BASE_URL}/threads/{thread_id}/runs/{run_id}",
                    headers=_headers(),
                )
                st.raise_for_status()
                status = st.json().get("status")
                if status == "completed":
                    break
                if status in {"failed", "expired", "cancelled"}:
                    return None
            except Exception as exc:
                print(f"[openai] erro ao consultar status do run: {exc}")
                return None
            await asyncio.sleep(1.0)
        else:
            print("[openai] run não concluiu dentro do tempo limite.")
            return None

        # 4) lê mensagens e extrai texto do assistente
        try:
            msgs = await client.get(
                f"{_BASE_URL}/threads/{thread_id}/messages",
                headers=_headers(),
                params={"limit": 10},
            )
            msgs.raise_for_status()
            data = msgs.json().get("data", [])
        except Exception as exc:
            print(f"[openai] erro ao buscar mensagens: {exc}")
            return None

        # normalmente a API já retorna ordenado do mais recente p/ o mais antigo
        for m in data:
            if m.get("role") == "assistant":
                contents = m.get("content", [])
                # blocks: [{"type":"text","text":{"value":"..."}}]
                if isinstance(contents, list):
                    for c in contents:
                        if c.get("type") == "text":
                            txt = (c.get("text") or {}).get("value")
                            if txt:
                                return txt
                elif isinstance(contents, str):
                    return contents

        return None
