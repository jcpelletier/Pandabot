"""
Pandabot Voice Gateway — FastAPI service.

Acts as the server-side brain for an Android voice terminal app:
  Flutter app → audio → /transcribe → Whisper STT → Claude loop → Kokoro TTS → MP3

WebSocket /ws carries real-time state events (thinking / speaking / idle).
All conversation turns are mirrored to Discord.

Environment variables:
    VOICE_GATEWAY_TOKEN   — shared secret for Bearer auth
    VOICE_GATEWAY_PORT    — listen port (default 8900)
    STT_URL               — Whisper endpoint (default http://localhost:8001)
    TTS_URL               — Kokoro endpoint (default http://localhost:8880)
    TTS_VOICE             — Kokoro voice (default af_heart)
    DISCORD_VOICE_CHANNEL_ID / DISCORD_CHANNEL_ID — Discord mirror channel
    DISCORD_BOT_TOKEN     — Discord bot token for mirror posts
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from contextlib import asynccontextmanager

import aiohttp
import uvicorn
from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from voice_gateway import discord_mirror, session, stt, tts
from voice_gateway.session import SessionManager

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
VOICE_GATEWAY_TOKEN: str = os.environ.get("VOICE_GATEWAY_TOKEN", "changeme")
VOICE_GATEWAY_PORT: int = int(os.environ.get("VOICE_GATEWAY_PORT", "8900"))

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------
try:
    from pandabot_core.identity import build_system_prompt

    system_prompt: str = build_system_prompt()
    logger.info("Loaded system prompt from pandabot_core.identity")
except ImportError:
    system_prompt = (
        "You are Pandabot, a helpful home server assistant. "
        "Respond conversationally and concisely — your response will be spoken aloud."
    )
    logger.info("pandabot_core not available; using fallback system prompt")

# ---------------------------------------------------------------------------
# Tool definitions (imported from the discord-bot root via PYTHONPATH)
# ---------------------------------------------------------------------------
try:
    from tools import TOOL_DEFINITIONS, execute_tool  # type: ignore[import]

    logger.info("Loaded TOOL_DEFINITIONS and execute_tool from tools.py")
except ImportError:
    logger.warning("Could not import tools.py; Claude will run without tools")
    TOOL_DEFINITIONS: list = []

    def execute_tool(name: str, params: dict) -> str:  # type: ignore[misc]
        return f"Tool {name!r} is not available in this deployment."


# ---------------------------------------------------------------------------
# Claude loop
# ---------------------------------------------------------------------------
try:
    from pandabot_core.llm.loop import run_claude_loop  # type: ignore[import]

    logger.info("Loaded run_claude_loop from pandabot_core")
except ImportError:
    logger.error(
        "pandabot_core.llm.loop not available — Claude loop will not work. "
        "Ensure PYTHONPATH includes /opt/pandabot-core."
    )

    def run_claude_loop(  # type: ignore[misc]
        user_message: str,
        history: list,
        tool_definitions: list,
        execute_tool_fn,
        system_prompt: str,
        **kwargs,
    ) -> str:
        return "I'm sorry, the AI backend is not currently available."


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
session_manager = SessionManager()

# WebSocket connections: device_id → set of active WebSocket objects
_ws_connections: dict[str, set[WebSocket]] = {}
_ws_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_bearer(authorization: str | None) -> None:
    """Raise HTTP 401 if the Authorization header doesn't match VOICE_GATEWAY_TOKEN."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if token != VOICE_GATEWAY_TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


async def _broadcast(event: dict, device_id: str | None) -> None:
    """Send a JSON event to all WebSocket clients for a device (or all devices)."""
    payload = json.dumps(event)
    async with _ws_lock:
        if device_id is not None:
            targets = list(_ws_connections.get(device_id, set()))
        else:
            targets = [ws for connections in _ws_connections.values() for ws in connections]

    dead: list[tuple[str | None, WebSocket]] = []
    for ws in targets:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append((device_id, ws))

    if dead:
        async with _ws_lock:
            for dev, ws in dead:
                if dev and dev in _ws_connections:
                    _ws_connections[dev].discard(ws)


async def _broadcast_idle_after_delay(device_id: str, delay: float = 0.5) -> None:
    await asyncio.sleep(delay)
    await _broadcast({"state": "idle", "device_id": device_id}, device_id)


def _total_connections() -> int:
    return sum(len(v) for v in _ws_connections.values())


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info(
        "Voice Gateway starting on port %d | STT=%s | TTS=%s | Voice=%s",
        VOICE_GATEWAY_PORT,
        os.environ.get("STT_URL", "http://localhost:8001"),
        os.environ.get("TTS_URL", "http://localhost:8880"),
        os.environ.get("TTS_VOICE", "af_heart"),
    )

    http_session = aiohttp.ClientSession()
    app.state.http_session = http_session

    # Share session with sub-modules
    stt.set_session(http_session)
    tts.set_session(http_session)
    discord_mirror.set_session(http_session)

    # Connectivity probe — warn but don't crash
    await _probe_service(
        http_session,
        os.environ.get("STT_URL", "http://localhost:8001"),
        "Whisper STT",
    )
    await _probe_service(
        http_session,
        os.environ.get("TTS_URL", "http://localhost:8880"),
        "Kokoro TTS",
    )

    yield

    # Shutdown
    logger.info("Voice Gateway shutting down — closing aiohttp session")
    await http_session.close()


async def _probe_service(session: aiohttp.ClientSession, base_url: str, name: str) -> None:
    """Attempt a HEAD/GET to the service base URL; log a warning if unreachable."""
    try:
        async with session.get(base_url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
            logger.info("%s reachable at %s (HTTP %d)", name, base_url, resp.status)
    except Exception as exc:
        logger.warning("%s at %s is NOT reachable: %s", name, base_url, exc)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Pandabot Voice Gateway", lifespan=lifespan)


# ---------------------------------------------------------------------------
# POST /transcribe
# ---------------------------------------------------------------------------

@app.post("/transcribe")
async def transcribe(
    request: Request,
    audio: UploadFile,
    authorization: str | None = Header(default=None),
    x_device_id: str | None = Header(default=None),
) -> Response:
    """
    Main voice pipeline:
      audio → Whisper STT → Claude loop → Kokoro TTS → MP3 response
    """
    _check_bearer(authorization)
    device_id = x_device_id or "default"
    http_session: aiohttp.ClientSession = request.app.state.http_session

    tmp_path: str | None = None
    try:
        # Save uploaded audio to a temp file
        suffix = os.path.splitext(audio.filename or "audio.wav")[1] or ".wav"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name
            content = await audio.read()
            tmp.write(content)

        logger.info("Received audio (%d bytes) from device %s", len(content), device_id)

        # Notify clients we are thinking
        await _broadcast({"state": "thinking", "device_id": device_id}, device_id)

        # STT
        user_text = await stt.transcribe(tmp_path, http_session)
        if not user_text:
            logger.info("STT returned empty result; returning 204")
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        logger.info("User said: %r", user_text)

        # Fetch conversation history
        history = session_manager.get_history(device_id)

        # Run Claude loop in thread executor (it is synchronous)
        loop = asyncio.get_event_loop()
        response_text: str = await loop.run_in_executor(
            None,
            run_claude_loop,
            user_text,
            history,
            TOOL_DEFINITIONS,
            execute_tool,
            system_prompt,
        )

        logger.info("Claude response: %r", response_text[:120])

        # Persist turn
        session_manager.add_turn(device_id, user_text, response_text)

        # TTS
        mp3_bytes = await tts.synthesize(response_text, http_session)
        if not mp3_bytes:
            logger.error("TTS returned no bytes for device %s", device_id)
            return Response(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # Notify clients we are speaking
        await _broadcast({"state": "speaking", "device_id": device_id}, device_id)

        # Mirror to Discord (fire-and-forget)
        asyncio.create_task(discord_mirror.post_turn(user_text, response_text, http_session))

        # Schedule idle notification after audio delivery
        asyncio.create_task(_broadcast_idle_after_delay(device_id, delay=0.5))

        return Response(content=mp3_bytes, media_type="audio/mpeg")

    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# GET /ws  — WebSocket state stream
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str = "",
    device_id: str = "default",
) -> None:
    if token != VOICE_GATEWAY_TOKEN:
        await websocket.close(code=4003)  # 4003 = policy violation / forbidden
        return

    await websocket.accept()
    logger.info("WebSocket connected: device=%s", device_id)

    async with _ws_lock:
        if device_id not in _ws_connections:
            _ws_connections[device_id] = set()
        _ws_connections[device_id].add(websocket)

    try:
        while True:
            # Accept incoming messages (ignored for now) to keep the connection alive
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                break
    except Exception:
        pass
    finally:
        async with _ws_lock:
            if device_id in _ws_connections:
                _ws_connections[device_id].discard(websocket)
                if not _ws_connections[device_id]:
                    del _ws_connections[device_id]
        logger.info("WebSocket disconnected: device=%s", device_id)


# ---------------------------------------------------------------------------
# POST /push  — push notification to connected clients
# ---------------------------------------------------------------------------

class PushPayload(BaseModel):
    type: str
    message: str
    device_id: str | None = None


@app.post("/push")
async def push(
    payload: PushPayload,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    _check_bearer(authorization)

    event = {
        "type": payload.type,
        "message": payload.message,
        "device_id": payload.device_id,
    }
    await _broadcast(event, payload.device_id)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "connections": _total_connections()})


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "voice_gateway.main:app",
        host="0.0.0.0",
        port=VOICE_GATEWAY_PORT,
        log_level="info",
    )
