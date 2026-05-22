"""
Speech-to-text using faster_whisper directly (same model cache as bot.py).

Runs in a thread executor so it doesn't block the asyncio event loop.
Model is loaded once on first call and reused for subsequent requests.

Environment variables:
    STT_MODEL          — Whisper model size (default: small)
    STT_BEAM_SIZE      — Beam search width; 1 = greedy decode (default: 1)
    STT_DEVICE         — 'cuda' or 'cpu' (default: cpu)
    STT_COMPUTE_TYPE   — Quantisation; float16 for cuda, int8 for cpu (default: int8)
    STT_GPU_MIN_FREE_MB — Minimum free VRAM (MB) required to use the GPU for a
                          transcription call (default: 1000). When a Steam game is
                          active it typically consumes 2-3 GB of the GTX 970's 4 GB,
                          leaving < 1000 MB free. In that case STT automatically falls
                          back to a lazy-loaded CPU model for that turn so the game
                          is not disrupted. Set to 0 to disable the check and always
                          use GPU when STT_DEVICE=cuda.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import threading

logger = logging.getLogger(__name__)

STT_MODEL           = os.environ.get("STT_MODEL",           "small")
STT_BEAM_SIZE       = int(os.environ.get("STT_BEAM_SIZE",   "1"))
STT_DEVICE          = os.environ.get("STT_DEVICE",          "cpu")
STT_COMPUTE_TYPE    = os.environ.get("STT_COMPUTE_TYPE",    "int8")
STT_GPU_MIN_FREE_MB = int(os.environ.get("STT_GPU_MIN_FREE_MB", "1000"))

_WHISPER_CACHE = "/opt/discord-bot/models"

# Primary model — loaded on STT_DEVICE (cuda or cpu).
# CPU fallback model — lazy-loaded the first time gaming causes a GPU offload.
_model_primary:  object | None = None
_model_cpu:      object | None = None
_model_lock = threading.Lock()


def _load_whisper(device: str, compute_type: str) -> object:
    from faster_whisper import WhisperModel  # type: ignore[import]
    ct = compute_type if device != "cpu" else "int8"  # cpu always uses int8
    logger.info("Loading Whisper %r on %s (%s)…", STT_MODEL, device, ct)
    model = WhisperModel(STT_MODEL, device=device, compute_type=ct, download_root=_WHISPER_CACHE)
    logger.info("Whisper model ready (%s %s)", device, ct)
    return model


def _get_primary_model() -> object:
    global _model_primary, STT_DEVICE
    if _model_primary is not None:
        return _model_primary
    with _model_lock:
        if _model_primary is None:
            try:
                _model_primary = _load_whisper(STT_DEVICE, STT_COMPUTE_TYPE)
            except Exception:
                logger.exception(
                    "Failed to load Whisper on %s; falling back to CPU int8", STT_DEVICE
                )
                STT_DEVICE = "cpu"
                _model_primary = _load_whisper("cpu", "int8")
    return _model_primary


def _get_cpu_fallback_model() -> object:
    """Lazy-load a CPU model used when GPU memory is too low (gaming active)."""
    global _model_cpu
    if _model_cpu is not None:
        return _model_cpu
    with _model_lock:
        if _model_cpu is None:
            logger.info("Lazy-loading CPU fallback Whisper model for gaming offload…")
            _model_cpu = _load_whisper("cpu", "int8")
    return _model_cpu


def _gpu_free_for_stt() -> bool:
    """Return True if the GPU has enough free VRAM for a Whisper inference pass.

    Runs nvidia-smi to check current free memory.  When a Steam game is active
    most of the GTX 970's 4 GB is allocated by the graphics driver; the free
    figure drops below STT_GPU_MIN_FREE_MB and this function returns False,
    triggering a CPU fallback for that turn only.  The GPU model stays loaded
    and is reused as soon as the game frees memory.
    """
    if STT_GPU_MIN_FREE_MB <= 0:
        return True  # check disabled
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode != 0:
            logger.warning("STT: nvidia-smi returned %d — using CPU fallback", result.returncode)
            return False
        free_mb = int(result.stdout.strip())
        if free_mb < STT_GPU_MIN_FREE_MB:
            logger.info(
                "STT: GPU only %d MB free (threshold %d MB) — gaming detected, using CPU fallback",
                free_mb, STT_GPU_MIN_FREE_MB,
            )
            return False
        return True
    except Exception:
        logger.warning("STT: could not query GPU memory — using CPU fallback")
        return False


def _transcribe_sync(audio_path: str) -> str | None:
    # When CUDA is configured, check GPU memory before each call.
    # If a game is consuming most of the VRAM, offload this turn to CPU.
    if STT_DEVICE == "cuda" and not _gpu_free_for_stt():
        model = _get_cpu_fallback_model()
    else:
        model = _get_primary_model()
    segments, _ = model.transcribe(audio_path, language="en", beam_size=STT_BEAM_SIZE)
    text = " ".join(seg.text for seg in segments).strip()
    return text or None


async def warm() -> None:
    """Pre-load the primary Whisper model in a background thread. Safe to call multiple times."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _get_primary_model)


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
