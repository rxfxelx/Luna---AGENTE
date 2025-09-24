# fastapi_app/services/openai_service.py
"""
Integração com OpenAI Assistants API v2.

Expõe:
- get_or_create_thread(session: AsyncSession, user: User) -> str
- ask_assistant(thread_id: str, user_message: str, phone: str) -> dict {"text": str|None, "did_tool": bool}
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Optional, Dict, Any, List

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.db_models import User
from .uazapi_service import (
    send_whatsapp_message,
    send_menu,
    LUNA_MENU_TEXT,
    LUNA_MENU_YES,
    LUNA_MENU_NO,
    LUNA_MENU_FOOTER,
    LUNA_VIDEO_URL,
    LUNA_VIDEO_CAPTION,
    LUNA_VIDEO_AFTER_TEXT,
    LUNA_END_TEXT,
)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ASSISTANT_ID = os.getenv("ASSISTANT_ID", "")
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


async def _execute_tool_call(name: str, args: Dict[str, Any], phone: str) -> Dict[str, Any]:
    """
    Mapeia tools → chamadas reais na Uazapi.
    Retorna um dicionário qualquer (serializável em JSON) para enviar em submit_tool_outputs.
    """
    try:
        if name == "enviar_caixinha_interesse":
            text = (args.get("text") or LUNA_MENU_TEXT).strip()
            choices = args.get("choices") or [LUNA_MENU_YES, LUNA_MENU_NO]
            footer = (args.get("footerText") or LUNA_MENU_FOOTER).strip()
            res = await send_menu(phone=phone, text=text, choices=choices, footer_text=footer)
            print(f"[tools] enviar_caixinha_interesse -> ok")
            return {"ok": True, "menu": res}

        if name == "enviar_video":
            url = args.get("url") or LUNA_VIDEO_URL
            caption = args.get("caption") or LUNA_VIDEO_CAPTION
            if not url:
                raise RuntimeError("Sem URL de vídeo (defina LUNA_VIDEO_URL ou passe 'url' nos args).")
            res_media = await send_whatsapp_message(
                phone=phone,
                content=caption or "",
                type_="media",
                media_url=url,
                caption=caption or "",
            )
            if LUNA_VIDEO_AFTER_TEXT:
                try:
                    await send_whatsapp_message(phone=phone, content=LUNA_VIDEO_AFTER_TEXT, type_="text")
                except Exception as e:
                    print(f"[tools] enviar_video -> after_text falhou: {e!r}")
            print(f"[tools] enviar_video -> ok")
            return {"ok": True, "media": res_media}

        if name == "enviar_msg":
            text = (args.get("text") or "").strip()
            if not text:
                return {"ok": False, "reason": "texto vazio"}
            res = await send_whatsapp_message(phone=phone, content=text, type_="text")
            print(f"[tools] enviar_msg -> ok")
            return {"ok": True, "message": res}

        if name == "numero_novo":
            # Sem CRM aqui; apenas acknowledge
            print(f"[tools] numero_novo -> ok (ack)")
            return {"ok": True, "stored": True}

        if name == "excluir_dados_lead":
            # Sem remoção física de dados neste backend; envia mensagem de encerramento se existir
            if LUNA_END_TEXT:
                try:
                    await send_whatsapp_message(phone=phone, content=LUNA_END_TEXT, type_="text")
                except Exception as e:
                    print(f"[tools] excluir_dados_lead -> enviar fim falhou: {e!r}")
            print(f"[tools] excluir_dados_lead -> ok (ack)")
            return {"ok": True, "done": True}

        return {"ok": False, "reason": f"tool desconhecida: {name}"}
    except Exception as exc:
        print(f"[tools] {name} -> erro: {exc!r}")
        return {"ok": False, "error": str(exc)}


async def ask_assistant(thread_id: str, user_message: str, phone: str) -> Dict[str, Any]:
    """
    Publica a mensagem, roda o Assistant e EXECUTA tools quando requisitado.
    Retorno:
      {"text": <str|None>, "did_tool": <bool>}
    """
    out_text: Optional[str] = None
    did_tool = False

    async with httpx.AsyncClient(timeout=60.0) as client:
        # 1) user message
        try:
            r = await client.post(
                f"{_BASE_URL}/threads/{thread_id}/messages",
                headers=_headers_assistants(),
                json={"role": "user", "content": [{"type": "text", "text": user_message}]},
            )
            if r.status_code >= 400:
                # fallback para conteúdo simples
                r2 = await client.post(
                    f"{_BASE_URL}/threads/{thread_id}/messages",
                    headers=_headers_assistants(),
                    json={"role": "user", "content": user_message},
                )
                r2.raise_for_status()
        except Exception as exc:
            print(f"[openai] erro ao postar mensagem do usuário: {exc}")

        # 2) cria run (assistant_id, se falhar usa model)
        run_id: Optional[str] = None
        try:
            run_resp = await client.post(
                f"{_BASE_URL}/threads/{thread_id}/runs",
                headers=_headers_assistants(),
                json={"assistant_id": ASSISTANT_ID},
            )
            if run_resp.status_code >= 400:
                print(f"[openai] run assistant_id falhou: {run_resp.status_code} body={run_resp.text}")
                run_resp = await client.post(
                    f"{_BASE_URL}/threads/{thread_id}/runs",
                    headers=_headers_assistants(),
                    json={"model": OPENAI_MODEL},
                )
            run_resp.raise_for_status()
            run_id = run_resp.json().get("id")
        except Exception as exc:
            print(f"[openai] erro ao criar run: {exc}")

        if not run_id:
            # fallback final: chat completions
            out_text = await _chat_fallback(user_message)
            return {"text": out_text, "did_tool": did_tool}

        # 3) loop de polling + execução de tools
        for _ in range(90):
            st = await client.get(
                f"{_BASE_URL}/threads/{thread_id}/runs/{run_id}",
                headers=_headers_assistants(),
            )
            st.raise_for_status()
            jd = st.json()
            status = jd.get("status")

            if status == "completed":
                break

            if status == "requires_action":
                # executar tools
                tool_calls: List[Dict[str, Any]] = (jd.get("required_action", {})
                                                      .get("submit_tool_outputs", {})
                                                      .get("tool_calls", []))
                outputs = []
                for tc in tool_calls:
                    fn = tc.get("function", {}) or {}
                    name = fn.get("name") or ""
                    args_raw = fn.get("arguments") or "{}"
                    try:
                        args = json.loads(args_raw)
                    except Exception:
                        args = {}
                    res = await _execute_tool_call(name, args, phone)
                    outputs.append({"tool_call_id": tc.get("id"), "output": json.dumps(res, ensure_ascii=False)})
                    if res.get("ok"):
                        did_tool = True

                # submete resultados das tools
                sub = await client.post(
                    f"{_BASE_URL}/threads/{thread_id}/runs/{run_id}/submit_tool_outputs",
                    headers=_headers_assistants(),
                    json={"tool_outputs": outputs},
                )
                if sub.status_code >= 400:
                    print(f"[openai] submit_tool_outputs falhou: {sub.status_code} body={sub.text}")
                    break  # evita loop infinito
            elif status in {"failed", "expired", "cancelled"}:
                print(f"[openai] run terminou com status={status} details={jd}")
                out_text = await _chat_fallback(user_message)
                return {"text": out_text, "did_tool": did_tool}
            else:
                # queued, in_progress, etc.
                await asyncio.sleep(1.0)
                continue

            await asyncio.sleep(0.8)
        else:
            print("[openai] run não concluiu no tempo; usando fallback.")
            out_text = await _chat_fallback(user_message)
            return {"text": out_text, "did_tool": did_tool}

        # 4) pega mensagens finais
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
            return {"text": None, "did_tool": did_tool}

        for m in data:
            if m.get("role") == "assistant":
                contents = m.get("content", [])
                if isinstance(contents, list):
                    for c in contents:
                        if c.get("type") == "text":
                            txt = (c.get("text") or {}).get("value")
                            if txt:
                                out_text = txt
                                break
                elif isinstance(contents, str):
                    out_text = contents
                if out_text:
                    break

        return {"text": out_text, "did_tool": did_tool}