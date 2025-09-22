"""
SQLAlchemy ORM models used by the application.

The models package exposes individual model modules under a
convenient namespace.  Importing :mod:`fastapi_app.models` will
register all models with SQLAlchemy's declarative base so that the
table metadata becomes available when creating the schema.
"""

from .db_models import Message, User  # noqa: F401