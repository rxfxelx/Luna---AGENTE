"""
OpenAI Assistants API integration helpers.

This module provides convenience functions to create or reuse
conversation threads and to submit messages to the configured
Assistant.  It uses HTTP calls via ``httpx`` rather than the OpenAI
Python SDK to avoid additional dependencies and to retain control
over polling behaviour.

Usage example::

    from .openai_service import get_or_create_thread, ask_assistant
    thread_id = await get_or_create_thread(db_session, user)
    reply = await ask_assistant(thread_id, "Hello!")

The assistant's final textual response is returned, or ``None`` if
something went wrong.
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.db_models import User

# Read OpenAI credentials and Assistant ID from the environment.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID = os.getenv("ASSISTANT_ID")

if not OPENAI_API_KEY:
    raise RuntimeError(
        "OPENAI_API_KEY environment variable must be set to call the OpenAI API."
    )
if not ASSISTANT_ID:
    raise RuntimeError(
        "ASSISTANT_ID environment variable must be set to identify the assistant." 
    )


async def get_or_create_thread(session: AsyncSession, user: User) -> str:
    """Return the OpenAI thread ID associated with a user.

    If the user doesn't already have a thread this function creates
    one by calling the OpenAI API and stores it on the user record.

    Parameters
    ----------
    session:
        A SQLAlchemy async session used to persist the user's
        thread_id once created.
    user:
        The user object whose thread ID should be retrieved or
        initialised.

    Returns
    -------
    str
        The thread ID as returned by the OpenAI API.
    """
    if user.thread_id:
        return user.thread_id
    # Create a new thread for this assistant and user
    url = f"https://api.openai.com/v1/assistants/{ASSISTANT_ID}/threads"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, json={})
        resp.raise_for_status()
        data = resp.json()
        thread_id = data.get("id")
    # Persist the thread ID on the user record
    user.thread_id = thread_id
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return thread_id


async def ask_assistant(thread_id: str, user_message: str) -> Optional[str]:
    """Send a message to an OpenAI Assistant and return its reply.

    The function posts the user's message to the specified thread,
    initiates a run and then polls until the assistant responds.  It
    extracts textual content from the assistant's message.  If an
    error occurs or the assistant does not return any text content a
    ``None`` value is returned.

    Parameters
    ----------
    thread_id:
        Identifier of the conversation thread.  The assistant uses
        this to maintain context across messages.
    user_message:
        The message text sent by the user.  Use placeholder text for
        media messages such as "[image]" or a transcription for audio.

    Returns
    -------
    Optional[str]
        The assistant's textual response, or ``None`` if none was
        received.
    """
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient() as client:
        # Step 1: append the user's message to the thread
        try:
            await client.post(
                f"https://api.openai.com/v1/threads/{thread_id}/messages",
                headers=headers,
                json={"role": "user", "content": user_message},
            )
        except Exception as exc:
            print(f"Error posting user message to thread: {exc}")
            return None
        # Step 2: create a run for the assistant
        try:
            run_resp = await client.post(
                f"https://api.openai.com/v1/threads/{thread_id}/runs",
                headers=headers,
                json={"assistant_id": ASSISTANT_ID},
            )
            run_resp.raise_for_status()
            run_data = run_resp.json()
            run_id = run_data.get("id")
        except Exception as exc:
            print(f"Error creating run: {exc}")
            return None
        # Step 3: poll run status until completed or timed out
        for _ in range(30):  # up to ~60 seconds if we sleep 2 seconds each
            try:
                status_resp = await client.get(
                    f"https://api.openai.com/v1/threads/{thread_id}/runs/{run_id}",
                    headers=headers,
                )
                status_data = status_resp.json()
                if status_data.get("status") == "completed":
                    break
            except Exception as exc:
                print(f"Error polling run status: {exc}")
                return None
            await asyncio.sleep(2)
        else:
            print("Assistant run did not complete in time.")
            return None
        # Step 4: fetch messages from the thread and find the latest assistant response
        try:
            msgs_resp = await client.get(
                f"https://api.openai.com/v1/threads/{thread_id}/messages",
                headers=headers,
            )
            msgs_resp.raise_for_status()
            messages = msgs_resp.json().get("data", [])
        except Exception as exc:
            print(f"Error fetching messages: {exc}")
            return None
        # Find the last assistant message in reverse order
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                content = msg.get("content")
                # The content field can be a list of blocks or a simple string
                if isinstance(content, list):
                    text_parts: list[str] = []
                    for block in content:
                        if block.get("type") == "text":
                            text = block.get("text", {}).get("value")
                            if text:
                                text_parts.append(text)
                    return "\n".join(text_parts) if text_parts else None
                elif isinstance(content, str):
                    return content
        return None