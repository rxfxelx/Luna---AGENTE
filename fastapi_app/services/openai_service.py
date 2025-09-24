"""
Integração com OpenAI Assistants API v2.

Expõe:
- get_or_create_thread(session: AsyncSession, user: User) -> str
- ask_assistant(thread_id: str, user_message: str) -> Optional[str]

Inclui:
- Injeção de instruções via ENV ASSISTANT_RUN_INSTRUCTIONS em cada run.
- Gestão de run ativa (aguarda/cancela) para não quebrar a thread.
- Fallback: se Assistants falhar, usa Chat Completions (OPENAI_MODEL).
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional, Tuple, Dict, Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.db_models import User

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ASSISTANT_ID = os.getenv("ASSISTANT_ID", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "") or "gpt-4o-mini"

# Prompt injetado por run (cole seu texto completo do fluxo aqui nas Variables do Railway)
ASSISTANT_RUN_INSTRUCTIONS = os.getenv("ASSISTANT_RUN_INSTRUCTIONS", "").strip()

# System padrão para fallback Chat Completions
OPENAI_FALLBACK_SYSTEM = os.getenv(
    "OPENAI_FALLBACK_SYSTEM",
    "Você é a Luna, uma assistente útil e direta. Responda em português do Brasil.",
)

# Controles extras
OPENAI_CANCEL_STUCK_RUNS = (os.getenv("OPENAI_CANCEL_STUCK_RUNS", "true") or "").lower() == "true"
OPENAI_RUN_POLL_SECONDS = int(os.getenv("OPENAI_RUN_POLL_SECONDS", "1"))
OPENAI_RUN_POLL_MAX_SECONDS = int(os.getenv("OPENAI_RUN_POLL_MAX_SECONDS", "60"))

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
        resp.raise_for_status()
        data = resp.json()
        thread_id = data.get("id")
        if not thread_id:
            raise RuntimeError("OpenAI não retornou 'id' do thread.")

    user.thread_id = thread_id
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return thread_id


# ---------------- Helpers de run/threads ----------------

_ACTIVE_STATUSES = {"queued", "in_progress", "requires_action"}

async def _latest_run(client: httpx.AsyncClient, thread_id: str) -> Tuple[Optional[str], Optional[str]]:
    """Retorna (run_id, status) do run mais recente (ou None, None)."""
    try:
        r = await client.get(f"{_BASE_URL}/threads/{thread_id}/runs", headers=_headers_assistants(), params={"limit": 1})
        if r.status_code >= 400:
            return (None, None)
        data = r.json().get("data", [])
        if not data:
            return (None, None)
        run = data[0]
        return (run.get("id"), run.get("status"))
    except Exception:
        return (None, None)

async def _cancel_run(client: httpx.AsyncClient, thread_id: str, run_id: str) -> None:
    try:
        await client.post(
            f"{_BASE_URL}/threads/{thread_id}/runs/{run_id}/cancel",
            headers=_headers_assistants(),
            json={},
        )
    except Exception:
        pass

async def _wait_until_no_active_run(client: httpx.AsyncClient, thread_id: str, max_seconds: int) -> None:
    """Espera até não haver run ativa; se ficar em requires_action e está habilitado, cancela."""
    total = 0
    while total < max_seconds:
        run_id, status = await _latest_run(client, thread_id)
        if not run_id or not status or status not in _ACTIVE_STATUSES:
            return
        if status == "requires_action" and OPENAI_CANCEL_STUCK_RUNS:
            await _cancel_run(client, thread_id, run_id)
            # dá um pequeno respiro para o cancel efetivar
            await asyncio.sleep(0.5)
        else:
            await asyncio.sleep(OPENAI_RUN_POLL_SECONDS)
            total += OPENAI_RUN_POLL_SECONDS

async def _post_user_message_allowing_active_run(
    client: httpx.AsyncClient, thread_id: str, user_message: str
) -> bool:
    """Posta a mensagem do usuário; se houver run ativa, espera/cancela e tenta de novo."""
    # 1ª tentativa
    r = await client.post(
        f"{_BASE_URL}/threads/{thread_id}/messages",
        headers=_headers_assistants(),
        json={"role": "user", "content": [{"type": "text", "text": user_message}]},
    )
    if r.status_code < 400:
        return True

    # Se falhou por run ativa, espera limpar e tenta fallback (string simples)
    body = r.text or ""
    if "active run" in body or "already has an active run" in body:
        await _wait_until_no_active_run(client, thread_id, max_seconds=OPENAI_RUN_POLL_MAX_SECONDS)
        # tenta novamente (blocks)
        r2 = await client.post(
            f"{_BASE_URL}/threads/{thread_id}/messages",
            headers=_headers_assistants(),
            json={"role": "user", "content": [{"type": "text", "text": user_message}]},
        )
        if r2.status_code < 400:
            return True
        # tenta como string simples
        r3 = await client.post(
            f"{_BASE_URL}/threads/{thread_id}/messages",
            headers=_headers_assistants(),
            json={"role": "user", "content": user_message},
        )
        return r3.status_code < 400

    # Outra falha qualquer → tenta string simples uma vez
    r2 = await client.post(
        f"{_BASE_URL}/threads/{thread_id}/messages",
        headers=_headers_assistants(),
        json={"role": "user", "content": user_message},
    )
    return r2.status_code < 400


# ---------------- Fallback Chat ----------------

async def _chat_fallback(user_message: str) -> Optional[str]:
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
    except Exception as exc:
        print(f"[openai] chat fallback erro: {exc}")
        return None


# ---------------- Fluxo principal ----------------

async def ask_assistant(thread_id: str, user_message: str) -> Optional[str]:
    """Assistants v2 com gestão de run ativa; fallback para model e Chat Completions."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        # Antes de postar nova mensagem, assegura que não há run ativa
        await _wait_until_no_active_run(client, thread_id, max_seconds=OPENAI_RUN_POLL_MAX_SECONDS)

        ok = await _post_user_message_allowing_active_run(client, thread_id, user_message)
        if not ok:
            # não conseguiu postar no thread → cai para Chat Completions
            return await _chat_fallback(user_message)

        # Cria run (assistant_id; se falhar usa model), com instructions do ENV
        run_payload: Dict[str, Any] = {"assistant_id": ASSISTANT_ID}
        if ASSISTANT_RUN_INSTRUCTIONS:
            run_payload["instructions"] = ASSISTANT_RUN_INSTRUCTIONS

        run_id: Optional[str] = None
        try:
            run_resp = await client.post(
                f"{_BASE_URL}/threads/{thread_id}/runs",
                headers=_headers_assistants(),
                json=run_payload,
            )
            if run_resp.status_code >= 400:
                # Tenta run por model
                print(f"[openai] run assistant_id falhou: {run_resp.status_code} body={run_resp.text}")
                run_payload = {"model": OPENAI_MODEL}
                if ASSISTANT_RUN_INSTRUCTIONS:
                    run_payload["instructions"] = ASSISTANT_RUN_INSTRUCTIONS
                run_resp = await client.post(
                    f"{_BASE_URL}/threads/{thread_id}/runs",
                    headers=_headers_assistants(),
                    json=run_payload,
                )
            run_resp.raise_for_status()
            run_id = run_resp.json().get("id")
        except Exception as exc:
            print(f"[openai] erro ao criar run: {exc}")

        if not run_id:
            return await _chat_fallback(user_message)

        # Polling até completar; se requires_action, cancela (limpa) e cai no fallback
        total = 0
        while total < OPENAI_RUN_POLL_MAX_SECONDS:
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
                    return await _chat_fallback(user_message)
                if status == "requires_action":
                    if OPENAI_CANCEL_STUCK_RUNS:
                        await _cancel_run(client, thread_id, run_id)
                    return await _chat_fallback(user_message)
            except Exception as exc:
                print(f"[openai] polling run: {exc}")
                return await _chat_fallback(user_message)

            await asyncio.sleep(OPENAI_RUN_POLL_SECONDS)
            total += OPENAI_RUN_POLL_SECONDS
        else:
            # timeout
            return await _chat_fallback(user_message)

        # Busca mensagens e extrai texto do assistente
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
