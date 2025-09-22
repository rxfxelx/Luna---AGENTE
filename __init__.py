"""
API route modules for the Luna backend.

Routes are organised in their own subpackage to keep the FastAPI
application modular.  Each file within this package should export a
router instance that can be included in the main application.
"""

from .whatsapp import router as whatsapp  # noqa: F401
