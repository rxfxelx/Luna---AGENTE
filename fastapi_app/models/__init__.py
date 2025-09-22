"""
SQLAlchemy ORM models package.

Importing this package will register the models and expose User/Message/Base.
"""

from .db_models import Base, Message, User  # noqa: F401
