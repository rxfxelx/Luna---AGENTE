# fastapi_app/services/openai_service.py
"""
Integração com OpenAI Assistants v2.

Principais funções expostas:
- get_or_create_thread(session, user) -> str
- ask_assistant_actions(thread_id: str, text: str) -> dict
    Retorna {"reply_text": str, "actions": [{"name": str, "args": dict}, ...]}
- ask_assistant(thread_id: str, text: str) -> str
    Compatibilidade: retorna apenas o texto final do assistant.

Observações importantes:
- Quando o run entra em `requires_action`, coletamos as tool-calls e
  SUBMETEMOS tool_outputs "OK" para liberar o run e obter a resposta final.
  Também devolvemos as ações para o caller executá-las (ex.: enviar menu/vídeo).
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List, Optional, Tuple

from openai import AsyncOpenAI

from ..models.db_models import User  # apenas para type hints, se quiser


# -------------------- ENV --------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ASSISTANT_ID = os.getenv("ASSISTANT_ID", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY não configurada.")
if not ASSISTANT_ID:
    # não paramos a app, mas avisamos
    print("[openai] WARN: ASSISTANT_ID não configurado – use tools por texto (#tools(...)) ou configure o ID.")

_client = AsyncOpenAI(api_key=OPENAI_API_KEY)


# -------------------- helpers --------------------
def _get_user_thread_attr_name(user: Any) -> Optional[str]:
    """
    Descobre dinamicamente qual atributo do User guarda o thread_id.
    Tenta em ordem alguns nomes comuns.
    """
    for attr in ("openai_thread_id", "assistant_thread_id", "thread_id", "thread"):
        if hasattr(user, attr):
            return attr
    return None


async def get_or_create_thread(session, user: User) -> str:
    """
    Obtém (ou cria) o thread_id e persiste no usuário.
    Não altera seu schema: usa o primeiro atributo existente dentre:
    openai_thread_id / assistant_thread_id / thread_id / thread.
    Se nenhum existir, tenta usar 'openai_thread_id' (silenciosamente).
    """
    attr = _get_user_thread_attr_name(user) or "openai_thread_id"
    tid: Optional[str] = getattr(user, attr, None)
    if tid:
        return tid

    thread = await _client.beta.threads.create()
    tid = thread.id
    try:
        setattr(user, attr, tid)
        session.add(user)
        await session.commit()
    except Exception as exc:
        print(f"[openai] WARN: falha ao persistir thread_id no usuário ({attr}): {exc!r}")
    return tid


async def _poll_run(thread_id: str, run_id: str, *, interval: float = 0.8, timeout: float = 45.0) -> Dict[str, Any]:
    """
    Faz polling do run até sair de estados transitórios.
    Retorna o objeto run final.
    """
    elapsed = 0.0
    while True:
        run = await _client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run_id)
        status = getattr(run, "status", "")
        if status not in {"queued", "in_progress", "requires_action"}:
            return run.model_dump() if hasattr(run, "model_dump") else dict(run)
        await asyncio.sleep(interval)
        elapsed += interval
        if elapsed >= timeout:
            return run.model_dump() if hasattr(run, "model_dump") else dict(run)


def _extract_text_from_messages(messages: Any) -> str:
    """
    Concatena o conteúdo textual das mensagens assistant mais recentes.
    """
    out_parts: List[str] = []
    try:
        for m in messages.data:
            if m.role != "assistant":
                continue
            for piece in m.content or []:
                if getattr(piece, "type", "") == "text":
                    out_parts.append(piece.text.value or "")
            if out_parts:
                break  # pega apenas a primeira mensagem assistant mais recente
    except Exception:
        # fallback generoso
        try:
            for m in messages.get("data", []):  # type: ignore[assignment]
                if m.get("role") != "assistant":
                    continue
                for piece in m.get("content", []):
                    if piece.get("type") == "text":
                        out_parts.append(piece["text"]["value"])
                if out_parts:
                    break
        except Exception:
            pass
    return "\n".join(p for p in out_parts if p).strip()


def _parse_tool_calls_from_run(run_obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    A partir de um objeto run em 'requires_action', extrai as tool-calls
    (nome e argumentos em dict).
    """
    actions: List[Dict[str, Any]] = []
    try:
        ra = run_obj.get("required_action", {}) or {}
        st = ra.get("submit_tool_outputs", {}) or {}
        for tc in st.get("tool_calls", []) or []:
            func = tc.get("function", {}) or {}
            name = (func.get("name") or "").strip()
            args_text = func.get("arguments") or "{}"
            args: Dict[str, Any]
            try:
                args = json.loads(args_text)
            except Exception:
                args = {}
            if name:
                actions.append({"name": name, "args": args, "tool_call_id": tc.get("id")})
    except Exception as exc:
        print(f"[openai] WARN: falha ao extrair tool-calls: {exc!r}")
    return actions


async def ask_assistant_actions(thread_id: str, text: str) -> Dict[str, Any]:
    """
    Envia `text` ao thread, executa um run e retorna:
      {"reply_text": str, "actions": [{"name": ..., "args": {...}}]}
    Obs.: Se houver tools, SUBMETE tool_outputs "OK" para destravar o run
    (o caller executa de verdade as ações no WhatsApp).
    """
    # 1) adiciona a mensagem do usuário
    await _client.beta.threads.messages.create(
        thread_id=thread_id,
        role="user",
        content=text or "",
    )

    # 2) cria o run
    run = await _client.beta.threads.runs.create(
        thread_id=thread_id,
        assistant_id=ASSISTANT_ID or "",
        model=OPENAI_MODEL or None,  # opcional
    )
    run_obj = run.model_dump() if hasattr(run, "model_dump") else dict(run)

    # 3) polling
    run_obj = await _poll_run(thread_id, run_obj["id"])

    actions: List[Dict[str, Any]] = []
    # 3a) se precisa de ação, colete as tools e submeta "OK" p/ continuar
    if run_obj.get("status") == "requires_action":
        actions = _parse_tool_calls_from_run(run_obj)
        tool_outputs = []
        for a in actions:
            # Mensagem curta só para o Assistant "acreditar" que executamos.
            tool_outputs.append({"tool_call_id": a.get("tool_call_id"), "output": "OK"})
        try:
            await _client.beta.threads.runs.submit_tool_outputs(
                thread_id=thread_id,
                run_id=run_obj["id"],
                tool_outputs=tool_outputs,
            )
            # poll novamente até concluir
            run_obj = await _poll_run(thread_id, run_obj["id"])
        except Exception as exc:
            print(f"[openai] submit_tool_outputs falhou: {exc!r}")

    # 4) busca a última resposta textual do assistant
    try:
        msgs = await _client.beta.threads.messages.list(thread_id=thread_id, order="desc", limit=10)
        reply_text = _extract_text_from_messages(msgs)
    except Exception as exc:
        print(f"[openai] WARN: falha ao ler mensagens do thread: {exc!r}")
        reply_text = ""

    return {"reply_text": reply_text, "actions": actions}


async def ask_assistant(thread_id: str, text: str) -> str:
    """
    Compatibilidade: devolve apenas o texto final.
    """
    res = await ask_assistant_actions(thread_id, text)
    return res.get("reply_text") or ""