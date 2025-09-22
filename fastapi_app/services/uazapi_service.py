"""
Helper functions for interacting with the Uazapi WhatsApp API and optional
Baserow storage.

The Uazapi API is used to send text and media messages back to users.
It expects an API key passed either as a header or query parameter
depending on your configuration.  The specific endpoint paths may vary
between installations; by default this module assumes two standard
endpoints for sending text and media.  If your deployment uses
different paths you can override them by setting the environment
variables ``UAZAPI_SEND_TEXT_PATH`` and ``UAZAPI_SEND_MEDIA_PATH``.

This module also includes a helper to upload files to Baserow.  The
method downloads a file from a provided URL (typically from Uazapi
itself) and then uploads it to Baserow's user file endpoint.  You can
omit configuring Baserow variables if you don't need offloading media
files to external storage – in that case the upload will be skipped.
"""

from __future__ import annotations

import os
from typing import Optional

import httpx

# Uazapi configuration.  The base URL should point to your Uazapi
# instance (e.g. ``https://example.uazapi.com``).  The token is used
# for authentication.  Additional paths allow for customising the
# endpoints used for sending messages.
UAZAPI_BASE_URL = os.getenv("UAZAPI_BASE_URL", "")
UAZAPI_TOKEN = os.getenv("UAZAPI_TOKEN", "")
UAZAPI_SEND_TEXT_PATH = os.getenv("UAZAPI_SEND_TEXT_PATH", "/send/text")
UAZAPI_SEND_MEDIA_PATH = os.getenv("UAZAPI_SEND_MEDIA_PATH", "/send/media")

# Baserow configuration.  When provided, media received via Uazapi
# webhooks can be uploaded to Baserow for persistent storage.  The
# Baserow base URL should point to your installation (e.g.
# ``https://api.baserow.io``) and the token should be a valid API key.
BASEROW_BASE_URL = os.getenv("BASEROW_BASE_URL", "")
BASEROW_API_TOKEN = os.getenv("BASEROW_API_TOKEN", "")

async def send_message(*, phone: str, text: str, media_url: Optional[str] = None, mime_type: Optional[str] = None) -> None:
    """Send a message to a WhatsApp user via Uazapi.

    If ``media_url`` is provided the message will be sent as a media
    message with an optional caption.  Otherwise a simple text
    message is sent.  Errors during the call are silently logged to
    stdout – in a production system you may want to integrate with
    proper logging infrastructure.

    Parameters
    ----------
    phone:
        The destination WhatsApp number (without domain) to send the
        message to.
    text:
        The text content of the message or the caption for media
        messages.
    media_url:
        Optional URL to the media file to attach.  If provided the
        ``mime_type`` parameter should be set accordingly.
    mime_type:
        The MIME type of the media file (e.g. ``image/jpeg``) if
        sending media.  Ignored for text messages.
    """
    if not UAZAPI_BASE_URL or not UAZAPI_TOKEN:
        print("Uazapi configuration missing – cannot send message.")
        return
    # Determine which endpoint to call based on whether media is
    # attached.  The paths can be customised via environment
    # variables; they are joined to the base URL without adding
    # additional slashes.
    if media_url:
        endpoint = f"{UAZAPI_BASE_URL.rstrip('/')}{UAZAPI_SEND_MEDIA_PATH}"
        payload = {
            "chatId": phone,
            "fileUrl": media_url,
            "caption": text or "",
            "mimeType": mime_type or "application/octet-stream",
        }
    else:
        endpoint = f"{UAZAPI_BASE_URL.rstrip('/')}{UAZAPI_SEND_TEXT_PATH}"
        payload = {
            "chatId": phone,
            "text": text,
        }
    headers = {"apikey": UAZAPI_TOKEN}
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(endpoint, json=payload, headers=headers, timeout=30)
            resp.raise_for_status()
        except Exception as exc:
            # Print the error; in production you'd likely use a logger
            print(f"Error sending message via Uazapi: {exc}")


async def upload_file_to_baserow(media_url: str) -> Optional[dict]:
    """Upload a file referenced by ``media_url`` to Baserow user files.

    This helper is optional.  It first downloads the file from the
    provided URL then performs a multipart upload to Baserow.  When
    successful it returns the JSON response from Baserow which
    includes the file ID and URL.  If Baserow configuration is
    missing or an error occurs, ``None`` is returned and the error is
    printed to stdout.

    Parameters
    ----------
    media_url:
        The URL of the media file to download and upload.  Typically
        this will be provided in the Uazapi webhook payload.
    """
    if not BASEROW_BASE_URL or not BASEROW_API_TOKEN:
        # Nothing to do if Baserow isn't configured
        print("Baserow configuration missing – skipping file upload.")
        return None
    async with httpx.AsyncClient() as client:
        try:
            # Retrieve the file from the given URL
            file_resp = await client.get(media_url, timeout=60)
            file_resp.raise_for_status()
            file_bytes = file_resp.content
            # Determine a filename based on the URL path.  Strip
            # querystring parameters if present.
            filename = media_url.split("/")[-1].split("?")[0] or "file"
            files = {"file": (filename, file_bytes)}
            headers = {"Authorization": f"Token {BASEROW_API_TOKEN}"}
            upload_url = f"{BASEROW_BASE_URL.rstrip('/')}/api/userfiles/upload_file/"
            upload_resp = await client.post(upload_url, headers=headers, files=files, timeout=60)
            upload_resp.raise_for_status()
            return upload_resp.json()
        except Exception as exc:
            print(f"Error uploading file to Baserow: {exc}")
            return None