"""
Speech-to-text using faster_whisper directly (same model cache as bot.py).

Runs in a thread executor so it doesn't block the asyncio event loop.
Model is loaded once on first call and reused for subsequent requests.

Environment variables:
    STT_MODEL          — Whisper model size (default: small)
    STT_BEAM_SIZE      — Beam search width; 1 = greedy decode (default: 1)
    STT_DEVICE         — 'cuda' or 'cpu' (default: cpu)
    STT_COMPUTE_TYPE   — Quantisation; float16 for cuda (Pascal+), float32 for cuda
                          on Maxwell (GTX 9xx), int8 for cpu (default: int8)
    STT_GPU_MIN_FREE_MB — Minimum free VRAM (MB) required to keep the GPU model
                          resident (default: 1000). When a Steam game is active it
                          typically consumes 2-3 GB of the GTX 970's 4 GB, leaving
                          < 1000 MB free. In that case STT both falls back to a
                          lazy-loaded CPU model for the current turn AND unloads
                          the resident cuda model to free its ~1.8 GB so the game
                          has the full card. Set to 0 to disable the check and
                          always keep GPU resident when STT_DEVICE=cuda.
    STT_GPU_RELOAD_MIN_FREE_MB — Free VRAM (MB) required to reload the cuda model
                          after it has been unloaded (default: 3000). Must be high
                          enough that loading Whisper (~1.8 GB on Maxwell float32)
                          still leaves more than STT_GPU_MIN_FREE_MB free, otherwise
                          we'd thrash unload/reload every turn.
    STT_VRAM_POLL_INTERVAL_SECS — How often (seconds) the background VRAM poller
                          checks GPU memory to proactively evict or reload the cuda
                          model without waiting for a voice turn (default: 60).
                          Set to 0 to disable the poller entirely.
    STT_KOKORO_CONTAINER  — Name of the Kokoro TTS Docker container to stop/start
                          alongside the Whisper model when VRAM pressure is detected
                          (default: "kokoro"). Set to "" to disable Kokoro management.
                          The process user must be in the docker group.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import os
import subprocess
import threading

logger = logging.getLogger(__name__)

STT_MODEL                    = os.environ.get("STT_MODEL",                    "small")
STT_BEAM_SIZE                = int(os.environ.get("STT_BEAM_SIZE",            "1"))
STT_DEVICE                   = os.environ.get("STT_DEVICE",                   "cpu")
STT_COMPUTE_TYPE             = os.environ.get("STT_COMPUTE_TYPE",             "int8")
STT_GPU_MIN_FREE_MB          = int(os.environ.get("STT_GPU_MIN_FREE_MB",          "1000"))
STT_GPU_RELOAD_MIN_FREE_MB   = int(os.environ.get("STT_GPU_RELOAD_MIN_FREE_MB",   "3000"))
STT_VRAM_POLL_INTERVAL_SECS  = int(os.environ.get("STT_VRAM_POLL_INTERVAL_SECS",  "60"))
STT_KOKORO_CONTAINER         = os.environ.get("STT_KOKORO_CONTAINER",              "kokoro")

_WHISPER_CACHE = "/opt/discord-bot/models"

# Primary model — loaded on STT_DEVICE (cuda or cpu).
# CPU fallback model — lazy-loaded the first time gaming causes a GPU offload.
# _was_evicted distinguishes "_model_primary is None because we never loaded it"
# (fresh boot — try to load on cuda) from "we deliberately unloaded it under VRAM
# pressure" (stay on CPU until VRAM recovers past the reload threshold).
_model_primary:   object | None = None
_model_cpu:       object | None = None
_was_evicted:     bool = False
_kokoro_stopped:  bool = False
_model_lock = threading.Lock()
_poller_stop  = threading.Event()


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


def _get_gpu_free_mb() -> int | None:
    """Return free VRAM in MiB via nvidia-smi, or None if the query fails."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode != 0:
            logger.warning("STT: nvidia-smi returned %d", result.returncode)
            return None
        return int(result.stdout.strip())
    except Exception:
        logger.warning("STT: could not query GPU memory")
        return None


def _unload_gpu_resident_model() -> None:
    """Free the resident cuda Whisper model so other processes (e.g. a Steam game)
    can reclaim its ~1.8 GB of VRAM. The CPU fallback handles inference until
    VRAM recovers past STT_GPU_RELOAD_MIN_FREE_MB and we reload on cuda."""
    global _model_primary, _was_evicted
    with _model_lock:
        if _model_primary is None or STT_DEVICE != "cuda":
            return
        logger.info("STT: unloading resident cuda Whisper model to free VRAM")
        _model_primary = None
        _was_evicted = True
        # ctranslate2 releases its CUDA allocations when the WhisperModel is GC'd.
        # An explicit collection makes the release synchronous so the next
        # nvidia-smi reading reflects it.
        gc.collect()


def _transcribe_sync(audio_path: str) -> str | None:
    global _was_evicted
    if STT_DEVICE != "cuda" or STT_GPU_MIN_FREE_MB <= 0:
        model = _get_primary_model()
    else:
        free_mb = _get_gpu_free_mb()
        if _model_primary is not None:
            # Resident — evict if VRAM has dropped below the floor (game launched).
            if free_mb is None or free_mb < STT_GPU_MIN_FREE_MB:
                logger.info(
                    "STT: GPU %s MB free (< %d) — evicting cuda model, using CPU",
                    free_mb if free_mb is not None else "?", STT_GPU_MIN_FREE_MB,
                )
                _unload_gpu_resident_model()
                model = _get_cpu_fallback_model()
            else:
                model = _get_primary_model()
        elif _was_evicted:
            # Previously unloaded — only reload once we have enough headroom that
            # loading the model still leaves > STT_GPU_MIN_FREE_MB free.
            if free_mb is not None and free_mb >= STT_GPU_RELOAD_MIN_FREE_MB:
                logger.info(
                    "STT: GPU has %d MB free (≥ %d) — reloading cuda model",
                    free_mb, STT_GPU_RELOAD_MIN_FREE_MB,
                )
                _was_evicted = False
                model = _get_primary_model()
            else:
                model = _get_cpu_fallback_model()
        else:
            # Fresh boot, never loaded — try cuda (load failure falls back internally).
            model = _get_primary_model()
    segments, _ = model.transcribe(audio_path, language="en", beam_size=STT_BEAM_SIZE)
    text = " ".join(seg.text for seg in segments).strip()
    return text or None


def _stop_kokoro() -> None:
    """Stop the Kokoro Docker container to reclaim its GPU VRAM for gaming."""
    global _kokoro_stopped
    if _kokoro_stopped or not STT_KOKORO_CONTAINER:
        return
    logger.info("STT poller: stopping Kokoro container %r to free VRAM", STT_KOKORO_CONTAINER)
    try:
        r = subprocess.run(
            ["docker", "stop", STT_KOKORO_CONTAINER],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            _kokoro_stopped = True
            logger.info("STT poller: Kokoro container stopped")
        else:
            logger.warning("STT poller: docker stop returned %d: %s", r.returncode, r.stderr.strip())
    except Exception:
        logger.exception("STT poller: failed to stop Kokoro container")


def _start_kokoro() -> None:
    """Start the Kokoro Docker container after VRAM recovers from a gaming session."""
    global _kokoro_stopped
    if not _kokoro_stopped or not STT_KOKORO_CONTAINER:
        return
    logger.info("STT poller: starting Kokoro container %r — VRAM recovered", STT_KOKORO_CONTAINER)
    try:
        r = subprocess.run(
            ["docker", "start", STT_KOKORO_CONTAINER],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            _kokoro_stopped = False
            logger.info("STT poller: Kokoro container started")
        else:
            logger.warning("STT poller: docker start returned %d: %s", r.returncode, r.stderr.strip())
    except Exception:
        logger.exception("STT poller: failed to start Kokoro container")


def _vram_poll_loop() -> None:
    """Background daemon thread: proactively free and restore GPU residents.

    On low VRAM (game launched): stops Kokoro container + unloads Whisper cuda model.
    On VRAM recovery (game exited): starts Kokoro container + reloads Whisper cuda model.
    Runs every STT_VRAM_POLL_INTERVAL_SECS seconds.
    """
    global _was_evicted
    logger.info("STT VRAM poller started (interval=%ds)", STT_VRAM_POLL_INTERVAL_SECS)
    while not _poller_stop.wait(STT_VRAM_POLL_INTERVAL_SECS):
        if STT_GPU_MIN_FREE_MB <= 0:
            continue
        free_mb = _get_gpu_free_mb()
        if free_mb is None:
            continue

        if free_mb < STT_GPU_MIN_FREE_MB:
            # VRAM low — evict all GPU residents
            if not _kokoro_stopped:
                logger.info("STT poller: GPU %d MB free (< %d) — evicting GPU residents", free_mb, STT_GPU_MIN_FREE_MB)
            _stop_kokoro()
            if STT_DEVICE == "cuda" and _model_primary is not None:
                _unload_gpu_resident_model()
        elif free_mb >= STT_GPU_RELOAD_MIN_FREE_MB:
            # VRAM recovered — restore GPU residents
            if _kokoro_stopped:
                logger.info("STT poller: GPU %d MB free (≥ %d) — restoring GPU residents", free_mb, STT_GPU_RELOAD_MIN_FREE_MB)
            _start_kokoro()
            if STT_DEVICE == "cuda" and _model_primary is None and _was_evicted:
                _was_evicted = False
                _get_primary_model()
    logger.info("STT VRAM poller stopped")


async def warm() -> None:
    """Pre-load the primary Whisper model and start the VRAM poller. Safe to call multiple times."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _get_primary_model)
    # Start poller if it manages anything: Kokoro container or cuda Whisper model.
    if STT_VRAM_POLL_INTERVAL_SECS > 0 and (STT_KOKORO_CONTAINER or STT_DEVICE == "cuda"):
        t = threading.Thread(target=_vram_poll_loop, name="stt-vram-poller", daemon=True)
        t.start()


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
