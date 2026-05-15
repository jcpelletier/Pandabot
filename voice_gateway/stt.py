"""
Speech-to-text client for the Whisper Docker container.

Endpoint: POST {STT_URL}/v1/audio/transcriptions
Format: multipart/form-data — file, model, language
Response: {"text": "..."}
"""

from __future__ import annotations

import logging
import os

import aiohttp

logger = logging.getLogger(__name__)

STT_URL = os.environ.get("STT_URL", "http://localhost:8001")
STT_MODEL = "Systran/faster-whisper-medium"

# Module-level reference to the shared aiohttp session; set by main.py at startup.
_http_session: aiohttp.ClientSession | None = None


def set_session(session: aiohttp.ClientSession) -> None:
    global _http_session
    _http_session = session


def get_session() -> aiohttp.ClientSession:
    if _http_session is None:
        raise RuntimeError("aiohttp ClientSession has not been initialised. Call set_session() first.")
    return _http_session


async def transcribe(audio_path: str, client_session: aiohttp.ClientSession | None = None) -> str | None:
    """
    Transcribe an audio file using the Whisper STT service.

    Args:
        audio_path: Local path to the audio file.
        client_session: Optional aiohttp session override; defaults to the module-level session.

    Returns:
        Transcribed text stripped of leading/trailing whitespace, or None on empty/error.
    """
    session = client_session or get_session()
    url = f"{STT_URL}/v1/audio/transcriptions"

    try:
        with open(audio_path, "rb") as f:
            form = aiohttp.FormData()
            form.add_field("file", f, filename=os.path.basename(audio_path))
            form.add_field("model", STT_MODEL)
            form.add_field("language", "en")

            async with session.post(url, data=form) as resp:
                if resp.status != 200:
                    logger.error("STT returned HTTP %d", resp.status)
                    return None
                data = await resp.json()
    except Exception:
        logger.exception("Error calling STT service at %s", url)
        return None

    text = data.get("text", "").strip()
    if not text:
        logger.debug("STT returned empty transcription")
        return None

    logger.info("STT transcribed: %r", text)
    return text
