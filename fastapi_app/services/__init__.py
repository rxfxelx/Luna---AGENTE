"""
Utility functions and classes for integrating with external services.

This package houses reusable helpers for talking to the Uazapi
(WhatsApp) API, the OpenAI Assistants API, and optionally for
uploading files to Baserow.  Keeping these concerns separated from
request handling makes the route definitions cleaner and easier to
understand.
"""

from .uazapi_service import send_message, upload_file_to_baserow  # noqa: F401
from .openai_service import get_or_create_thread, ask_assistant  # noqa: F401