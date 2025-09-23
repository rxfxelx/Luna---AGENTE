"""
SQLAlchemy ORM models package.

Importing this package registers the models with SQLAlchemy's metadata.
"""

from .db_models import Base, Message, User  # noqa: F401

__all__ = ["Base", "User", "Message"]
