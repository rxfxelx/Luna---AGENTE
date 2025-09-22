"""
ORM model definitions for the Luna backend.

This module defines the ``User`` and ``Message`` models used to
persist chat history and user data in a relational database.  The
models use SQLAlchemy's declarative syntax and are designed to be
loaded asynchronously via the engine configured in
``fastapi_app.db``.
"""

from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import relationship

from ..db import Base


class User(Base):
    """Represents a WhatsApp user interacting with the Luna assistant."""

    __tablename__ = "users"

    id: int = Column(Integer, primary_key=True, index=True)
    # The WhatsApp phone number (or jid without the domain) is stored
    # here.  It must be unique so that each user corresponds to a
    # single conversation thread.
    phone: str = Column(String(30), unique=True, nullable=False)
    # Optional display name extracted from incoming messages.  This
    # value can change over time as contacts update their names.
    name: str | None = Column(String(255))
    # ID of the OpenAI Assistant thread associated with this user.
    thread_id: str | None = Column(String(255))
    # Timestamp when the user record was created.
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationship back to messages sent by or to this user.
    messages = relationship("Message", back_populates="user", cascade="all, delete-orphan")


class Message(Base):
    """Represents a single message exchanged between the user and assistant."""

    __tablename__ = "messages"

    id: int = Column(Integer, primary_key=True, index=True)
    # Foreign key linking to the owning user.  The ``ondelete="CASCADE"``
    # ensures that when a user is removed all of their messages are
    # automatically deleted as well.
    user_id: int = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # Indicates whether the message was sent by the user or the assistant.
    sender: str = Column(String(10), nullable=False)
    # Plain‑text content of the message, if applicable.  For media
    # messages this may contain a placeholder description such as
    # "[image]" or a transcription of an audio file.
    content: str | None = Column(Text)
    # Type of the media attached to the message: ``text``, ``image``,
    # ``audio``, ``video``, ``document`` or ``vcard``.  Unknown types
    # are stored as ``unknown``.
    media_type: str | None = Column(String(20))
    # For media messages this field stores a URL or identifier that
    # references where the file can be retrieved.  It could point
    # directly to the Uazapi file URL or a record in Baserow after
    # uploading.
    media_url: str | None = Column(Text)
    # Timestamp of when the message was created.  The server's current
    # time is used as the default value.
    timestamp = Column(DateTime(timezone=True), server_default=func.now())

    # Relationship back to the owning user record.
    user = relationship("User", back_populates="messages")