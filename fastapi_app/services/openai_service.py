# fastapi_app/services/openai_service.py
from __future__ import annotations

import os
import asyncio
from typing import Any, Dict, Optional, Tuple, List

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.db_models import User

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ASSISTANT_ID = os.getenv("ASSISTANT_ID", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # usado no fallback de chat
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY não configurada.")

# Header exigido para Assistants v2
# Ref. geral sobre Assistants v2 e descontinuação da v1 beta: https://platform.openai.com/docs/deprecations
_OPENAI_HEADERS = {
    "Authorization": f"Bearer {OPENAI_API_KEY}",
    "OpenAI-Beta": "assistants=v2",
}

_THREAD_CACHE: Dict[int, str] = {}  # user_id -> thread_id (fallback em memória)

async def _create_thread(client: httpx.AsyncClient) -> str:
    r = await client.post(f"{OPENAI_BASE_URL}/threads", headers=_OPENAI_HEADERS, json={})
    r.raise_for_status()
    data = r.json()
    return data["id"]

async def get_or_create_thread(session: AsyncSession, user: User) -> str:
    # tenta achar um atributo persistido no modelo
    for attr in ("openai_thread_id", "thread_id", "openai_thread"):
        tid = getattr(user, attr, None)
        if isinstance(tid, str) and tid.strip():
            return tid.strip()

    # cache em memória (ajuda caso o modelo não tenha coluna)
    if user.id in _THREAD_CACHE:
        return _THREAD_CACHE[user.id]

    async with httpx.AsyncClient(timeout=20.0) as client:
        thread_id = await _create_thread(client)

    # tenta persistir em alguma coluna conhecida; se não existir, ignora
    saved = False
    for attr in ("openai_thread_id", "thread_id", "openai_thread"):
        if hasattr(user, attr):
            try:
                setattr(user, attr, thread_id)
                await session.commit()
                saved = True
                break
            except Exception:
                await session.rollback()
                break

    if not saved:
        _THREAD_CACHE[user.id] = thread_id
    return thread_id

async def _list_messages_text(client: httpx.AsyncClient, thread_id: str) -> str:
    r = await client.get(
        f"{OPENAI_BASE_URL}/threads/{thread_id}/messages",
        headers=_OPENAI_HEADERS,
        params={"limit": 10, "order": "desc"},
    )
    r.raise_for_status()
    data = r.json()
    for msg in data.get("data", []):
        if msg.get("role") == "assistant":
            for part in msg.get("content", []):
                if part.get("type") == "text":
                    return (part.get("text", {}) or {}).get("value", "") or ""
    return ""

async def _submit_dummy_tool_outputs(client: httpx.AsyncClient, thread_id: str, run_id: str, required: dict) -> None:
    """
    Se o Assistente pedir ferramentas (requires_action), submetemos saídas mínimas ("ok") para destravar o fluxo.
    """
    tool_outputs: List[Dict[str, str]] = []
    for call in required.get("tool_calls", []):
        tool_outputs.append({"tool_call_id": call["id"], "output": "ok"})
    if tool_outputs:
        r = await client.post(
            f"{OPENAI_BASE_URL}/threads/{thread_id}/runs/{run_id}/submit_tool_outputs",
            headers=_OPENAI_HEADERS,
            json={"tool_outputs": tool_outputs},
        )
        # mesmo que falhe, continuamos polling
        try:
            r.raise_for_status()
        except Exception:
            pass

async def ask_assistant(thread_id: str, text: str, *, max_wait_seconds: int = 18) -> str:
    """
    Cria mensagem no thread, dispara um Run e faz polling curto até obter a resposta.
    - Em 'requires_action': envia 'tool_outputs' mínimos para destravar.
    - Se exceder timeout: tenta fallback chat.completions com OPENAI_MODEL.
    """
    if not ASSISTANT_ID:
        # Sem assistente definido: cai direto no fallback chat.completions
        return await _chat_fallback(text)

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 1) Adiciona mensagem do usuário
        try:
            r = await client.post(
                f"{OPENAI_BASE_URL}/threads/{thread_id}/messages",
                headers=_OPENAI_HEADERS,
                json={"role": "user", "content": text},
            )
            r.raise_for_status()
        except Exception as exc:
            print(f"[openai] add message failed: {exc!r}; falling back to chat.")
            return await _chat_fallback(text)

        # 2) Cria Run
        try:
            r = await client.post(
                f"{OPENAI_BASE_URL}/threads/{thread_id}/runs",
                headers=_OPENAI_HEADERS,
                json={"assistant_id": ASSISTANT_ID},
            )
            r.raise_for_status()
        except Exception as exc:
            print(f"[openai] run create failed: {exc!r}; falling back to chat.")
            return await _chat_fallback(text)

        run = r.json()
        run_id = run["id"]

        # 3) Poll até completar ou estourar timeout curto
        slept = 0.0
        step = 1.0
        while slept < max_wait_seconds:
            try:
                r = await client.get(
                    f"{OPENAI_BASE_URL}/threads/{thread_id}/runs/{run_id}",
                    headers=_OPENAI_HEADERS,
                )
                r.raise_for_status()
                cur = r.json()
            except Exception as exc:
                print(f"[openai] retrieve run error: {exc!r}")
                await asyncio.sleep(step)
                slept += step
                continue

            status = cur.get("status")
            if status == "completed":
                return await _list_messages_text(client, thread_id)
            if status == "requires_action":
                required = (cur.get("required_action") or {}).get("submit_tool_outputs", {})
                await _submit_dummy_tool_outputs(client, thread_id, run_id, required)
            elif status in {"failed", "cancelled", "expired"}:
                print(f"[openai] run finished with status={status}")
                break

            await asyncio.sleep(step)
            slept += step

        # 4) Timeout → fallback chat
        return await _chat_fallback(text)

async def _chat_fallback(text: str) -> str:
    """
    Fallback rápido via /chat/completions, com uma instrução mínima para manter o protocolo #tools(...).
    """
    if not OPENAI_MODEL:
        return "Certo!"
    sys = (
        "Você é a Luna, assistente da Verbo Vídeo. Seja objetiva, amigável e, quando "
        "quiser disparar ações externas, inclua tags no final do texto como #tools(menu), "
        "#tools(video) ou #tools(handoff)."
    )
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                f"{OPENAI_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                json={
                    "model": OPENAI_MODEL,
                    "messages": [
                        {"role": "system", "content": sys},
                        {"role": "user", "content": text},
                    ],
                    "temperature": 0.4,
                },
            )
            r.raise_for_status()
            data = r.json()
            return (data["choices"][0]["message"]["content"] or "").strip() or "Certo!"
    except Exception as exc:
        print(f"[openai] chat fallback failed: {exc!r}")
        return "Certo!"
