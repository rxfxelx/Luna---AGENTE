"""
Service layer exports.

Este módulo reexporta helpers para que outros módulos importem
de fastapi_app.services sem referenciar submódulos diretamente.
"""

from .uazapi_service import (
    send_whatsapp_message,
    send_message,
    send_menu,                 # <— novo
    upload_file_to_baserow,
    # expoe LUNA_* quando necessário em camadas superiores
    LUNA_MENU_YES, LUNA_MENU_NO,
    LUNA_VIDEO_URL, LUNA_VIDEO_CAPTION, LUNA_VIDEO_AFTER_TEXT, LUNA_END_TEXT,
)  # noqa: F401

from .openai_service import get_or_create_thread, ask_assistant  # noqa: F401

__all__ = [
    "send_whatsapp_message",
    "send_message",
    "send_menu",
    "upload_file_to_baserow",
    "get_or_create_thread",
    "ask_assistant",
    "LUNA_MENU_YES",
    "LUNA_MENU_NO",
    "LUNA_VIDEO_URL",
    "LUNA_VIDEO_CAPTION",
    "LUNA_VIDEO_AFTER_TEXT",
    "LUNA_END_TEXT",
]