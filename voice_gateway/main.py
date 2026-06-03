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
    TTS_VOICE             — Kokoro voice (default am_santa)
    DISCORD_VOICE_CHANNEL_ID / DISCORD_CHANNEL_ID — Discord mirror channel
    DISCORD_BOT_TOKEN     — Discord bot token for mirror posts

WebSocket envelope schema (server → client):
    {state: 'thinking'|'speaking'|'idle', device_id}

WebSocket messages (client → server):
    {type: 'cast_devices', devices: ["name1", "name2", ...]}
        Sent by the Flutter app whenever the discovered Chromecast device
        list changes. Gateway stores these per device_id and injects them
        into the LLM system prompt so it knows which cast_target names to use.
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
    {type: 'speak', text, audio_b64, device_id}
        System notification pushed from the Discord bot. audio_b64 is a
        base64-encoded MP3. Client should play it immediately if not busy.
    {type: 'speak_chunk', seq: int, audio_b64: str, device_id}
        Sentence-level TTS chunk for a voice turn. Sent one per sentence before
        the HTTP /transcribe response returns. Client queues and plays each
        chunk as it arrives so audio starts before all sentences are synthesised.
    {type: 'speak_done', total: int, device_id}
        Sentinel sent after all speak_chunk events for a turn. total is the
        number of chunks sent. /transcribe returns HTTP 202 immediately after
        this event is broadcast.

Music control tools (play_music, pause_music, resume_music, skip_track,
stop_music) suppress TTS via the per-request voice_ctx['silent_tts'] flag.
The /transcribe endpoint returns 204 No Content in that case so the client
does not try to play any audio response.

POST /chat accepts {"text": "...", "device_id": "...", "voice": "..."} and
runs the same Claude→TTS pipeline as /transcribe but skips Whisper STT.
Used by Android Auto: Google Assistant App Actions provide the transcription;
the Android background service posts the extracted text here directly.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import tempfile
from contextlib import asynccontextmanager

import aiohttp
import uvicorn
from fastapi import (
    FastAPI,
    Form,
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
    "Length: three sentences maximum, no exceptions. Not even for 'describe everything', 'list all your tools', "
    "'tell me a story', or similar open-ended requests — those still get three sentences or fewer. "
    "One sentence is ideal. Err on the side of too brief; the user can ask follow-ups.\n\n"
    "If the user asks for a list or enumeration, give a single sentence overview and stop. "
    "Good: \"I've got tools for system stats, Jellyfin, Jenkins, file management, and hardware health.\" "
    "Bad: proceeding to describe each category. Never enumerate items one by one.\n\n"
    "NEVER call switch_model — voice queries must run on whatever model the operator chose in Discord. "
    "If the user asks you to switch models, tell them to do it from Discord with !haiku, !deepseek, etc.\n\n"
    "SCHEDULING — this rule overrides the 'answer immediately' instructions above: if the user asks for "
    "something at a FUTURE time or on a condition ('in X minutes', 'in X hours', 'tomorrow', 'at [time]', "
    "'remind me', 'when X happens', 'once X is done', etc.), you MUST call manage_schedule to defer the "
    "task — do NOT answer or perform the action immediately. After scheduling, confirm in one short sentence "
    "(e.g. 'Got it, I'll tell you a joke in two minutes.').\n\n"
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
    "RADIO: if the user asks to stream or play a radio station (by call sign, name, or frequency), "
    "you MUST call play_radio with the call sign or name as the query — EVERY time, even if you called it "
    "moments ago in this conversation. Do NOT produce a confirmation like \"Streaming X on your terminal.\" "
    "or any similar phrase without first calling the tool. The tool call is what starts the stream; "
    "a text response without a tool call does nothing. "
    "To stop a stream: call stop_radio. "
    "To check what is playing: call radio_status. "
    "Speak the tool's returned summary verbatim after calling it.\n\n"
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

# Cast devices reported by each client: device_id → list of friendly names
_cast_devices: dict[str, list[str]] = {}


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
        os.environ.get("TTS_VOICE", "am_santa"),
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
# Music control intent shortcuts
# ---------------------------------------------------------------------------
# Three rounds of preamble strengthening haven't fully stopped deepseek from
# hallucinating "Going back to the previous track." / "Resuming." / "Done,
# exited music mode." without actually calling the tool. For the most common
# music control utterances we pattern-match BEFORE running the LLM loop and
# dispatch the tool directly. The LLM only sees anything that doesn't match.

import re as _re

_MUSIC_INTENTS: list[tuple[_re.Pattern, str, dict, str]] = [
    # (regex, tool name, kwargs, spoken confirmation — TTS-suppressed)
    (_re.compile(r"^pause( it| the music)?[.!]?$", _re.IGNORECASE),
     "pause_music", {}, "Paused."),
    (_re.compile(r"^(resume( music| playing)?|continue( music| playing)?|keep going|play(?: it| music)?)[.!]?$", _re.IGNORECASE),
     "resume_music", {}, "Resuming."),
    (_re.compile(r"^(next( song| track)?|skip( this| the song| track)?)[.!]?$", _re.IGNORECASE),
     "skip_track", {}, "Skipping."),
    (_re.compile(r"^(back|previous( song| track)?|last song|go back|previous)[.!]?$", _re.IGNORECASE),
     "previous_track", {}, "Going back."),
    (_re.compile(r"^(loop|repeat|loop (this|the)( album| queue)|loop all|repeat all)[.!]?$", _re.IGNORECASE),
     "set_loop_mode", {"mode": "all"}, "Looping the queue."),
    (_re.compile(r"^(repeat (this|the) song|loop( this)? one|repeat one)[.!]?$", _re.IGNORECASE),
     "set_loop_mode", {"mode": "one"}, "Repeating this song."),
    (_re.compile(r"^(no loop|no repeat|stop looping|turn off (loop|repeat))[.!]?$", _re.IGNORECASE),
     "set_loop_mode", {"mode": "off"}, "Loop off."),
    (_re.compile(r"^(stop( the music)?|hold on)[.!]?$", _re.IGNORECASE),
     "stop_music", {}, "Stopped."),
    (_re.compile(r"^(stop playing music|exit music( mode)?|turn off (the )?music|close (the )?music|i'?m done with music)[.!]?$", _re.IGNORECASE),
     "exit_music", {}, "Music off."),
]


def _try_music_intent(utterance: str) -> tuple[str, str, dict, str] | None:
    """If the utterance is a recognised music control phrase, return
    (utterance, tool_name, kwargs, spoken_confirmation). Else None.

    Strips trailing whitespace + common STT artefacts before matching.
    """
    u = utterance.strip().rstrip(",")
    for rx, tool, kwargs, say in _MUSIC_INTENTS:
        if rx.match(u):
            return u, tool, kwargs, say
    return None


# ---------------------------------------------------------------------------
# _process_utterance — shared pipeline (called by /transcribe and /chat)
# ---------------------------------------------------------------------------

async def _process_utterance(
    user_text: str,
    device_id: str,
    voice: str | None,
    http_session: aiohttp.ClientSession,
) -> Response:
    """text → Claude loop → Kokoro TTS → WebSocket broadcast.

    Returns 202 Accepted (TTS chunks broadcast) or 204 No Content (silent turn).
    Caller is responsible for broadcasting the 'thinking' state before calling this.
    """
    pending_envelopes: list[dict] = []
    voice_ctx = {
        'emit': pending_envelopes.append,
        'silent_tts': False,
    }

    # FAST PATH — music control intents bypass the LLM. The model has
    # been caught hallucinating "Going back to the previous track."
    # / "Resuming." / "Done, exited music mode." without calling the
    # tool. Pattern-match the common phrases and dispatch directly.
    intent = _try_music_intent(user_text)
    if intent is not None:
        _, tool_name, tool_kwargs, spoken = intent
        logger.info("Music intent shortcut: %s%s -> %s", tool_name, tool_kwargs, spoken)
        set_voice_context(voice_ctx)
        try:
            execute_tool(tool_name, tool_kwargs)
        finally:
            set_voice_context(None)
        response_text = spoken
    else:
        history = session_manager.get_history(device_id)

        cast_devs = _cast_devices.get(device_id, [])
        effective_prompt = system_prompt
        if cast_devs:
            names = ", ".join(f'"{d}"' for d in cast_devs)
            effective_prompt += (
                f"\n\nChromecast devices currently visible on the network: {names}.\n"
                "When the user asks to cast music to a device:\n"
                "- Match their utterance to one of the names above (fuzzy match).\n"
                "- If confident of the match, call play_music with cast_target set "
                "to the EXACT device name from the list above.\n"
                "- If ambiguous between multiple devices, list the available device "
                "names and ask the user to be more specific — do not call play_music yet.\n"
                "- If the user asks to stop casting, call stop_music.\n"
            )

        # Streaming TTS bridge: LLM loop runs in a thread executor and calls
        # on_delta with each text chunk. on_delta pushes chunks onto an asyncio
        # queue via call_soon_threadsafe so the async consumer can synthesize
        # and broadcast each sentence as it arrives rather than waiting for the
        # full response.
        event_loop = asyncio.get_event_loop()
        delta_queue: asyncio.Queue[str | None] = asyncio.Queue()

        def on_delta(chunk: str) -> None:
            # silent_tts is set by tool calls in earlier rounds; by the time
            # on_delta fires for the final text round it is already stable.
            if not voice_ctx['silent_tts']:
                event_loop.call_soon_threadsafe(delta_queue.put_nowait, chunk)

        def _run_loop_with_ctx():
            set_voice_context(voice_ctx)
            try:
                return run_claude_loop(
                    user_text,
                    history,
                    TOOL_DEFINITIONS,
                    execute_tool,
                    effective_prompt,
                    on_text_delta=on_delta,
                )
            finally:
                set_voice_context(None)
                # Always unblock the consumer — even on exception.
                event_loop.call_soon_threadsafe(delta_queue.put_nowait, None)

        # Sentence-boundary regex (same as tts._SENTENCE_SPLIT_RE but used as search).
        _SENT_RE = _re.compile(r'(?<=[.!?])\s+')

        async def _stream_tts() -> int:
            """Drain delta_queue, synthesize sentences in parallel, broadcast in order.

            Fires TTS tasks as sentences arrive but limits Kokoro to 2 concurrent
            calls via a semaphore. Sentence N+1 is synthesizing while sentence N is
            being broadcast, without hammering the TTS container with all sentences
            at once (which causes timeouts on longer responses).
            """
            tts_queue: asyncio.Queue[asyncio.Task | None] = asyncio.Queue()
            first_chunk = True
            _sem = asyncio.Semaphore(2)

            async def _synthesize(sentence: str) -> bytes | None:
                async with _sem:
                    return await tts.synthesize(sentence, http_session, voice=voice)

            async def _produce() -> None:
                buf = ""
                while True:
                    chunk = await delta_queue.get()
                    if chunk is None:
                        break
                    buf += chunk
                    while m := _SENT_RE.search(buf):
                        sentence = buf[:m.start()].strip()
                        buf = buf[m.end():]
                        if sentence:
                            await tts_queue.put(asyncio.create_task(_synthesize(sentence)))
                if buf.strip():
                    await tts_queue.put(asyncio.create_task(_synthesize(buf.strip())))
                await tts_queue.put(None)

            async def _consume() -> int:
                nonlocal first_chunk
                seq = 0
                while True:
                    task = await tts_queue.get()
                    if task is None:
                        break
                    mp3 = await task
                    if mp3:
                        if first_chunk:
                            await _broadcast({"state": "speaking", "device_id": device_id}, device_id)
                            first_chunk = False
                        audio_b64 = base64.b64encode(mp3).decode()
                        await _broadcast(
                            {"type": "speak_chunk", "seq": seq, "audio_b64": audio_b64, "device_id": device_id},
                            device_id,
                        )
                        logger.debug("speak_chunk seq=%d  %d bytes  device=%s", seq, len(mp3), device_id)
                        seq += 1
                return seq

            consume_result, _ = await asyncio.gather(_consume(), _produce())
            return consume_result

        response_text, streaming_seq = await asyncio.gather(
            event_loop.run_in_executor(None, _run_loop_with_ctx),
            _stream_tts(),
        )

    logger.info(
        "Claude response: %r  envelopes=%d silent_tts=%s",
        response_text[:120], len(pending_envelopes), voice_ctx['silent_tts'],
    )

    # Don't store silent (tool-dispatched) turns in history. The action
    # happened via WS envelope; storing the tool's return text causes the
    # LLM to regurgitate that text on the next identical utterance instead
    # of calling the tool again.
    if not voice_ctx['silent_tts']:
        session_manager.add_turn(device_id, user_text, response_text)

    await _broadcast(
        {"type": "turn", "user_text": user_text, "assistant_text": response_text},
        device_id,
    )

    for env in pending_envelopes:
        env.setdefault("device_id", device_id)
        await _broadcast(env, device_id)

    asyncio.create_task(discord_mirror.post_turn(user_text, response_text, http_session))

    if voice_ctx['silent_tts']:
        asyncio.create_task(_broadcast_idle_after_delay(device_id, delay=0.2))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # For the LLM path, TTS was streamed concurrently above; just close the turn.
    # For the music fast path, run the old sequential TTS path.
    if intent is not None:
        await _broadcast({"state": "speaking", "device_id": device_id}, device_id)
        seq = 0
        async for chunk_bytes in tts.synthesize_sentences(response_text, http_session, voice=voice):
            audio_b64 = base64.b64encode(chunk_bytes).decode()
            await _broadcast(
                {"type": "speak_chunk", "seq": seq, "audio_b64": audio_b64, "device_id": device_id},
                device_id,
            )
            logger.debug("speak_chunk seq=%d  %d bytes  device=%s", seq, len(chunk_bytes), device_id)
            seq += 1
    else:
        seq = streaming_seq

    await _broadcast(
        {"type": "speak_done", "total": seq, "device_id": device_id},
        device_id,
    )
    logger.info("TTS done: %d chunk(s) for device %s", seq, device_id)

    asyncio.create_task(_broadcast_idle_after_delay(device_id, delay=0.5))

    return Response(status_code=status.HTTP_202_ACCEPTED)


# ---------------------------------------------------------------------------
# POST /transcribe
# ---------------------------------------------------------------------------

@app.post("/transcribe")
async def transcribe(
    request: Request,
    audio: UploadFile,
    voice: str | None = Form(default=None),
    authorization: str | None = Header(default=None),
    x_device_id: str | None = Header(default=None),
    x_tts_voice: str | None = Header(default=None),
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
        suffix = os.path.splitext(audio.filename or "audio.wav")[1] or ".wav"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name
            content = await audio.read()
            tmp.write(content)

        logger.info("Received audio (%d bytes) from device %s", len(content), device_id)

        if len(content) < 1000:
            logger.info("Audio too small (%d bytes) — ignoring", len(content))
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        await _broadcast({"state": "thinking", "device_id": device_id}, device_id)

        user_text = await stt.transcribe(tmp_path, http_session)
        if not user_text:
            logger.info("STT returned empty result; returning 204")
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        logger.info("User said: %r", user_text)

        return await _process_utterance(user_text, device_id, voice or x_tts_voice, http_session)

    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# POST /chat  — text-first pipeline (Android Auto / Google Assistant App Actions)
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    text: str
    device_id: str = "default"
    voice: str | None = None


@app.post("/chat")
async def chat(
    request: Request,
    body: ChatRequest,
    authorization: str | None = Header(default=None),
) -> Response:
    """
    Text-first pipeline — skips Whisper STT:
      text → Claude loop → Kokoro TTS → WebSocket broadcast

    Used by Android Auto: Google Assistant provides transcription via an App
    Action intent; the Android background service posts the extracted text here
    instead of sending raw audio to /transcribe.

    Returns 202 Accepted (TTS broadcast via WebSocket) or 204 No Content
    for silent turns (music control commands).
    """
    _check_bearer(authorization)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    http_session: aiohttp.ClientSession = request.app.state.http_session
    device_id = body.device_id or "default"

    logger.info("Chat request from device %s: %r", device_id, text[:120])
    await _broadcast({"state": "thinking", "device_id": device_id}, device_id)

    return await _process_utterance(text, device_id, body.voice, http_session)


# ---------------------------------------------------------------------------
# GET /voices  — Kokoro voice catalog proxy
# ---------------------------------------------------------------------------

@app.get("/voices")
async def voices(
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Proxy Kokoro's /v1/audio/voices catalog so clients have a single endpoint."""
    _check_bearer(authorization)
    http_session: aiohttp.ClientSession = request.app.state.http_session
    catalog = await tts.list_voices(http_session)
    if catalog is None:
        return JSONResponse({"error": "voice catalog unavailable"}, status_code=502)
    return JSONResponse(catalog)


# ---------------------------------------------------------------------------
# POST /tts-preview  — audition a voice with a short sample
# ---------------------------------------------------------------------------

class TtsPreviewRequest(BaseModel):
    voice: str
    text: str = "Hello, I'm Pandabot. This is what I sound like."


@app.post("/tts-preview")
async def tts_preview(
    request: Request,
    body: TtsPreviewRequest,
    authorization: str | None = Header(default=None),
) -> Response:
    """Synthesize a short audition clip in the requested voice."""
    _check_bearer(authorization)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    if len(text) > 200:
        raise HTTPException(status_code=400, detail="text exceeds 200 chars")
    http_session: aiohttp.ClientSession = request.app.state.http_session
    mp3_bytes = await tts.synthesize(text, http_session, voice=body.voice)
    if not mp3_bytes:
        return Response(status_code=status.HTTP_502_BAD_GATEWAY)
    return Response(content=mp3_bytes, media_type="audio/mpeg")


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
            try:
                msg_text = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            try:
                msg = json.loads(msg_text)
                if isinstance(msg, dict) and msg.get("type") == "cast_devices":
                    devs = msg.get("devices", [])
                    if isinstance(devs, list):
                        _cast_devices[device_id] = [str(d) for d in devs if d]
                        logger.debug("Cast devices for %s: %s", device_id, _cast_devices[device_id])
            except Exception:
                pass
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
# POST /play_radio  — push a radio stream URL to connected Flutter clients
# ---------------------------------------------------------------------------

class PlayRadioPayload(BaseModel):
    station: str
    url: str
    cast_target: str | None = None
    device_id: str | None = None


@app.post("/play_radio")
async def play_radio_endpoint(
    payload: PlayRadioPayload,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    _check_bearer(authorization)
    event: dict = {"type": "play_radio", "station": payload.station, "url": payload.url}
    if payload.cast_target:
        event["cast_target"] = payload.cast_target
    await _broadcast(event, payload.device_id)
    logger.info("/play_radio: station=%r cast_target=%r", payload.station, payload.cast_target)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# GET /cast_devices  — list Cast devices reported by connected Flutter clients
# ---------------------------------------------------------------------------

@app.get("/cast_devices")
async def cast_devices_endpoint(
    authorization: str | None = Header(default=None),
    device_id: str | None = None,
) -> JSONResponse:
    """Return the union of Cast devices reported by all (or one) Flutter client."""
    _check_bearer(authorization)
    if device_id:
        devices = list(_cast_devices.get(device_id, []))
    else:
        seen: set[str] = set()
        devices = []
        for devs in _cast_devices.values():
            for d in devs:
                if d not in seen:
                    seen.add(d)
                    devices.append(d)
    return JSONResponse({"devices": devices})


# ---------------------------------------------------------------------------
# POST /stop_radio  — stop radio playback on connected Flutter clients
# ---------------------------------------------------------------------------

class StopRadioPayload(BaseModel):
    device_id: str | None = None


@app.post("/stop_radio")
async def stop_radio_endpoint(
    payload: StopRadioPayload = StopRadioPayload(),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    _check_bearer(authorization)
    await _broadcast({"type": "stop_radio"}, payload.device_id)
    logger.info("/stop_radio broadcast")
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# POST /speak  — synthesise text and push audio to connected Flutter clients
# ---------------------------------------------------------------------------

class SpeakPayload(BaseModel):
    text: str
    device_id: str | None = None
    voice: str | None = None


@app.post("/speak")
async def speak(
    request: Request,
    payload: SpeakPayload,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """
    Synthesise text via TTS and broadcast the resulting MP3 to all connected
    Flutter clients as a {type: 'speak', text, audio_b64} WebSocket event.
    Called by the Discord bot when a non-LLM notification fires.
    """
    _check_bearer(authorization)
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    http_session: aiohttp.ClientSession = request.app.state.http_session
    mp3_bytes = await tts.synthesize(text, http_session, voice=payload.voice)
    if not mp3_bytes:
        logger.warning("/speak: TTS returned no bytes for text %r", text[:80])
        return JSONResponse({"ok": False, "error": "TTS failed"}, status_code=502)

    audio_b64 = base64.b64encode(mp3_bytes).decode()
    clients_before = _total_connections()
    await _broadcast(
        {"type": "speak", "text": text, "audio_b64": audio_b64, "device_id": payload.device_id},
        payload.device_id,
    )
    logger.info(
        "/speak: %d chars → %d bytes audio → %d client(s)",
        len(text), len(mp3_bytes), clients_before,
    )
    return JSONResponse({"ok": True, "clients_notified": clients_before})


# ---------------------------------------------------------------------------
# POST /debug/inject  — text injection for automated UI/Cast testing
# ---------------------------------------------------------------------------

class InjectRequest(BaseModel):
    text: str
    device_id: str = "pandabot-terminal-1"


@app.post("/debug/inject")
async def debug_inject(
    request: Request,
    body: InjectRequest,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """
    Inject a text command without TTS — for automated UI/Cast testing.
    Runs the full Claude loop and broadcasts envelopes to connected WebSocket
    clients but does not synthesize or broadcast audio.

    Returns {response_text, envelopes, silent_tts}.
    """
    _check_bearer(authorization)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    http_session: aiohttp.ClientSession = request.app.state.http_session
    device_id = body.device_id or "default"

    logger.info("Debug inject from device %s: %r", device_id, text[:120])

    pending_envelopes: list[dict] = []
    voice_ctx = {
        'emit': pending_envelopes.append,
        'silent_tts': False,
    }

    await _broadcast({"state": "thinking", "device_id": device_id}, device_id)

    intent = _try_music_intent(text)
    if intent is not None:
        _, tool_name, tool_kwargs, spoken = intent
        logger.info("Music intent shortcut: %s%s -> %s", tool_name, tool_kwargs, spoken)
        set_voice_context(voice_ctx)
        try:
            execute_tool(tool_name, tool_kwargs)
        finally:
            set_voice_context(None)
        response_text = spoken
    else:
        history = session_manager.get_history(device_id)
        cast_devs = _cast_devices.get(device_id, [])
        effective_prompt = system_prompt
        if cast_devs:
            names = ", ".join(f'"{d}"' for d in cast_devs)
            effective_prompt += (
                f"\n\nChromecast devices currently visible on the network: {names}.\n"
                "When the user asks to cast music to a device:\n"
                "- Match their utterance to one of the names above (fuzzy match).\n"
                "- If confident of the match, call play_music with cast_target set "
                "to the EXACT device name from the list above.\n"
                "- If ambiguous between multiple devices, list the available device "
                "names and ask the user to be more specific — do not call play_music yet.\n"
                "- If the user asks to stop casting, call stop_music.\n"
            )

        def _run_loop_with_ctx():
            set_voice_context(voice_ctx)
            try:
                return run_claude_loop(
                    text,
                    history,
                    TOOL_DEFINITIONS,
                    execute_tool,
                    effective_prompt,
                )
            finally:
                set_voice_context(None)

        loop = asyncio.get_event_loop()
        response_text: str = await loop.run_in_executor(None, _run_loop_with_ctx)

    logger.info(
        "Inject response: %r  envelopes=%d",
        response_text[:120], len(pending_envelopes),
    )

    if not voice_ctx['silent_tts']:
        session_manager.add_turn(device_id, text, response_text)

    await _broadcast(
        {"type": "turn", "user_text": text, "assistant_text": response_text},
        device_id,
    )

    for env in pending_envelopes:
        env.setdefault("device_id", device_id)
        await _broadcast(env, device_id)

    asyncio.create_task(discord_mirror.post_turn(text, response_text, http_session))
    asyncio.create_task(_broadcast_idle_after_delay(device_id, delay=0.2))

    return JSONResponse({
        "response_text": response_text,
        "envelopes": pending_envelopes,
        "silent_tts": voice_ctx['silent_tts'],
    })


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
