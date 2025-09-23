"""
Integração com OpenAI Assistants API v2.

Expõe:
- get_or_create_thread(session: AsyncSession, user: User) -> str
- ask_assistant(thread_id: str, user_message: str) -> Optional[str]
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
# Fallback de modelo se não houver assistant válido
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "") or "gpt-4o-mini"
OPENAI_FALLBACK_SYSTEM = os.getenv(
    "OPENAI_FALLBACK_SYSTEM",
    "Você é a Luna, uma assistente útil e direta. Responda em português do Brasil.",
)

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
    """Retorna o thread_id do usuário; cria no OpenAI se não existir."""
    if user.thread_id:
        return user.thread_id

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(f"{_BASE_URL}/threads", headers=_headers_assistants(), json={})
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            print(f"[openai] criar thread: status={e.response.status_code} body={e.response.text}")
            raise
        thread_id = resp.json().get("id")
        if not thread_id:
            raise RuntimeError("OpenAI não retornou 'id' do thread.")

    user.thread_id = thread_id
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return thread_id


async def _chat_fallback(user_message: str) -> Optional[str]:
    """Último fallback via Chat Completions (garante resposta sem Assistants)."""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{_BASE_URL}/chat/completions",
                headers=_headers_chat(),
                json={
                    "model": OPENAI_MODEL,
                    "messages": [
                        {"role": "system", "content": OPENAI_FALLBACK_SYSTEM},
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
    except httpx.HTTPStatusError as e:
        print(f"[openai] chat fallback status={e.response.status_code} body={e.response.text}")
        return None
    except Exception as exc:
        print(f"[openai] chat fallback erro: {exc}")
        return None


async def ask_assistant(thread_id: str, user_message: str) -> Optional[str]:
    """Assistants v2 com fallbacks: run por assistant_id → run por model → chat completions."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        # 1) Posta a mensagem do usuário
        try:
            r = await client.post(
                f"{_BASE_URL}/threads/{thread_id}/messages",
                headers=_headers_assistants(),
                json={"role": "user", "content": [{"type": "text", "text": user_message}]},
            )
            if r.status_code >= 400:
                # fallback para conteúdo simples (algumas contas ainda aceitam string)
                r2 = await client.post(
                    f"{_BASE_URL}/threads/{thread_id}/messages",
                    headers=_headers_assistants(),
                    json={"role": "user", "content": user_message},
                )
                r2.raise_for_status()
        except Exception as exc:
            print(f"[openai] erro ao postar mensagem do usuário: {exc}")

        # 2) Cria run
        run_id: Optional[str] = None
        try:
            run = await client.post(
                f"{_BASE_URL}/threads/{thread_id}/runs",
                headers=_headers_assistants(),
                json={"assistant_id": ASSISTANT_ID},
            )
            if run.status_code >= 400:
                print(f"[openai] run assistant_id falhou: status={run.status_code} body={run.text}")
                run = await client.post(
                    f"{_BASE_URL}/threads/{thread_id}/runs",
                    headers=_headers_assistants(),
                    json={"model": OPENAI_MODEL},
                )
            run.raise_for_status()
            run_id = run.json().get("id")
        except Exception as exc:
            print(f"[openai] erro ao criar run: {exc}")

        # 3) Se não criou run, cai p/ Chat Completions
        if not run_id:
            return await _chat_fallback(user_message)

        # 4) Polling
        for _ in range(60):
            try:
                st = await client.get(
                    f"{_BASE_URL}/threads/{thread_id}/runs/{run_id}",
                    headers=_headers_assistants(),
                )
                st.raise_for_status()
                jd = st.json()
                status = jd.get("status")
                if status == "completed":
                    break
                if status in {"failed", "expired", "cancelled"}:
                    print(f"[openai] run terminou com status={status}")
                    return await _chat_fallback(user_message)
                if status == "requires_action":
                    print("[openai] run requires_action; usando fallback.")
                    return await _chat_fallback(user_message)
            except Exception as exc:
                print(f"[openai] polling run: {exc}")
                return await _chat_fallback(user_message)
            await asyncio.sleep(1.0)
        else:
            print("[openai] run não concluiu no tempo; fallback.")
            return await _chat_fallback(user_message)

        # 5) Lê mensagens e extrai texto
        try:
            msgs = await client.get(
                f"{_BASE_URL}/threads/{thread_id}/messages",
                headers=_headers_assistants(),
                params={"limit": 20},
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

        # Sem texto → fallback final
        return await _chat_fallback(user_message)
