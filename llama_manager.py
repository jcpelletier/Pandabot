"""
Llama / Ollama health check for local model inference.

Ollama manages GPU automatically — no mode switching needed. This module
provides a startup health check and a no-op ensure_gpu_mode() so bot.py
can call it unconditionally regardless of which backend is in use.

Env vars
--------
  LLAMA_PORT   Port Ollama listens on (default 11434)
  LLAMA_HOST   Host Ollama listens on (default 127.0.0.1)
"""

import logging
import os
import urllib.request

log = logging.getLogger("panda-bot.llama")

LLAMA_PORT = int(os.environ.get("LLAMA_PORT", "11434"))
LLAMA_HOST = os.environ.get("LLAMA_HOST", "127.0.0.1")

_HEALTH_URL = f"http://{LLAMA_HOST}:{LLAMA_PORT}/"


def _check_health() -> bool:
    try:
        with urllib.request.urlopen(_HEALTH_URL, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def current_mode() -> str:
    """Ollama always uses GPU — returns 'gpu-full' when healthy, 'unknown' otherwise."""
    return "gpu-full" if _check_health() else "unknown"


async def ensure_gpu_mode() -> bool:
    """No-op — Ollama handles GPU automatically. Returns True when Ollama is healthy."""
    healthy = _check_health()
    if not healthy:
        log.warning("llama_manager: Ollama health check failed")
    return healthy


def init() -> None:
    """Log Ollama health status at startup."""
    if _check_health():
        log.info("llama_manager: Ollama is healthy at %s", _HEALTH_URL)
    else:
        log.warning("llama_manager: Ollama not responding at %s — local LLM unavailable", _HEALTH_URL)
