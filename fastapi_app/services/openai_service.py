"""
Integração com OpenAI Assistants API v2
- Fila por thread (Lock) para evitar 'active run'
- Espera automática quando há run ativo (400)
- Run com 'instructions' vindas de ASSISTANT_RUN_INSTRUCTIONS
- Fallback: run por 'model' e, por fim, Chat Completions
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Optional, Dict

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.db_models import User

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ASSISTANT_ID = os.getenv("ASSISTANT_ID", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "") or "gpt-4o-mini"
RUN_INSTRUCTIONS = os.getenv("ASSISTANT_RUN_INSTRUCTIONS", "").strip()

_BASE_URL = "https://api.openai.com/v1"

# -------- Locks por thread --------
_thread_locks: Dict[str, asyncio.Lock] = {}

def _lock_for(thread_id: str) -> asyncio.Lock:
    L = _thread_locks.get(thread_id)
    if L is None:
        L = asyncio.Lock()
        _thread_locks[thread_id] = L
    return L


def _headers_assistants() -> dict:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY não configurado.")
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


async def _wait_active_run(client: httpx.AsyncClient, thread_id: str, run_id: Optional[str], max_s: int = 45) -> None:
    """Espera um run ativo terminar (queued/in_progress/requires_action/cancelling)."""
    if not run_id:
        # Sem run_id explícito; tentamos pequena espera (caso run esteja finalizando)
        for _ in range(max_s):
            await asyncio.sleep(1)
        return
    for _ in range(max_s):
        try:
            st = await client.get(f"{_BASE_URL}/threads/{thread_id}/runs/{run_id}", headers=_headers_assistants())
            st.raise_for_status()
            js = st.json()
            status = js.get("status")
            if status in {"completed", "failed", "expired", "cancelled"}:
                return
        except Exception:
            # Se não conseguimos consultar, não travamos o fluxo
            return
        await asyncio.sleep(1)


async def _chat_fallback(user_message: str) -> Optional[str]:
    """Fallback via Chat Completions (garante resposta mesmo sem Assistants)."""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{_BASE_URL}/chat/completions",
                headers=_headers_chat(),
                json={
                    "model": OPENAI_MODEL,
                    "messages": [
                        {"role": "system", "content": "Você é a Luna, responda sempre em PT-BR de forma direta."},
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


async def ask_assistant(thread_id: str, user_message: str) -> Optional[str]:
    """
    - Sequencializa por thread (Lock)
    - Tenta Assistants v2; se 'run ativo', espera e tenta de novo
    - Se ainda falhar, usa run por 'model' e, por fim, Chat Completions
    """
    async with _lock_for(thread_id):
        async with httpx.AsyncClient(timeout=60.0) as client:
            # 1) Adiciona mensagem do usuário
            try:
                r = await client.post(
                    f"{_BASE_URL}/threads/{thread_id}/messages",
                    headers=_headers_assistants(),
                    json={"role": "user", "content": [{"type": "text", "text": user_message}]},
                )
                if r.status_code == 400 and "active run" in r.text.lower():
                    # Extrai run_id da mensagem de erro, se vier
                    m = re.search(r"(run_[a-zA-Z0-9]+)", r.text)
                    run_id = m.group(1) if m else None
                    print(f"[openai] run ativo detectado ({run_id}); aguardando…")
                    await _wait_active_run(client, thread_id, run_id)
                    # reposta
                    r = await client.post(
                        f"{_BASE_URL}/threads/{thread_id}/messages",
                        headers=_headers_assistants(),
                        json={"role": "user", "content": [{"type": "text", "text": user_message}]},
                    )
                r.raise_for_status()
            except Exception as exc:
                print(f"[openai] erro ao postar mensagem do usuário: {exc}")

            # 2) Cria run com assistant_id + instructions; se falhar, tenta com model
            run_id: Optional[str] = None
            body = {"assistant_id": ASSISTANT_ID}
            if RUN_INSTRUCTIONS:
                body["instructions"] = RUN_INSTRUCTIONS

            try:
                run_resp = await client.post(
                    f"{_BASE_URL}/threads/{thread_id}/runs",
                    headers=_headers_assistants(),
                    json=body,
                )
                if run_resp.status_code == 400 and "active run" in run_resp.text.lower():
                    m = re.search(r"(run_[a-zA-Z0-9]+)", run_resp.text)
                    arun = m.group(1) if m else None
                    print(f"[openai] run ativo ao criar novo; aguardando {arun}…")
                    await _wait_active_run(client, thread_id, arun)
                    run_resp = await client.post(
                        f"{_BASE_URL}/threads/{thread_id}/runs",
                        headers=_headers_assistants(),
                        json=body,
                    )

                # fallback: run por model
                if run_resp.status_code >= 400:
                    print(f"[openai] run assistant_id falhou: {run_resp.status_code} {run_resp.text[:200]}")
                    body2 = {"model": OPENAI_MODEL}
                    if RUN_INSTRUCTIONS:
                        body2["instructions"] = RUN_INSTRUCTIONS
                    run_resp = await client.post(
                        f"{_BASE_URL}/threads/{thread_id}/runs",
                        headers=_headers_assistants(),
                        json=body2,
                    )
                run_resp.raise_for_status()
                run_id = run_resp.json().get("id")
            except httpx.HTTPStatusError as e:
                print(f"[openai] erro ao criar run: status={e.response.status_code} body={e.response.text}")
            except Exception as exc:
                print(f"[openai] erro ao criar run: {exc}")

            if not run_id:
                print("[openai] sem run_id — usando Chat Completions.")
                return await _chat_fallback(user_message)

            # 3) Polling
            for _ in range(60):
                try:
                    st = await client.get(
                        f"{_BASE_URL}/threads/{thread_id}/runs/{run_id}",
                        headers=_headers_assistants(),
                    )
                    st.raise_for_status()
                    js = st.json()
                    status = js.get("status")
                    if status == "completed":
                        break
                    if status in {"failed", "expired", "cancelled"}:
                        print(f"[openai] run terminou com status={status}")
                        return await _chat_fallback(user_message)
                    if status == "requires_action":
                        print("[openai] requires_action -> fallback")
                        return await _chat_fallback(user_message)
                except Exception as exc:
                    print(f"[openai] polling erro: {exc}")
                    return await _chat_fallback(user_message)
                await asyncio.sleep(1.0)
            else:
                print("[openai] run não concluiu no tempo; fallback")
                return await _chat_fallback(user_message)

            # 4) Coleta respostas
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
            return await _chat_fallback(user_message)
