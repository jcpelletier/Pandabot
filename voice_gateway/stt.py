"""
Speech-to-text using faster_whisper directly (same model cache as bot.py).

Runs in a thread executor so it doesn't block the asyncio event loop.
Model is loaded once on first call and reused for subsequent requests.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading

logger = logging.getLogger(__name__)

STT_MODEL = os.environ.get("STT_MODEL", "medium")
_WHISPER_CACHE = "/opt/discord-bot/models"

_whisper_model = None
_whisper_lock = threading.Lock()


def _get_model():
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            from faster_whisper import WhisperModel  # type: ignore[import]
            logger.info("Loading Whisper model %r (CPU int8)...", STT_MODEL)
            _whisper_model = WhisperModel(
                STT_MODEL,
                device="cpu",
                compute_type="int8",
                download_root=_WHISPER_CACHE,
            )
            logger.info("Whisper model ready")
    return _whisper_model


def _transcribe_sync(audio_path: str) -> str | None:
    model = _get_model()
    segments, _ = model.transcribe(audio_path, language="en", beam_size=5)
    text = " ".join(seg.text for seg in segments).strip()
    return text or None


async def transcribe(audio_path: str, _session=None) -> str | None:
    """Transcribe audio_path; returns stripped text or None on empty/error."""
    try:
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(None, _transcribe_sync, audio_path)
        if text:
            logger.info("STT transcribed: %r", text)
        else:
            logger.debug("STT returned empty transcription")
        return text
    except Exception:
        logger.exception("STT error for %s", audio_path)
        return None


# Kept for compatibility — no longer needed but harmless to call
def set_session(session) -> None:  # noqa: ANN001
    pass
