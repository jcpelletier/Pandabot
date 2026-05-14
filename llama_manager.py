"""
llama-server management for local model inference.

Supports multiple local model services (gemma, qwen) with exclusive activation —
only one runs at a time. Switching calls /opt/llama/switch-model.sh which stops
the current service and starts the requested one, then polls /health until ready.

Env vars
--------
  LLAMA_PORT             Port llama-server listens on (default 8081)
  LLAMA_HOST             Host (default 127.0.0.1)
  LLAMA_LOCAL_PROFILES   Comma-separated profile names that are local models
                         e.g. "gemma,qwen"
"""

import asyncio
import logging
import os
import subprocess
import urllib.request

log = logging.getLogger("panda-bot.llama")

LLAMA_PORT = int(os.environ.get("LLAMA_PORT", "8081"))
LLAMA_HOST = os.environ.get("LLAMA_HOST", "127.0.0.1")

_HEALTH_URL = f"http://{LLAMA_HOST}:{LLAMA_PORT}/health"

_LOCAL_PROFILES: set[str] = {
    p.strip()
    for p in os.environ.get("LLAMA_LOCAL_PROFILES", "gemma,qwen").split(",")
    if p.strip()
}


def is_local_profile(profile_name: str) -> bool:
    return profile_name in _LOCAL_PROFILES


def _check_health() -> bool:
    try:
        with urllib.request.urlopen(_HEALTH_URL, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def current_mode() -> str:
    return "gpu-full" if _check_health() else "unknown"


def _switch_model_blocking(profile_name: str) -> bool:
    """Call switch-model.sh synchronously. Intended to run in a thread executor."""
    result = subprocess.run(
        ["sudo", "/opt/llama/switch-model.sh", profile_name],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        log.error("switch-model.sh failed: %s", result.stderr or result.stdout)
        return False
    log.info("switch-model.sh: %s", result.stdout.strip())
    return True


async def ensure_model(profile_name: str) -> bool:
    """
    Ensure the local model for `profile_name` is loaded.
    Stops any conflicting service and starts the right one if needed.
    Returns True when the server is healthy.
    """
    if not is_local_profile(profile_name):
        return _check_health()

    if _check_health():
        # A model is already running — check if it's the right one by looking
        # at which service is active. If it matches, we're done.
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", f"llama-server-{profile_name}"],
            capture_output=True,
        )
        gemma_is_right = profile_name == "gemma" and subprocess.run(
            ["systemctl", "is-active", "--quiet", "llama-server"],
            capture_output=True,
        ).returncode == 0
        if result.returncode == 0 or gemma_is_right:
            return True

    log.info("llama_manager: switching to local profile '%s'", profile_name)
    return await asyncio.get_running_loop().run_in_executor(
        None, _switch_model_blocking, profile_name
    )


async def ensure_gpu_mode() -> bool:
    """Legacy no-op kept for compatibility. Use ensure_model(profile_name)."""
    return _check_health()


def init() -> None:
    if _check_health():
        log.info("llama_manager: server healthy at %s", _HEALTH_URL)
    else:
        log.warning("llama_manager: server not responding at %s", _HEALTH_URL)
