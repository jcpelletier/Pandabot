"""
Llama server mode manager — CPU vs GPU for local model inference.

The llama-server systemd service runs in CPU mode by default (no VRAM used).
When a request arrives for a local model (Gemma), ensure_gpu_mode() switches the
server to GPU mode for fast inference. The idle-check task in bot.py calls
ensure_cpu_mode() after LLAMA_IDLE_TIMEOUT_SECS of inactivity, freeing VRAM.

How the switch works
--------------------
Mode switching calls: sudo /opt/llama/set-mode.sh gpu|cpu
That script writes the new LLAMA_GPU_LAYERS value to /etc/llama-server-mode.env
and restarts the systemd service, then polls /health until the server is ready.

The model file is loaded via mmap (default llama.cpp behaviour). When the server
process exits, the mmap is released, but the OS page cache often retains the file
pages in RAM. A restart in GPU mode then only needs to copy those cached pages to
VRAM over PCIe — typically 2–4 s — rather than reading from disk (~10–15 s cold).

Sudoers requirement (one-time server setup):
  discord-bot ALL=(root) NOPASSWD: /opt/llama/set-mode.sh

Env vars
--------
  LLAMA_PORT              Port the server listens on (default 8081)
  LLAMA_HOST              Host the server listens on (default 127.0.0.1)
  LLAMA_IDLE_TIMEOUT_SECS Seconds of inactivity before GPU→CPU switch (default 600)
  LLAMA_SET_MODE_CMD      Path to the mode-switch script (default /opt/llama/set-mode.sh)
  PANDABOT_DATA_DIR       Used to persist the current mode across bot restarts
"""

import asyncio
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request

log = logging.getLogger("panda-bot.llama")

LLAMA_PORT = int(os.environ.get("LLAMA_PORT", "8081"))
LLAMA_HOST = os.environ.get("LLAMA_HOST", "127.0.0.1")
LLAMA_IDLE_TIMEOUT_SECS = int(os.environ.get("LLAMA_IDLE_TIMEOUT_SECS", "600"))
LLAMA_SET_MODE_CMD = os.environ.get("LLAMA_SET_MODE_CMD", "/opt/llama/set-mode.sh")

_HEALTH_URL = f"http://{LLAMA_HOST}:{LLAMA_PORT}/health"
_MODE_FILE = os.path.join(
    os.environ.get("PANDABOT_DATA_DIR", "/opt/discord-bot"),
    "llama_mode.txt",
)

_current_mode: str = "unknown"
_last_used: float = 0.0
_switch_lock: asyncio.Lock | None = None  # created lazily (needs running event loop)


def _get_lock() -> asyncio.Lock:
    global _switch_lock
    if _switch_lock is None:
        _switch_lock = asyncio.Lock()
    return _switch_lock


def _read_mode_file() -> str:
    try:
        with open(_MODE_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return "unknown"


def _write_mode_file(mode: str) -> None:
    try:
        with open(_MODE_FILE, "w") as f:
            f.write(mode)
    except Exception as e:
        log.warning("Could not write llama mode file: %s", e)


def _check_health() -> bool:
    try:
        with urllib.request.urlopen(_HEALTH_URL, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def _do_mode_switch(mode: str) -> bool:
    """Run set-mode.sh synchronously (intended for thread executor). Returns True on success."""
    try:
        result = subprocess.run(
            ["sudo", LLAMA_SET_MODE_CMD, mode],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            log.error("set-mode.sh %s failed (rc=%d): %s", mode, result.returncode, result.stderr.strip())
            return False
        return True
    except subprocess.TimeoutExpired:
        log.error("set-mode.sh %s timed out after 60s", mode)
        return False
    except Exception as e:
        log.error("Mode switch to %s failed: %s", mode, e)
        return False


async def ensure_gpu_mode() -> bool:
    """
    Ensure llama-server is in GPU mode. Idempotent — returns immediately if already GPU.
    Returns True when the server is ready, False if the switch failed.
    """
    global _current_mode, _last_used
    _last_used = time.monotonic()

    if _current_mode == "gpu" and _check_health():
        return True

    async with _get_lock():
        # Re-check inside lock — another coroutine may have switched while we waited
        if _current_mode == "gpu" and _check_health():
            return True

        log.info("llama_manager: switching to GPU mode")
        loop = asyncio.get_running_loop()
        success = await loop.run_in_executor(None, _do_mode_switch, "gpu")
        if success:
            _current_mode = "gpu"
            _write_mode_file("gpu")
            log.info("llama_manager: GPU mode ready")
        else:
            log.error("llama_manager: GPU mode switch failed — requests will use CPU")
        return success


async def ensure_cpu_mode() -> None:
    """Switch llama-server to CPU mode (idle/standby). Idempotent."""
    global _current_mode

    if _current_mode == "cpu":
        return

    async with _get_lock():
        if _current_mode == "cpu":
            return

        log.info("llama_manager: switching to CPU mode (idle)")
        loop = asyncio.get_running_loop()
        success = await loop.run_in_executor(None, _do_mode_switch, "cpu")
        if success:
            _current_mode = "cpu"
            _write_mode_file("cpu")
            log.info("llama_manager: CPU mode active, VRAM freed")


def current_mode() -> str:
    """Return the last known mode: 'cpu', 'gpu', or 'unknown'."""
    return _current_mode


def touch_last_used() -> None:
    """Record a request timestamp — resets the idle timer."""
    global _last_used
    _last_used = time.monotonic()


def is_idle() -> bool:
    """Return True if no Gemma requests have arrived within LLAMA_IDLE_TIMEOUT_SECS."""
    return time.monotonic() - _last_used > LLAMA_IDLE_TIMEOUT_SECS


def init() -> None:
    """Restore last-known mode from the persisted file (call once at startup)."""
    global _current_mode
    _current_mode = _read_mode_file()
    log.info("llama_manager: initialized, last known mode=%s", _current_mode)
