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
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "")  # opcional: fallback

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


async def get_or_create_thread(session: AsyncSession, user: User) -> str:
    if user.thread_id:
        return user.thread_id

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(f"{_BASE_URL}/threads", headers=_headers(), json={})
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


async def ask_assistant(thread_id: str, user_message: str) -> Optional[str]:
    async with httpx.AsyncClient(timeout=60.0) as client:
        # 1) Adiciona a mensagem do usuário
        try:
            r = await client.post(
                f"{_BASE_URL}/threads/{thread_id}/messages",
                headers=_headers(),
                json={"role": "user", "content": [{"type": "text", "text": user_message}]},
            )
            if r.status_code >= 400:
                # fallback para conteúdo simples
                r2 = await client.post(
                    f"{_BASE_URL}/threads/{thread_id}/messages",
                    headers=_headers(),
                    json={"role": "user", "content": user_message},
                )
                try:
                    r2.raise_for_status()
                except httpx.HTTPStatusError as e:
                    print(f"[openai] postar mensagem: status={e.response.status_code} body={e.response.text}")
                    return None
        except Exception as exc:
            print(f"[openai] erro ao postar mensagem do usuário: {exc}")
            return None

        # 2) Cria o run (assistant_id); se 400, loga corpo e tenta fallback por modelo (opcional)
        try:
            run_resp = await client.post(
                f"{_BASE_URL}/threads/{thread_id}/runs",
                headers=_headers(),
                json={"assistant_id": ASSISTANT_ID},
            )
            if run_resp.status_code == 400 and OPENAI_MODEL:
                print(f"[openai] run 400 com assistant_id; tentando fallback model={OPENAI_MODEL!r}")
                run_resp = await client.post(
                    f"{_BASE_URL}/threads/{thread_id}/runs",
                    headers=_headers(),
                    json={"model": OPENAI_MODEL},
                )
            run_resp.raise_for_status()
            run_id = run_resp.json().get("id")
            if not run_id:
                print(f"[openai] run criado sem id? body={run_resp.text}")
                return None
        except httpx.HTTPStatusError as e:
            print(f"[openai] erro ao criar run: status={e.response.status_code} body={e.response.text}")
            return None
        except Exception as exc:
            print(f"[openai] erro ao criar run: {exc}")
            return None

        # 3) Acompanha status
        for _ in range(60):  # ~60s
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
                    print(f"[openai] run terminou com status={status}")
                    return None
            except httpx.HTTPStatusError as e:
                print(f"[openai] polling run: status={e.response.status_code} body={e.response.text}")
                return None
            except Exception as exc:
                print(f"[openai] erro no polling do run: {exc}")
                return None
            await asyncio.sleep(1.0)
        else:
            print("[openai] run não concluiu dentro do tempo limite.")
            return None

        # 4) Busca mensagens e extrai texto
        try:
            msgs = await client.get(
                f"{_BASE_URL}/threads/{thread_id}/messages",
                headers=_headers(),
                params={"limit": 20},
            )
            msgs.raise_for_status()
            data = msgs.json().get("data", [])
        except httpx.HTTPStatusError as e:
            print(f"[openai] listar mensagens: status={e.response.status_code} body={e.response.text}")
            return None
        except Exception as exc:
            print(f"[openai] erro ao buscar mensagens: {exc}")
            return None

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
        return None
