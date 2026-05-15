"""
Text-to-speech client for the Kokoro TTS Docker container.

Endpoint: POST {TTS_URL}/v1/audio/speech
Body: {"model": "kokoro", "input": text, "voice": TTS_VOICE, "response_format": "mp3"}
Response: raw MP3 bytes
"""

from __future__ import annotations

import logging
import os
import re

import aiohttp

logger = logging.getLogger(__name__)

TTS_URL = os.environ.get("TTS_URL", "http://localhost:8880")
TTS_VOICE = os.environ.get("TTS_VOICE", "af_heart")

# Module-level reference to the shared aiohttp session; set by main.py at startup.
_http_session: aiohttp.ClientSession | None = None


def set_session(session: aiohttp.ClientSession) -> None:
    global _http_session
    _http_session = session


def get_session() -> aiohttp.ClientSession:
    if _http_session is None:
        raise RuntimeError("aiohttp ClientSession has not been initialised. Call set_session() first.")
    return _http_session


def _strip_markdown(text: str) -> str:
    """Remove markdown formatting so Kokoro doesn't read symbols aloud."""
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)   # **bold**
    text = re.sub(r'\*(.+?)\*', r'\1', text)         # *italic*
    text = re.sub(r'#{1,6}\s*', '', text)             # ## headers
    text = re.sub(r'`{1,3}[^`]*`{1,3}', '', text)    # `code`
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)  # [links](url)
    text = re.sub(r'^\s*[-*]\s+', '', text, flags=re.MULTILINE)  # bullet points
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


async def synthesize(text: str, client_session: aiohttp.ClientSession | None = None) -> bytes | None:
    """
    Synthesize speech from text using the Kokoro TTS service.

    Args:
        text: The text to speak.
        client_session: Optional aiohttp session override; defaults to the module-level session.

    Returns:
        Raw MP3 bytes, or None on error.
    """
    session = client_session or get_session()
    url = f"{TTS_URL}/v1/audio/speech"
    clean_text = _strip_markdown(text)

    payload = {
        "model": "kokoro",
        "input": clean_text,
        "voice": TTS_VOICE,
        "response_format": "mp3",
    }

    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with session.post(url, json=payload, timeout=timeout) as resp:
            if resp.status != 200:
                logger.error("TTS returned HTTP %d", resp.status)
                return None
            mp3_bytes = await resp.read()
    except Exception:
        logger.exception("Error calling TTS service at %s", url)
        return None

    if not mp3_bytes:
        logger.error("TTS returned empty audio bytes")
        return None

    logger.debug("TTS synthesized %d bytes of MP3", len(mp3_bytes))
    return mp3_bytes
