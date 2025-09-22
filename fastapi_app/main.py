"""
Entry point for the FastAPI application.

This module constructs the FastAPI app, registers startup events to
initialise the database schema (when needed), and includes all
available API routers.  Importing ``app`` from this module will
provide the configured application instance ready to be served by an
ASGI server such as Uvicorn.
"""

from __future__ import annotations

from fastapi import FastAPI

from .db import init_models
from .routes import whatsapp


def create_app() -> FastAPI:
    """Instantiate and configure the FastAPI application."""
    app = FastAPI(
        title="Luna Backend",
        description=(
            "An API backend for the Luna WhatsApp assistant, providing"
            " webhook handling and integration with OpenAI's Assistants API."
        ),
        version="0.1.0",
    )

    # Include routers.  You can mount additional routers here as your
    # application grows.
    app.include_router(whatsapp, prefix="")

    @app.on_event("startup")
    async def on_startup() -> None:
        # Create database tables if they don't exist.  In a real
        # deployment you should run migrations outside of the app
        # process, but for convenience this ensures the tables exist.
        await init_models()

    @app.get("/health", tags=["system"])
    async def healthcheck() -> dict[str, str]:
        """Simple healthcheck endpoint used for uptime monitoring."""
        return {"status": "ok"}

    return app


# Create a global application instance.  Uvicorn will import this
# ``app`` when starting the server.
app = create_app()