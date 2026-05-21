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
import subprocess

import aiohttp

logger = logging.getLogger(__name__)

TTS_URL = os.environ.get("TTS_URL", "http://localhost:8880")
TTS_VOICE = os.environ.get("TTS_VOICE", "am_santa")

# Module-level reference to the shared aiohttp session; set by main.py at startup.
_http_session: aiohttp.ClientSession | None = None

# Cached 300ms silent MP3 prepended to every TTS response so the client's
# AudioTrack has time to warm up before the actual audio begins — without this,
# Android devices clip the first ~250ms ("Bob is 10" -> "ob is 10").
_silent_prefix_cache: bytes | None = None


def _silent_prefix() -> bytes:
    global _silent_prefix_cache
    if _silent_prefix_cache is not None:
        return _silent_prefix_cache
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner", "-loglevel", "error",
                "-f", "lavfi",
                "-i", "anullsrc=r=24000:cl=mono",
                "-t", "0.3",
                "-c:a", "libmp3lame",
                "-b:a", "64k",
                "-f", "mp3", "-",
            ],
            capture_output=True, timeout=10, check=True,
        )
        _silent_prefix_cache = result.stdout
        logger.info("Generated %d-byte silent MP3 prefix for client warmup", len(_silent_prefix_cache))
    except Exception:
        logger.exception("Failed to generate silent MP3 prefix — first ~250ms of responses may clip")
        _silent_prefix_cache = b""
    return _silent_prefix_cache


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


async def list_voices(client_session: aiohttp.ClientSession | None = None) -> dict | None:
    """Fetch the Kokoro voice catalog. Returns the parsed JSON or None on error."""
    session = client_session or get_session()
    url = f"{TTS_URL}/v1/audio/voices"
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with session.get(url, timeout=timeout) as resp:
            if resp.status != 200:
                logger.error("Kokoro /voices returned HTTP %d", resp.status)
                return None
            return await resp.json()
    except Exception:
        logger.exception("Error fetching voice catalog from %s", url)
        return None


async def synthesize(
    text: str,
    client_session: aiohttp.ClientSession | None = None,
    voice: str | None = None,
) -> bytes | None:
    """
    Synthesize speech from text using the Kokoro TTS service.

    Args:
        text: The text to speak.
        client_session: Optional aiohttp session override; defaults to the module-level session.
        voice: Optional Kokoro voice override (e.g. "af_bella"). Falls back to TTS_VOICE
            env default when None/empty.

    Returns:
        Raw MP3 bytes, or None on error.
    """
    session = client_session or get_session()
    url = f"{TTS_URL}/v1/audio/speech"
    clean_text = _strip_markdown(text)

    payload = {
        "model": "kokoro",
        "input": clean_text,
        "voice": (voice or "").strip() or TTS_VOICE,
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
    return _silent_prefix() + mp3_bytes
