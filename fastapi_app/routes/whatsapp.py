"""
Webhook and API endpoints for WhatsApp interactions.

This module defines the ``/webhook/whatsapp`` endpoint which is
invoked by Uazapi when new WhatsApp messages arrive.  The endpoint
extracts relevant information from the payload, persists incoming
messages, forwards text to the OpenAI assistant, and sends the
assistant's response back via Uazapi.  It also exposes a simple
health check on ``/health``.  Additional endpoints can be added to
this file as needed (for example, to expose manual send capabilities).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models.db_models import Message, User
from ..services.openai_service import get_or_create_thread, ask_assistant
from ..services.uazapi_service import send_message, upload_file_to_baserow

router = APIRouter()


async def _get_or_create_user(db: AsyncSession, phone: str, name: Optional[str]) -> User:
    """Retrieve or create a ``User`` record by phone number.

    The phone number uniquely identifies a user.  If a user record
    does not yet exist it will be created with the provided name.
    Otherwise, if the name has changed it will be updated.  The
    caller is responsible for committing the transaction.
    """
    result = await db.execute(select(User).where(User.phone == phone))
    user: Optional[User] = result.scalars().first()
    if user is None:
        user = User(phone=phone, name=name)
        db.add(user)
        await db.commit()
        await db.refresh(user)
    else:
        if name and user.name != name:
            user.name = name
            await db.commit()
    return user


@router.post("/webhook/whatsapp")
async def whatsapp_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Handle incoming WhatsApp messages from Uazapi.

    This endpoint expects a JSON payload containing information about
    the message.  It supports text and various media types.  The
    specific structure of the incoming payload depends on how your
    Uazapi instance is configured.  The implementation below follows
    the common structure returned by the ``messages.upsert`` event
    described in the Uazapi documentation.  If your payload differs
    you may need to adjust the extraction logic.
    """
    try:
        payload = await request.json()
    except Exception:
        logging.exception("Failed to parse JSON from webhook request")
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    # Safely extract the message list.  Uazapi wraps messages
    # underneath ``data.data.messages``.  We guard accesses with
    # ``get`` to avoid KeyError if fields are missing.
    messages: List[Dict[str, Any]] = (
        payload.get("data", {}).get("data", {}).get("messages", [])
    )
    if not messages:
        # No messages to process; return OK to avoid retries
        return {"status": "no-messages"}
    # Only process the first message for now.  If multiple messages
    # arrive at once (e.g. a batch) you may iterate over them.
    message = messages[0]
    key: Dict[str, Any] = message.get("key", {})
    remote_jid: Optional[str] = key.get("remoteJid")
    if not remote_jid:
        raise HTTPException(status_code=400, detail="Missing remoteJid in payload")
    # The remote JID has the form ``<phone>@s.whatsapp.net``.  We
    # strip the domain to get the phone number.
    phone = remote_jid.split("@")[0]
    # Extract the sender's display name if provided
    name = message.get("pushName")
    # Create or fetch the user record
    user = await _get_or_create_user(db, phone, name)
    # Determine the message type and its content
    message_content: Dict[str, Any] = message.get("message", {})
    user_text: str = ""
    media_type: str = "text"
    media_url: Optional[str] = None
    # Check for each supported message type.  Uazapi uses keys like
    # ``textMessage``, ``imageMessage``, etc., nested under
    # ``message``.  We'll set ``user_text`` to a placeholder for
    # media messages.
    if "textMessage" in message_content:
        text_msg = message_content["textMessage"]
        # The ``textMessage`` could be a plain string or a dict
        if isinstance(text_msg, str):
            user_text = text_msg
        elif isinstance(text_msg, dict):
            user_text = text_msg.get("text", "")
        media_type = "text"
    elif "imageMessage" in message_content:
        media = message_content["imageMessage"]
        media_type = "image"
        media_url = media.get("url")
        user_text = "[image]"
    elif "videoMessage" in message_content:
        media = message_content["videoMessage"]
        media_type = "video"
        media_url = media.get("url")
        user_text = "[video]"
    elif "audioMessage" in message_content:
        media = message_content["audioMessage"]
        media_type = "audio"
        media_url = media.get("url")
        user_text = "[audio]"
    elif "documentMessage" in message_content:
        media = message_content["documentMessage"]
        media_type = "document"
        media_url = media.get("url")
        user_text = "[document]"
    elif "contactsMessage" in message_content:
        media_type = "vcard"
        user_text = "[vcard]"
    else:
        # Unknown or unsupported message type – store the raw message
        user_text = "[unsupported message type]"
        media_type = "unknown"
    # Persist the incoming message to the database
    incoming = Message(
        user_id=user.id,
        sender="user",
        content=user_text,
        media_type=media_type,
        media_url=media_url,
    )
    db.add(incoming)
    await db.commit()
    # If a media file is attached, offload it to Baserow in the background
    if media_url and media_type not in ("text", "unknown"):
        background_tasks.add_task(upload_file_to_baserow, media_url)
    # Obtain or create the OpenAI conversation thread
    thread_id = await get_or_create_thread(db, user)
    # Query the assistant for a reply
    reply_text = await ask_assistant(thread_id, user_text)
    if not reply_text:
        reply_text = "Desculpe, não consegui processar sua mensagem."
    # Persist the assistant's message
    outgoing = Message(
        user_id=user.id,
        sender="assistant",
        content=reply_text,
        media_type="text",
    )
    db.add(outgoing)
    await db.commit()
    # Send the reply back to the user via Uazapi asynchronously
    background_tasks.add_task(send_message, phone=phone, text=reply_text)
    return {"status": "processed"}