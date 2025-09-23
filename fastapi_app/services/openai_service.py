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
# Modelo para fallback (opcional). Se não informar, usaremos 'gpt-4o-mini'.
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "") or "gpt-4o-mini"
# System prompt para fallback via Chat Completions
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
    if user.thread_id:
        return user.thread_id

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(f"{_BASE_URL}/threads", headers=_headers_assistants(), json={})
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            print(f"[openai] criar thread: status={e.response.status_code} body={e.response.text}")
            raise
        data = resp.json()
        thread_id = data.get("id")
        if not thread_id:
            raise RuntimeError("OpenAI não retornou 'id' do thread.")

    user.thread_id = thread_id
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return thread_id


async def _chat_fallback(user_message: str) -> Optional[str]:
    """Fallback final via Chat Completions (garante resposta mesmo sem Assistants)."""
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
            if isinstance(msg, str) and msg.strip():
                return msg.strip()
            return None
    except httpx.HTTPStatusError as e:
        print(f"[openai] chat fallback status={e.response.status_code} body={e.response.text}")
        return None
    except Exception as exc:
        print(f"[openai] chat fallback erro: {exc}")
        return None


async def ask_assistant(thread_id: str, user_message: str) -> Optional[str]:
    """Tenta Assistants v2; se falhar, cai para modelo (run com 'model') e, por fim, Chat Completions."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        # 1) Adiciona a mensagem do usuário na thread (formato v2)
        try:
            r = await client.post(
                f"{_BASE_URL}/threads/{thread_id}/messages",
                headers=_headers_assistants(),
                json={"role": "user", "content": [{"type": "text", "text": user_message}]},
            )
            if r.status_code >= 400:
                # fallback para conteúdo simples (algumas contas aceitam só string)
                r2 = await client.post(
                    f"{_BASE_URL}/threads/{thread_id}/messages",
                    headers=_headers_assistants(),
                    json={"role": "user", "content": user_message},
                )
                try:
                    r2.raise_for_status()
                except httpx.HTTPStatusError as e:
                    print(f"[openai] postar mensagem: status={e.response.status_code} body={e.response.text}")
                    # não retorna ainda; vamos tentar fallback por modelo logo abaixo
        except Exception as exc:
            print(f"[openai] erro ao postar mensagem do usuário: {exc}")

        # 2) Cria o run com assistant_id; se 400/404/etc, tenta run com 'model'
        run_id: Optional[str] = None
        try:
            run_resp = await client.post(
                f"{_BASE_URL}/threads/{thread_id}/runs",
                headers=_headers_assistants(),
                json={"assistant_id": ASSISTANT_ID},
            )
            if run_resp.status_code >= 400:
                print(f"[openai] run assistant_id falhou: status={run_resp.status_code} body={run_resp.text}")
                # Fallback: run com 'model'
                run_resp = await client.post(
                    f"{_BASE_URL}/threads/{thread_id}/runs",
                    headers=_headers_assistants(),
                    json={"model": OPENAI_MODEL},
                )
            run_resp.raise_for_status()
            run_id = run_resp.json().get("id")
        except httpx.HTTPStatusError as e:
            print(f"[openai] erro ao criar run: status={e.response.status_code} body={e.response.text}")
        except Exception as exc:
            print(f"[openai] erro ao criar run: {exc}")

        # 3) Se ainda não temos run, cai direto para Chat Completions
        if not run_id:
            print("[openai] sem run_id — usando fallback Chat Completions.")
            return await _chat_fallback(user_message)

        # 4) Polling até completar
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
                    print(f"[openai] run terminou com status={status} details={jd}")
                    return await _chat_fallback(user_message)
                # Se requer ação (tools), como não temos ferramentas, melhor cair pro fallback
                if status == "requires_action":
                    print(f"[openai] run requires_action; usando fallback.")
                    return await _chat_fallback(user_message)
            except httpx.HTTPStatusError as e:
                print(f"[openai] polling run: status={e.response.status_code} body={e.response.text}")
                return await _chat_fallback(user_message)
            except Exception as exc:
                print(f"[openai] erro no polling do run: {exc}")
                return await _chat_fallback(user_message)
            await asyncio.sleep(1.0)
        else:
            print("[openai] run não concluiu no tempo; usando fallback.")
            return await _chat_fallback(user_message)

        # 5) Busca mensagens e extrai texto
        try:
            msgs = await client.get(
                f"{_BASE_URL}/threads/{thread_id}/messages",
                headers=_headers_assistants(),
                params={"limit": 20},
            )
            msgs.raise_for_status()
            data = msgs.json().get("data", [])
        except httpx.HTTPStatusError as e:
            print(f"[openai] listar mensagens: status={e.response.status_code} body={e.response.text}")
            return await _chat_fallback(user_message)
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

        # Nada encontrado -> fallback
        return await _chat_fallback(user_message)
