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

WebSocket envelope schema (server → client):
    {state: 'thinking'|'speaking'|'idle', device_id}
        Lifecycle markers around a voice turn.
    {type: 'turn', user_text, assistant_text, device_id}
        Mirrored conversation history; sent for every turn including silent ones.
    {type: 'push', message, device_id}
        Operator-initiated push notification.
    {type: 'play_audio', queue: [{id,title,artist,album,duration_ms,url,art_url}],
        summary, source, device_id}
        Music playback request. Client should load queue[0].url and play; on
        track end advance through the queue. Includes a short human-readable
        summary string for UI.
    {type: 'playback_control', action: 'pause'|'resume'|'skip'|'stop', device_id}
        Music control from voice. Client manipulates its current player.

Music control tools (play_music, pause_music, resume_music, skip_track,
stop_music) suppress TTS via the per-request voice_ctx['silent_tts'] flag.
The /transcribe endpoint returns 204 No Content in that case so the client
does not try to play any audio response.
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
_VOICE_PREAMBLE = (
    "You are responding via a voice interface. Your reply will be converted to speech and played aloud, "
    "so you must write the way a person speaks, not the way a person writes a Discord post.\n\n"
    "Hard rules:\n"
    "- No emojis, ever. They will be read aloud as words and ruin the response.\n"
    "- No markdown: no bullet points, no headers, no bold, no italics, no lists, no backticks.\n"
    "- No category labels followed by a colon (\"System & Monitoring:\", \"Logs:\"). Write flowing prose.\n"
    "- No follow-up questions, no offers to do more — answer and stop.\n"
    "- Use natural spoken phrasing. Say \"about forty gigs\" not \"40.2 GB\". Say \"yeah\" not \"yes\".\n\n"
    "Length: prefer terse. Most answers should be one short sentence. Two sentences if context truly helps. "
    "Three sentences is the absolute maximum and only for questions that genuinely require it (e.g. \"list everything\"). "
    "Err on the side of being too brief rather than too thorough — the user can always ask follow-ups.\n\n"
    "If the user asks for a list of many things, summarise in a sentence rather than enumerating "
    "(\"I've got tools for system stats, logs, Jellyfin, file management, and ripping\" — not a categorised dump).\n\n"
    "NEVER call switch_model — voice queries must run on whatever model the operator chose in Discord. "
    "If the user asks you to switch models, tell them to do it from Discord with !haiku, !deepseek, etc.\n\n"
    "Tool use is REQUIRED for factual questions about the server, Jellyfin library, family members, system stats, "
    "logs, or anything else covered by your tools. Never guess or invent numbers, dates, names, or facts. "
    "If you don't have a relevant tool, say \"I don't know\" rather than making something up.\n\n"
    "PLAYING MUSIC: if the user asks you to play music (artist, album, song, soundtrack, etc.), you MUST call "
    "the play_music tool with the artist / album / track fields you extracted from their utterance. Do NOT just say "
    "\"playing that now\" without calling the tool — that produces no audio and is a regression-tier failure. "
    "Similarly, music control commands MUST go through the corresponding tool: "
    "pause -> pause_music; resume / continue / play (when already in music mode) -> resume_music; "
    "skip / next / next song -> skip_track; back / previous / previous song -> previous_track; "
    "loop / repeat / loop this album / repeat this song -> set_loop_mode; "
    "soft stop (still resumable) -> stop_music; full exit ('stop playing music', 'exit music', "
    "'turn off the music', 'I'm done with music') -> exit_music. "
    "Speak the tool's returned summary verbatim after calling it.\n\n"
    "DO NOT second-guess playback state. You do NOT know whether music is currently playing — the client device "
    "is the source of truth. NEVER respond with phrases like \"no music is playing\", \"nothing to skip\", \"there's "
    "nothing to resume\", \"it's already playing\", \"going back to the previous track\" (without the call), "
    "\"looping the whole queue\" (without the call), \"done, exited music mode\" (without the call) — these are "
    "all hallucinated acknowledgements that produce no actual effect on the device. The contract is: tool call "
    "FIRST, then a short spoken confirmation. If you only produce the confirmation without the tool call, the user "
    "sees no change and the test fails.\n\n"
    "Common phrasings and the REQUIRED tool:\n"
    "  'pause' / 'pause it' -> pause_music\n"
    "  'resume' / 'continue' / 'play' (during music) / 'keep going' -> resume_music\n"
    "  'next' / 'next song' / 'skip' / 'skip this' -> skip_track\n"
    "  'back' / 'previous' / 'previous song' / 'last song' / 'go back' -> previous_track\n"
    "  'loop' / 'repeat' / 'loop this album' / 'repeat this song' / 'stop looping' -> set_loop_mode\n"
    "  'stop' (alone, no 'playing') / 'hold on' -> stop_music (soft)\n"
    "  'stop playing music' / 'stop the music' / 'exit music' / 'turn off the music' -> exit_music (hard)\n"
    "Always call the listed tool for these phrasings; do NOT respond with prose only.\n\n"
)

try:
    from pandabot_core.identity import build_system_prompt

    system_prompt: str = _VOICE_PREAMBLE + build_system_prompt()
    logger.info("Loaded system prompt from pandabot_core.identity")
except ImportError:
    system_prompt = (
        _VOICE_PREAMBLE +
        "You are Pandabot, a helpful home server assistant."
    )
    logger.info("pandabot_core not available; using fallback system prompt")

# ---------------------------------------------------------------------------
# Tool definitions (imported from the discord-bot root via PYTHONPATH)
# ---------------------------------------------------------------------------
try:
    from tools import TOOL_DEFINITIONS, execute_tool, set_voice_context  # type: ignore[import]

    logger.info("Loaded TOOL_DEFINITIONS, execute_tool, set_voice_context from tools.py")
except ImportError:
    logger.warning("Could not import tools.py; Claude will run without tools")
    TOOL_DEFINITIONS: list = []

    def execute_tool(name: str, params: dict) -> str:  # type: ignore[misc]
        return f"Tool {name!r} is not available in this deployment."

    def set_voice_context(ctx):  # type: ignore[misc]
        pass


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

        if len(content) < 1000:
            logger.info("Audio too small (%d bytes) — ignoring", len(content))
            return Response(status_code=status.HTTP_204_NO_CONTENT)

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

        # Per-request voice context: tools that need to talk to the device
        # (e.g. play_music, pause_music) push envelopes onto this list and
        # set silent_tts when no spoken response should follow.
        pending_envelopes: list[dict] = []
        voice_ctx = {
            'emit': pending_envelopes.append,
            'silent_tts': False,
        }

        def _run_loop_with_ctx():
            set_voice_context(voice_ctx)
            try:
                return run_claude_loop(
                    user_text,
                    history,
                    TOOL_DEFINITIONS,
                    execute_tool,
                    system_prompt,
                )
            finally:
                set_voice_context(None)

        # Run Claude loop in thread executor (it is synchronous)
        loop = asyncio.get_event_loop()
        response_text: str = await loop.run_in_executor(None, _run_loop_with_ctx)

        logger.info(
            "Claude response: %r  envelopes=%d silent_tts=%s",
            response_text[:120], len(pending_envelopes), voice_ctx['silent_tts'],
        )

        # Persist turn
        session_manager.add_turn(device_id, user_text, response_text)

        # Send turn to client for conversation history display (always, even for silent turns)
        await _broadcast(
            {"type": "turn", "user_text": user_text, "assistant_text": response_text},
            device_id,
        )

        # Broadcast any tool-emitted envelopes (play_audio, playback_control, etc.)
        for env in pending_envelopes:
            env.setdefault("device_id", device_id)
            await _broadcast(env, device_id)

        # Discord mirror happens for every voice turn, including silent ones,
        # so operators can scroll back and see music commands in history
        asyncio.create_task(discord_mirror.post_turn(user_text, response_text, http_session))

        # Silent turns (music control, etc.) skip TTS entirely; return 204
        if voice_ctx['silent_tts']:
            asyncio.create_task(_broadcast_idle_after_delay(device_id, delay=0.2))
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        # TTS
        mp3_bytes = await tts.synthesize(response_text, http_session)
        if not mp3_bytes:
            logger.error("TTS returned no bytes for device %s", device_id)
            return Response(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # Notify clients we are speaking
        await _broadcast({"state": "speaking", "device_id": device_id}, device_id)

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

    # Start warming Whisper in the background as soon as a client connects
    asyncio.create_task(stt.warm())

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
