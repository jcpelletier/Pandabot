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


_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"  # symbols & pictographs, emoticons, transport, supplemental
    "\U00002600-\U000027BF"  # misc symbols & dingbats
    "\U0001F1E6-\U0001F1FF"  # regional indicators (flags)
    "\U0000FE0F"             # variation selector-16
    "\U0000200D"             # zero-width joiner
    "]+",
    flags=re.UNICODE,
)


def _strip_markdown(text: str) -> str:
    """Strip markdown + emojis so Kokoro speaks clean prose."""
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'#{1,6}\s*', '', text)
    text = re.sub(r'`{1,3}[^`]*`{1,3}', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    text = re.sub(r'^\s*[-*]\s+', '', text, flags=re.MULTILINE)
    text = _EMOJI_RE.sub('', text)
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
