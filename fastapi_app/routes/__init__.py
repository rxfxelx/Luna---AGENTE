"""
API route modules for the Luna backend.

Exposes:
- get_whatsapp_router(): returns the WhatsApp webhook router
"""

from .whatsapp import get_router as get_whatsapp_router  # noqa: F401

__all__ = ["get_whatsapp_router"]
