"""
Manage Kokoro TTS GPU/CPU mode.

GPU mode: model runs on GTX 970 (~2807 MiB VRAM, fast synthesis)
CPU mode: model runs on i7-4790K (no VRAM, adequate for infrequent TTS)

Switching restarts the Docker container via set-mode.sh and takes ~15-30 s.
The caller should speak a readiness phrase in voice after ensure_gpu_mode() returns.

Called by bot.py when users join/leave the TTS voice channel.
Sudoers entry required:
  discord-bot ALL=(root) NOPASSWD: /opt/kokoro/set-mode.sh
"""

import asyncio
import logging
import os
import subprocess
import urllib.request

log = logging.getLogger("panda-bot.kokoro")

KOKORO_SET_MODE_CMD = os.environ.get("KOKORO_SET_MODE_CMD", "/opt/kokoro/set-mode.sh")
KOKORO_URL = os.environ.get("TTS_URL", "http://localhost:8880")

_current_mode: str = "unknown"
_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def current_mode() -> str:
    return _current_mode


def _check_health() -> bool:
    try:
        urllib.request.urlopen(f"{KOKORO_URL}/health", timeout=2)
        return True
    except Exception:
        return False


def _do_mode_switch(mode: str) -> bool:
    try:
        result = subprocess.run(
            ["sudo", KOKORO_SET_MODE_CMD, mode],
            capture_output=True, text=True, timeout=90,
        )
        if result.returncode == 0:
            log.info("Kokoro switched to %s mode", mode)
            return True
        log.error("Kokoro set-mode.sh %s failed: %s", mode, result.stderr.strip())
        return False
    except Exception as exc:
        log.error("Kokoro mode switch error: %s", exc)
        return False


async def ensure_gpu_mode() -> bool:
    """Switch Kokoro to GPU mode. Idempotent. Returns True when healthy."""
    global _current_mode
    if _current_mode == "gpu" and _check_health():
        return True
    async with _get_lock():
        if _current_mode == "gpu" and _check_health():
            return True
        log.info("Switching Kokoro → GPU mode")
        loop = asyncio.get_running_loop()
        success = await loop.run_in_executor(None, _do_mode_switch, "gpu")
        if success:
            _current_mode = "gpu"
        return success


async def ensure_cpu_mode() -> None:
    """Switch Kokoro to CPU mode. Idempotent."""
    global _current_mode
    if _current_mode == "cpu":
        return
    async with _get_lock():
        if _current_mode == "cpu":
            return
        log.info("Switching Kokoro → CPU mode")
        loop = asyncio.get_running_loop()
        success = await loop.run_in_executor(None, _do_mode_switch, "cpu")
        if success:
            _current_mode = "cpu"
