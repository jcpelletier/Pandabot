"""
Panda Discord bot — server status assistant + Jenkins failure notifier.

Responds to all messages (and DMs) by querying Claude with a curated set of
read-only server tools.  Also runs a local-only HTTP webhook that Jenkins
(and other scripts) POST to for failure alerts.

Environment variables (see .env.example):
  DISCORD_TOKEN          — Discord bot token
  DISCORD_CHANNEL_ID     — Default channel ID for notifications
  ANTHROPIC_API_KEY      — Claude API key
  JENKINS_URL            — Jenkins base URL (default http://localhost:8080)
  JENKINS_USER           — Jenkins API user
  JENKINS_TOKEN          — Jenkins API token
  WEBHOOK_PORT           — Port for the local notification webhook (default 8765)
  WEBHOOK_SECRET         — Shared secret Jenkins must send (optional but recommended)
"""

import asyncio
import concurrent.futures
import datetime
import io
import logging
import os
import random
import re
import struct
import subprocess
import textwrap
import threading
import uuid
import wave

import aiohttp
from aiohttp import web
import discord
from discord.ext import commands

# pandabot-core infrastructure — replaces local llm_usage, llm_provider, scheduler copies
from pandabot_core.llm import usage as llm_usage
from pandabot_core.llm import provider as llm_provider
from pandabot_core.llm.provider import get_provider, get_provider_name
from pandabot_core.llm.loop import run_claude_loop as _run_claude_loop_core
from pandabot_core.telemetry import ai_event as _ai_event, ai_trace as _ai_trace
from pandabot_core.discord_comms import make_model_switch_cog as _make_model_switch_cog
from pandabot_core.discord_comms import make_help_cog as _make_help_cog
from pandabot_core.discord_comms import (
    keep_typing, split_message, send_with_retry as _send_with_retry,
    build_history as _build_history, ConfirmationManager, model_switch_banner,
    make_confirmation_view, announce_startup as _announce_startup,
)
from pandabot_core import identity as _identity
from pandabot_core import scheduler  # used in fire_scheduled_task and task_scheduler
from pandabot_core.channels import (
    BotChannelMap, make_message_bot_tool, send_to_bot_threadsafe,
)

from tools import TOOL_DEFINITIONS, execute_tool, ENABLE_LOCAL_LLM, ENABLE_FAMILY, FAMILY_SPREADSHEET_ID  # noqa: E402

# Inter-bot messaging (channel-as-inbox). Lets Pandabot relay requests to its
# siblings — e.g. ask PandaBot-Dev to start a goal, or PandaBot-Devops for infra.
_BOT_NAME = os.environ.get("BOT_NAME", "pandabot")
_CHANNEL_MAP = BotChannelMap.from_env()
_TOOL_DEFINITIONS = [*TOOL_DEFINITIONS]
if _CHANNEL_MAP:
    _TOOL_DEFINITIONS.append(make_message_bot_tool(_CHANNEL_MAP))
import llama_manager

# ---------------------------------------------------------------------------
# Pending-confirmation state
# ---------------------------------------------------------------------------
_confirmations = ConfirmationManager()

# ---------------------------------------------------------------------------
# Voice / TTS state
# ---------------------------------------------------------------------------
# Maps guild_id → VoiceClient (populated by !join, cleared by !leave / idle)
_voice_clients: dict[int, discord.VoiceClient] = {}
# Monotonic timestamp of the last audio play per guild (for idle timeout)
_voice_last_play: dict[int, float] = {}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
log = logging.getLogger("panda-bot")

# Temporary: expose MLS/DAVE debug output from discord internals so we can
# diagnose why the MLS key exchange isn't completing.
class _DaveFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        return any(k in msg for k in ('MLS', 'DAVE', 'dave', 'binary frame', 'epoch', 'welcome', 'commit', 'proposal'))
_dave_handler = logging.StreamHandler()
_dave_handler.setLevel(logging.DEBUG)
_dave_handler.addFilter(_DaveFilter())
_dave_handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"))
for _n in ('discord.gateway', 'discord.voice_state'):
    _l = logging.getLogger(_n)
    _l.setLevel(logging.DEBUG)
    _l.addHandler(_dave_handler)

# ---------------------------------------------------------------------------
# DAVE MLS deduplication — applied at class level before any voice connects.
#
# Root cause: two concurrent WebSocket connections share the same
# VoiceConnectionState and DaveSession. Both WS receive loops deliver the
# same MLS binary frames within the same millisecond. The affected methods:
#
#   • reinit_dave_session — both fire on SESSION_DESCRIPTION (op 4); each
#     sends a key package, Discord sends two MLS welcome responses.
#   • process_proposals — both WS call it and each sends a CommitWelcome;
#     two commits in flight causes one to be rejected on the other side.
#   • process_commit — both WS call it; the second call fails because the
#     commit was already applied, triggering _recover_from_invalid_commit →
#     reinit_dave_session, which resets the entire DAVE session.
#   • process_welcome — both WS call it; second call corrupts MLS state.
#
# Fix: deduplicate each method on (session_id, payload_hash) within 1 s.
# process_proposals returns None when skipped (no CommitWelcome → no second
# commit sent to Discord). All other methods return None on skip too (void).
# ---------------------------------------------------------------------------
import hashlib as _hashlib
import time as _time_module

try:
    import discord.voice_state as _dvs

    _orig_reinit_dave_session = _dvs.VoiceConnectionState.reinit_dave_session
    _reinit_last_call: dict[int, float] = {}
    _REINIT_DEBOUNCE_SECS = 1.0

    async def _debounced_reinit_dave_session(self) -> None:
        now = _time_module.monotonic()
        obj_id = id(self)
        elapsed = now - _reinit_last_call.get(obj_id, 0.0)
        if elapsed < _REINIT_DEBOUNCE_SECS:
            log.info(
                "DAVE: debounced duplicate reinit_dave_session (%.0f ms since last — skipping)",
                elapsed * 1000,
            )
            return
        _reinit_last_call[obj_id] = now
        log.info("DAVE: reinit_dave_session proceeding (%.0f ms since last)", elapsed * 1000)
        await _orig_reinit_dave_session(self)

    _dvs.VoiceConnectionState.reinit_dave_session = _debounced_reinit_dave_session
except Exception:
    pass  # discord.voice_state unavailable in test environment

try:
    import davey as _davey

    _mls_last: dict[tuple[int, str], tuple[bytes, float]] = {}  # (id(session), method) -> (hash, time)
    _MLS_DEDUP_SECS = 1.0

    def _mls_is_duplicate(session_id: int, method: str, payload: bytes) -> bool:
        now = _time_module.monotonic()
        h = _hashlib.sha256(payload).digest()
        key = (session_id, method)
        last = _mls_last.get(key)
        if last is not None:
            last_h, last_t = last
            if h == last_h and now - last_t < _MLS_DEDUP_SECS:
                log.info("DAVE: skipping duplicate %s (%.0f ms since last)", method, (now - last_t) * 1000)
                return True
        _mls_last[key] = (h, now)
        log.info("DAVE: %s proceeding", method)
        return False

    _orig_process_proposals = _davey.DaveSession.process_proposals
    _orig_process_commit = _davey.DaveSession.process_commit
    _orig_process_welcome = _davey.DaveSession.process_welcome

    def _deduped_process_proposals(self, optype, payload: bytes):
        # optype is a Rust enum; encode via repr to avoid TypeError from bytes()
        key = repr(optype).encode() + b"|" + payload
        if _mls_is_duplicate(id(self), "process_proposals", key):
            return None
        return _orig_process_proposals(self, optype, payload)

    def _deduped_process_commit(self, payload: bytes) -> None:
        if _mls_is_duplicate(id(self), "process_commit", payload):
            return
        _orig_process_commit(self, payload)

    def _deduped_process_welcome(self, payload: bytes) -> None:
        if _mls_is_duplicate(id(self), "process_welcome", payload):
            return
        _orig_process_welcome(self, payload)

    _davey.DaveSession.process_proposals = _deduped_process_proposals
    _davey.DaveSession.process_commit = _deduped_process_commit
    _davey.DaveSession.process_welcome = _deduped_process_welcome
except Exception:
    pass  # davey unavailable in test environment

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_VERSION_FILE = os.path.join(os.path.dirname(__file__), "VERSION")
BOT_VERSION = int(open(_VERSION_FILE).read().strip()) if os.path.exists(_VERSION_FILE) else 0

DISCORD_TOKEN              = os.environ["DISCORD_TOKEN"]
DISCORD_CHANNEL_ID         = int(os.environ["DISCORD_CHANNEL_ID"])
# Bot user IDs allowed to send prompts (e.g. PandaQA). Comma-separated.
TRUSTED_BOT_IDS            = {int(x) for x in os.environ.get("TRUSTED_BOT_IDS", "").split(",") if x.strip()}
ANTHROPIC_API_KEY          = os.environ.get("ANTHROPIC_API_KEY", "")  # required only when LLM_PROVIDER=anthropic
WEBHOOK_PORT               = int(os.environ.get("WEBHOOK_PORT", "8765"))
WEBHOOK_SECRET             = os.environ.get("WEBHOOK_SECRET", "")
TAILSCALE_IP               = os.environ.get("TAILSCALE_IP", "")
DISK_ALERT_THRESHOLD_PCT   = int(os.environ.get("DISK_ALERT_THRESHOLD_PCT", "85"))
DISK_ALERT_PATH            = os.environ.get("DISK_ALERT_PATH", "/mnt/media")
WATCHDOG_SERVICES          = [s.strip() for s in os.environ.get("WATCHDOG_SERVICES", "jellyfin,sunshine").split(",") if s.strip()]
AI_IKEY                    = os.environ.get("APPINSIGHTS_IKEY", "")

# Bot identity + server description
BOT_NAME             = os.environ.get("BOT_NAME",   "Panda")
BOT_EMOJI            = os.environ.get("BOT_EMOJI",  "🐼")
TZ_NAME              = os.environ.get("TZ_NAME",    "America/New_York (Eastern Time, EDT/EST)")
SERVER_DESCRIPTION   = os.environ.get("SERVER_DESCRIPTION",  "")
HARDWARE_DESCRIPTION = os.environ.get("HARDWARE_DESCRIPTION",
                           "NVIDIA GTX 970 (4 GB VRAM), 2 TB NTFS HDD at /mnt/media")
# Operator connection context — included in system prompt when set.
# Example: "wsl ssh -i ~/.ssh/id_ed25519 genesis@192.168.1.100"
OPERATOR_SSH_CMD     = os.environ.get("OPERATOR_SSH_CMD", "")
# Discord user ID to @ping when a scheduled task posts a terminal result.
SCHEDULED_TASK_PING_USER_ID = os.environ.get("SCHEDULED_TASK_PING_USER_ID", "")
AI_ENDPOINT                = os.environ.get("APPINSIGHTS_ENDPOINT", "")

# TTS
ENABLE_TTS               = os.environ.get("ENABLE_TTS", "false").lower() == "true"
TTS_URL                  = os.environ.get("TTS_URL", "http://localhost:8880")
TTS_VOICE                = os.environ.get("TTS_VOICE", "af_heart")
TTS_IDLE_TIMEOUT         = int(os.environ.get("TTS_IDLE_TIMEOUT_SECS", "300"))
TTS_AUTO_JOIN_CHANNEL_ID = int(os.environ["TTS_AUTO_JOIN_CHANNEL_ID"]) if os.environ.get("TTS_AUTO_JOIN_CHANNEL_ID") else None
TTS_TRIGGER_BOT_IDS      = {int(x) for x in os.environ.get("TTS_TRIGGER_BOT_IDS", "").split(",") if x.strip()}
ENABLE_KOKORO_IDLE       = os.environ.get("ENABLE_KOKORO_IDLE", "false").lower() == "true"

# Voice gateway (Flutter app notifications)
VOICE_GATEWAY_URL   = os.environ.get("VOICE_GATEWAY_URL", "http://127.0.0.1:8900")
VOICE_GATEWAY_TOKEN = os.environ.get("VOICE_GATEWAY_TOKEN", "")

if ENABLE_KOKORO_IDLE:
    import kokoro_manager

ENABLE_STT          = os.environ.get("ENABLE_STT", "false").lower() == "true"
STT_URL             = os.environ.get("STT_URL", "http://localhost:8001")
STT_MODEL           = os.environ.get("STT_MODEL", "medium")
STT_SILENCE_TIMEOUT = float(os.environ.get("STT_SILENCE_TIMEOUT_SECS", "1.5"))
STT_RMS_THRESHOLD   = int(os.environ.get("STT_RMS_THRESHOLD", "500"))

# Local LLM (llama.cpp) — active profile name that routes to llama-server
LLAMA_PROFILE_NAME  = os.environ.get("LOCAL_LLM_PROFILE_NAME", "qwen")

def _build_system_prompt() -> str:
    """Delegate to pandabot_core.identity, injecting Pandabot-specific sections."""
    from tools import ENABLE_JENKINS
    _p = get_provider()
    llm_line = f"You are powered by {_p.primary_model} (provider: {get_provider_name()})."

    jenkins_instructions = ""
    if ENABLE_JENKINS:
        jenkins_instructions = textwrap.dedent("""\

        When the user asks to run or trigger a Jenkins job:
          1. Call trigger_jenkins_job to start it.
          2. Immediately call manage_schedule(action='create') to schedule a
             condition_check follow-up — do this in the same response, not as a
             separate step. Use the timing hints from the trigger response.
             tool_calls: [get_jenkins_build_status for that job]
             condition_pattern: '"result":\\s*"(SUCCESS|FAILURE|UNSTABLE|ABORTED)"'
             generative_prompt: summarise the result in 1-2 sentences from {{results}}
          3. Tell the user the job is running and that you'll notify them when done.

        When the user asks to change or view a Jenkins job schedule:
          - Call set_jenkins_schedule with no schedule to view current schedule.
          - Call set_jenkins_schedule with schedule + confirmed=false to preview the
            change and ask the user to confirm.
          - Only call with confirmed=true after the user explicitly replies 'yes'.

        When the user asks to move, rename, or delete files in the media library:
          - Always call manage_files with confirmed=false first to show a preview.
          - Present the preview to the user and ask them to reply yes to confirm.
          - Do NOT call manage_files with confirmed=true yourself — the bot handles
            confirmed execution directly when the user replies yes.
        """)

    pandabot_extras = [
        "For any question about what movies are in the library -- including genre "
        "or mood recommendations (stoner, horror, 80s, feel-good, etc.) -- call "
        "query_jellyfin(search_movies). It returns Jellyfin metadata: genres, "
        "ratings, and plot summaries for every movie. Only use query_media_library "
        "when the user specifically needs filesystem details like file size, codec, "
        "or bitrate.",
        "",
        "CRITICAL -- cross-verification rule: query_jellyfin is the AUTHORITY "
        "on what movies exist in the library. If query_jellyfin(search_movies) "
        "says a movie is NOT in the library, trust it. If you later find matching "
        "filenames via query_media_library(find_files), you MUST call "
        "query_media_library(file_info) on at least one result to verify it is "
        "actually a playable video file before reporting it as a movie. "
        "Non-video files (ROMs, images, subtitles, game assets) can have names "
        "that look like movies but are not playable content -- the [OTHER] tag "
        "on a find_files result means it is NOT a video file.",
        "",
        "CRITICAL -- family information rule: when the user asks anything about "
        "a specific person (their relationship, birthday, contact info, parents, "
        "children, or any personal detail), you MUST call query_family_info first. "
        "Never answer questions about people from training data or memory -- you "
        "are a tool-use assistant, not a trivia bot, and guessing personal details "
        "is always wrong. If query_family_info returns no result, say so.",
        "",
        "When asked to tell a joke, story, riddle, poem, song lyric, or to give "
        "creative examples: do NOT default to your most common training-prior "
        "choices (programmer jokes, panda puns, dad jokes about the obvious topic). "
        "Each user turn arrives with a [Variety seed: ...] hint — treat the two "
        "words as a topic, mood, or imagery nudge to push you away from your "
        "default and toward an unexpected angle, format, or subject. The user "
        "cannot see the seed; do not mention it in your reply.",
    ]

    return _identity.build_system_prompt(
        llm_line=llm_line,
        jenkins_instructions=jenkins_instructions,
        extra_sections=pandabot_extras,
    )

DISCORD_MSG_LIMIT = 1900  # leave headroom below the 2000-char limit

# ---------------------------------------------------------------------------
# Discord bot
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

if ENABLE_TTS or ENABLE_STT:
    try:
        discord.opus.load_opus("libopus.so.0")
        logging.getLogger("panda-bot").info("libopus loaded")
    except Exception as _opus_err:
        logging.getLogger("panda-bot").warning("Could not load libopus: %s", _opus_err)


# split_message and send_with_retry are now from pandabot_core.discord_comms
send_with_retry = _send_with_retry


def _calc_rms(data: bytes) -> float:
    """Return RMS amplitude of raw 16-bit LE PCM bytes (0–32767 scale)."""
    n = len(data) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack(f"<{n}h", data[: n * 2])
    return (sum(s * s for s in samples) / n) ** 0.5


def _normalize_audio(samples: "np.ndarray", target_rms: float = 0.25) -> "np.ndarray":
    """Normalize audio to a target RMS level (pure linear scaling, no soft-clip).

    RMS-based normalization preserves the speech-to-noise ratio better than
    peak-based normalization.  When audio is mostly quiet with occasional loud
    transients (like Discord Opus output), peak normalization amplifies the
    noise floor along with everything else, making Whisper's job harder.

    Pure linear scaling only (no tanh soft-clip).  Testing with large-v3 showed
    that removing the tanh soft-clip changed the transcription result from
    "Thanks for watching!" (hallucination) to "Thank you." (closer to speech),
    with no_speech_prob dropping from 0.696 to 0.676 at RMS=0.3.  The soft-clip
    was distorting the audio in a way that pushed Whisper toward its
    hallucination mode.

    The higher target RMS (0.25 vs 0.12) brings quiet Opus-decoded speech
    further into Whisper's effective input range.

    Silent audio (RMS < 0.001) is returned as-is.
    """
    import numpy as np
    rms = float(np.sqrt(np.mean(samples ** 2)))
    if rms < 0.001:
        return samples
    gain = target_rms / rms
    return samples * gain


def _pcm_to_wav(pcm: bytes, sample_rate: int = 48000, channels: int = 2) -> bytes:
    """Wrap raw PCM bytes in a WAV container (in memory)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)       # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


# discord-ext-voice-recv provides voice receiving for discord.py (not in stdlib discord.py)
try:
    from discord.ext import voice_recv as _vr
    _VoiceRecvClient = _vr.VoiceRecvClient
    _AudioSinkBase   = _vr.AudioSink
except ImportError:
    _vr              = None
    _VoiceRecvClient = discord.VoiceClient
    _AudioSinkBase   = object


class STTSink(_AudioSinkBase):
    """Buffers per-user PCM audio, fires STT transcription after silence.

    Improvements over the original implementation:
      - Adaptive RMS threshold: dynamically adjusts based on observed noise floor
      - Minimum utterance duration raised to 0.6s (was 0.4s) to avoid transcribing
        short clicks/pops
      - Logs per-utterance stats (duration, peak RMS, frame count) for debugging
    """

    SAMPLE_RATE  = 48000
    CHANNELS     = 2
    SAMPLE_WIDTH = 2    # bytes (16-bit)
    MIN_SECS     = 0.6  # discard clips shorter than this (raised from 0.4)

    def __init__(self, guild_id: int, *, loopback_mode: bool = False,
                 suppress_transcribe: bool = False):
        if _AudioSinkBase is not object:
            super().__init__()
        self.guild_id = guild_id
        self._buffers: dict[int, bytearray] = {}
        self._timers: dict[int, threading.Timer] = {}
        self._decoders: dict[int, discord.opus.Decoder] = {}
        self._lock = threading.Lock()
        # Adaptive noise floor tracking — per-user running average of RMS during silence
        self._noise_floor: dict[int, float] = {}
        self._noise_samples: dict[int, int] = {}
        # Stats for the current utterance (reset on each new utterance start)
        self._utt_stats: dict[int, dict] = {}
        # Packet filenames that contributed to each utterance — for correlating saved .bin
        # packets with the final debug WAV (simultaneous packet+WAV capture).
        # SSRC tracking: maps SSRC → user_id for auditing which audio stream we're receiving.
        # Logged on first packet and at silence/flush for SSRC mapping audit.
        self._ssrc_map: dict[int, int] = {}  # ssrc → user_id
        # Loopback test mode: when True, allow capturing the bot's own audio playback
        # so !test_audio can verify end-to-end audio reception.
        self.loopback_mode = loopback_mode
        # When True, skip the _on_stt_transcript call so loopback audio doesn't
        # trigger a hallucinated Whisper transcription + Claude reply.
        self._suppress_transcribe = suppress_transcribe

    def wants_opus(self) -> bool:
        # Must be True — voice_recv's internal decoder crashes on first bad Opus packet,
        # killing the router thread permanently. We decode per-packet ourselves instead.
        return True

    def write(self, user, data) -> None:
        # user is None when voice_recv hasn't mapped the SSRC to a member yet
        # (race: audio arrives before the SPEAKING gateway event). Fall back to
        # SSRC lookup so we don't silently drop the first burst of speech.
        if user is None:
            pkt0 = getattr(data, "packet", None)
            raw_ssrc = getattr(pkt0, "ssrc", None)
            if raw_ssrc is not None:
                vc0 = _voice_clients.get(self.guild_id)
                uid0 = getattr(vc0, "_ssrc_to_id", {}).get(raw_ssrc) if vc0 else None
                if uid0 is not None:
                    user = (vc0.guild.get_member(uid0) or vc0.client.get_user(uid0)) if vc0 else None
            if user is None:
                return
        uid = user.id if hasattr(user, "id") else int(user)
        if bot.user and uid == bot.user.id and not self.loopback_mode:
            return

        packet     = getattr(data, "packet", None)
        opus_bytes = getattr(packet, "decrypted_data", None) or getattr(data, "opus", None)

        # Determine packet type and SSRC for decode failure logging and SSRC tracking
        pkt_type = "UNKNOWN"
        seq = -1
        ts = -1
        ssrc = -1
        if packet is not None:
            pkt_cls = type(packet).__name__
            if pkt_cls == "SilencePacket":
                pkt_type = "SILENCE"
            elif pkt_cls == "FakePacket":
                pkt_type = "FAKE"
            elif pkt_cls == "RTPPacket":
                pkt_type = "RTP"
                # RTP header: first 12 bytes; seq=bytes 2-3, timestamp=bytes 4-7, SSRC=bytes 8-11
                hdr = getattr(packet, "header", None)
                if hdr and len(hdr) >= 12:
                    seq  = (hdr[2] << 8) | hdr[3]
                    ts   = (hdr[4] << 24) | (hdr[5] << 16) | (hdr[6] << 8) | hdr[7]
                    ssrc = (hdr[8] << 24) | (hdr[9] << 16) | (hdr[10] << 8) | hdr[11]
            else:
                pkt_type = pkt_cls

        if not opus_bytes:
            return

        # DAVE E2E decryption — mandatory since Discord made it non-optional.
        # discord.py handles the MLS key exchange in the voice WebSocket; we just
        # need to call decrypt() here once the session is ready.
        # If dave_session exists, we must decrypt — passing ciphertext to the Opus
        # decoder produces garbage PCM and causes Whisper hallucinations.
        vc = _voice_clients.get(self.guild_id)
        dave_session = getattr(getattr(vc, '_connection', None), 'dave_session', None)
        if dave_session is not None:
            if not dave_session.ready:
                # MLS handshake not complete yet — drop rather than pass ciphertext
                log.debug("STT: DAVE not ready — dropping pkt user=%s", uid)
                return
            if not dave_session.can_passthrough(uid):
                try:
                    import davey as _davey
                    opus_bytes = dave_session.decrypt(uid, _davey.MediaType.audio, opus_bytes)
                except Exception as _dave_err:
                    try:
                        _known_uids = dave_session.get_user_ids()
                    except Exception as _ue:
                        _known_uids = f"error:{_ue}"
                    log.warning(
                        "STT: DAVE decrypt failed user=%s session=%d user_ids=%s: %s — dropping",
                        uid, id(dave_session), _known_uids, _dave_err,
                    )
                    return

        try:
            if uid not in self._decoders:
                self._decoders[uid] = discord.opus.Decoder()
            pcm = self._decoders[uid].decode(opus_bytes, fec=False)
        except Exception as exc:
            # Log Opus TOC byte (first byte of compressed data) for diagnostics
            opus_toc = opus_bytes[0] if opus_bytes and len(opus_bytes) > 0 else -1
            opus_config = bin(opus_toc)[2:].zfill(8) if opus_toc >= 0 else "N/A"
            log.warning(
                "STT: decode failed for user %s pkt=%s seq=%d ts=%d "
                "opus_len=%d opus_toc=0x%02x(%s) err=%s",
                uid, pkt_type, seq, ts,
                len(opus_bytes) if opus_bytes else 0,
                opus_toc, opus_config, exc,
            )
            # Reset the decoder to break error propagation.
            # Opus is stateful: one bad packet corrupts the decoder's internal
            # state, causing ALL subsequent packets to fail too.
            # By recreating the decoder, the next valid packet gets a fresh start.
            if uid in self._decoders:
                del self._decoders[uid]
            try:
                self._decoders[uid] = discord.opus.Decoder()
                pcm = self._decoders[uid].decode(opus_bytes, fec=False)
                log.info("STT: decoder reset succeeded for user %s seq=%d opus_len=%d",
                         uid, seq, len(opus_bytes) if opus_bytes else 0)
            except Exception as exc2:
                log.warning("STT: decoder reset STILL failed for user %s seq=%d err=%s",
                            uid, seq, exc2)
                return

        rms = _calc_rms(pcm)

        # Track SSRC-to-user mapping (only for RTP packets with valid SSRC)
        if pkt_type == "RTP" and ssrc > 0:
            existing_uid = self._ssrc_map.get(ssrc)
            if existing_uid is None:
                self._ssrc_map[ssrc] = uid
            elif existing_uid != uid:
                log.warning("STT SSRC CONFLICT: ssrc=%d mapped to user=%s but now seen from user=%s!",
                            ssrc, existing_uid, uid)

        # --- Adaptive noise floor ---
        # Track the noise floor by observing RMS during non-speech frames.
        # Use an exponential moving average with a fast update rate.
        noise_floor = self._noise_floor.get(uid, 0.0)
        noise_count = self._noise_samples.get(uid, 0)

        # Determine if this frame is speech using an adaptive threshold
        # Threshold = max(static STT_RMS_THRESHOLD, noise_floor * 2.5)
        adaptive_threshold = max(STT_RMS_THRESHOLD, noise_floor * 2.5)
        is_speech = rms > adaptive_threshold

        if not is_speech:
            # Update noise floor estimate (exponential moving average)
            if noise_count == 0:
                noise_floor = rms
            else:
                alpha = 0.05  # slow adaptation to avoid reacting to brief noises
                noise_floor = (1 - alpha) * noise_floor + alpha * rms
            self._noise_floor[uid] = noise_floor
            self._noise_samples[uid] = noise_count + 1

        with self._lock:
            in_utterance = uid in self._buffers

            if is_speech:
                # NEW UTTERANCE DETECTED: Reset decoder to clear any accumulated
                # Comfort Noise Generator (CNG) state from the inter-utterance gap.
                #
                # Between utterances, Discord sends SILK NB CNG frames (4.3% of
                # all packets). These write comfort noise parameters into the
                # decoder's internal prediction memory (adaptive codebook, LPC
                # coefficients, pitch synthesis filter state). When the next
                # utterance's CELT NB frames arrive, the decoder has CNG state
                # in its inter-frame prediction, which destroys the pitch
                # harmonic structure (PitchAuto drops from 0.54 -> 0.12-0.18).
                #
                # Recreating the decoder at utterance start gives us a clean
                # slate with zero prediction memory, allowing the CELT NB
                # packets to reconstruct proper pitch structure.
                if not in_utterance and uid in self._decoders:
                    old_decoder = self._decoders.pop(uid, None)
                    log.info("STT: decoder reset for user %s at utterance start (cleared CNG prediction state)",
                             uid)
                # Speech frame: (re)start the silence timer and accumulate
                timer = self._timers.pop(uid, None)
                if timer:
                    timer.cancel()
                self._buffers.setdefault(uid, bytearray()).extend(pcm)

                # Track utterance stats
                if uid not in self._utt_stats:
                    self._utt_stats[uid] = {"frames": 0, "peak_rms": 0.0, "total_rms": 0.0}
                self._utt_stats[uid]["frames"] += 1
                self._utt_stats[uid]["peak_rms"] = max(self._utt_stats[uid]["peak_rms"], rms)
                self._utt_stats[uid]["total_rms"] += rms

                timer = threading.Timer(STT_SILENCE_TIMEOUT, self._on_silence, args=[uid])
                timer.daemon = True
                timer.start()
                self._timers[uid] = timer
            elif in_utterance:
                # Silence frame mid-utterance: keep it so Whisper hears natural pauses
                self._buffers[uid].extend(pcm)
            # silence before any speech → ignore

    def _on_silence(self, user_id: int) -> None:
        """Called from threading.Timer after silence — schedule transcription on the event loop."""
        with self._lock:
            buf = self._buffers.pop(user_id, None)
            self._timers.pop(user_id, None)
            stats = self._utt_stats.pop(user_id, None)
            # Log SSRC-to-user mapping at silence (mapping audit)
            ssrc_reverse = {v: k for k, v in self._ssrc_map.items()}
            user_ssrc = ssrc_reverse.get(user_id, -1)
        if not buf:
            return
        min_bytes = int(self.MIN_SECS * self.SAMPLE_RATE * self.CHANNELS * self.SAMPLE_WIDTH)
        if len(buf) < min_bytes:
            return

        duration = len(buf) / (self.SAMPLE_RATE * self.CHANNELS * self.SAMPLE_WIDTH)
        if stats and stats["frames"] > 0:
            avg_rms = stats["total_rms"] / stats["frames"]
            log.info(
                "STT: silence for user %s ssrc=%s — %.2fs, %d frames, "
                "peak_rms=%.0f avg_rms=%.0f noise_floor=%.0f — transcribing",
                user_id, user_ssrc, duration, stats["frames"],
                stats["peak_rms"], avg_rms,
                self._noise_floor.get(user_id, 0.0),
            )
        else:
            log.info("STT: silence for user %s ssrc=%s, %.2fs — transcribing",
                     user_id, user_ssrc, duration)

        if self._suppress_transcribe:
            log.info("STT: loopback mode — suppressing transcript for user %s", user_id)
            return
        asyncio.run_coroutine_threadsafe(
            _on_stt_transcript(self.guild_id, user_id, bytes(buf)), bot.loop
        )

    def flush(self) -> None:
        """Flush any remaining audio buffers (transcribe what we have without waiting for silence).

        Called during cleanup so no audio is lost when the bot disconnects.
        """
        with self._lock:
            uids = list(self._buffers.keys())
            bufs = {uid: self._buffers.pop(uid) for uid in uids}
            stats_map = {}
            for uid in uids:
                timer = self._timers.pop(uid, None)
                if timer:
                    timer.cancel()
                stats_map[uid] = self._utt_stats.pop(uid, None)
        for uid, buf in bufs.items():
            if not buf:
                continue
            min_bytes = int(self.MIN_SECS * self.SAMPLE_RATE * self.CHANNELS * self.SAMPLE_WIDTH)
            if len(buf) < min_bytes:
                continue
            duration = len(buf) / (self.SAMPLE_RATE * self.CHANNELS * self.SAMPLE_WIDTH)
            log.info("STT: flushing %d bytes (%.2fs) for user %s", len(buf), duration, uid)

            if self._suppress_transcribe:
                log.info("STT: loopback mode — suppressing flush transcription for user %s", uid)
                continue
            asyncio.run_coroutine_threadsafe(
                _on_stt_transcript(self.guild_id, uid, bytes(buf)), bot.loop
            )

    def cleanup(self) -> None:
        self.flush()
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._buffers.clear()
            self._timers.clear()
        self._decoders.clear()
        self._noise_floor.clear()
        self._noise_samples.clear()
        self._utt_stats.clear()


def _start_listening(vc: discord.VoiceClient, guild_id: int, **sink_kwargs) -> None:
    """Attach an STTSink and begin receiving audio.

    Parameters
    ----------
    vc : VoiceClient
        The voice client to attach to.
    guild_id : int
        The guild ID for the sink.
    **sink_kwargs
        Extra keyword arguments forwarded to the STTSink constructor
        (e.g. loopback_mode=True for the !test_audio loopback test).
    """
    if not ENABLE_STT or _vr is None:
        return
    try:
        vc.listen(STTSink(guild_id, **sink_kwargs))
        log.info("STT listening started in guild %s (sink_kwargs=%s)", guild_id, sink_kwargs)
    except Exception as exc:
        log.warning("Could not start STT: %s", exc, exc_info=True)
    _start_dave_diagnostics(vc, guild_id)


def _stop_listening(vc: discord.VoiceClient) -> None:
    """Stop receiving audio and clean up the sink."""
    if not ENABLE_STT or _vr is None:
        return
    try:
        vc.stop_listening()
    except Exception as exc:
        log.warning("Could not stop STT: %s", exc)


def _start_dave_diagnostics(vc: discord.VoiceClient, guild_id: int) -> None:
    """Monkey-patch reinit_dave_session to log each call, and start a periodic
    DAVE session state monitor so we can see when user_ids gets populated."""
    conn = getattr(vc, '_connection', None)
    if conn is None:
        return

    _reinit_count = [0]
    _orig_reinit = getattr(conn, 'reinit_dave_session', None)
    if _orig_reinit is not None:
        async def _logged_reinit(_orig=_orig_reinit, _conn=conn):
            _reinit_count[0] += 1
            ds = _conn.dave_session
            log.info("DAVE: reinit_dave_session #%d ENTER ds=%d ready=%s",
                     _reinit_count[0], id(ds) if ds else -1,
                     ds.ready if ds else None)
            await _orig()
            ds = _conn.dave_session
            try:
                uids = ds.get_user_ids() if ds else []
            except Exception:
                uids = "error"
            log.info("DAVE: reinit_dave_session #%d DONE ds=%d ready=%s user_ids=%s",
                     _reinit_count[0], id(ds) if ds else -1,
                     ds.ready if ds else None, uids)
        conn.reinit_dave_session = _logged_reinit

    async def _monitor():
        for tick in range(1, 16):
            await asyncio.sleep(2)
            ds = getattr(conn, 'dave_session', None)
            if ds is None:
                log.info("DAVE monitor [%ds] guild=%s: no dave_session", tick * 2, guild_id)
                continue
            try:
                uids = ds.get_user_ids()
            except Exception as _e:
                uids = f"error:{_e}"
            log.info("DAVE monitor [%ds] guild=%s ds=%d ready=%s status=%s user_ids=%s",
                     tick * 2, guild_id, id(ds), ds.ready, ds.status, uids)
    asyncio.create_task(_monitor())


_whisper_model = None
_whisper_model_lock = threading.Lock()


_WHISPER_CACHE = "/opt/discord-bot/models"


def _get_whisper_model():
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    with _whisper_model_lock:
        if _whisper_model is None:
            from faster_whisper import WhisperModel
            log.info("Loading Whisper model '%s' (CPU int8)...", STT_MODEL)
            _whisper_model = WhisperModel(
                STT_MODEL,
                device="cpu",
                compute_type="int8",
                download_root=_WHISPER_CACHE,
            )
            log.info("Whisper model ready")
    return _whisper_model


_WHISPER_HALLUCINATIONS = {
    "thanks for watching", "thank you for watching",
    "please like and subscribe", "like and subscribe", "see you next time",
    "see you in the next video", "i'll see you next time", "i'll see you in the next",
    "bye", "goodbye", "you", "thank you", "thanks", "okay", "ok", "um", "uh", "hmm",
    "i don't know", "i don't know what", "i'm sorry", "sorry",
    "please subscribe", "don't forget to subscribe", "hit the like button",
}

def _is_whisper_hallucination(text: str) -> bool:
    """Return True if text is a known Whisper hallucination artifact."""
    normalized = text.lower().strip().rstrip(".!?,")
    # Exact match
    if normalized in _WHISPER_HALLUCINATIONS:
        return True
    # Substring match — catches variants like "I'll see you next time" containing "see you next time"
    for phrase in _WHISPER_HALLUCINATIONS:
        if len(phrase) >= 8 and phrase in normalized:
            return True
    return False


def _transcribe_pcm_sync(pcm_bytes: bytes) -> str | None:
    """Transcribe raw 48kHz stereo 16-bit PCM via faster-whisper; returns text or None.

    Converts PCM → float32 mono 16kHz numpy array and passes it directly to
    model.transcribe(), bypassing the av/ffmpeg WAV conversion path which was
    producing empty segments despite valid audio.

    Pipeline: 48kHz stereo s16 PCM
      1. np reshape+mean  → 48kHz mono float64
      2. Decimate by 3    → 16kHz mono float64  (exact 3:1 ratio; no anti-alias
         needed because CELT NB Opus audio has negligible energy above 8kHz)
      3. Convert          → float32 [-1, 1]
      4. Normalization    → RMS ≈ 0.12 (RMS-based, preserves speech-to-noise ratio)
      5. Whisper          → transcription (VAD disabled; relaxed thresholds)

    NOTE: Uses numpy instead of audioop for steps 1–2.  audioop.ratecv on
    Python 3.12+ has known quality issues (produces spike artifacts and
    stair-step distortion in the C implementation).  Simple numpy indexing
    (mono[::3]) replicates the same decimation without the bugs.
    """
    import numpy as np
    model = _get_whisper_model()
    try:
        # Step 1: Stereo 48kHz s16 → mono 48kHz float64
        #   Replaces audioop.tomono(pcm_bytes, 2, 0.5, 0.5)
        raw = np.frombuffer(pcm_bytes, dtype=np.int16).reshape(-1, 2)
        mono = raw.mean(axis=1, dtype=np.float64)

        # Step 2: 48kHz → 16kHz (exact 3:1 ratio) — simple decimate by 3
        #   Replaces audioop.ratecv(mono_pcm, 2, 1, 48000, 16000, None)
        #
        #   audioop.ratecv for an exact 3:1 ratio just picks every 3rd sample.
        #   We replicate this with numpy indexing (mono[::3]) — no anti-aliasing
        #   filter needed because CELT NB Opus audio has negligible energy above 8kHz
        #   (only 0.2% in 4-8kHz range), so there's nothing to alias.
        #
        #   The previous v100 approach used a 15-tap triangular FIR LPF with -3dB
        #   at ~3200Hz, which destroyed the 2000-4000Hz range (35%→9.2%) — exactly
        #   where CELT NB concentrates consonant/sibilant information that Whisper
        #   needs for phoneme discrimination.
        decimated = mono[::3]

        # Convert to float32 [-1, 1]
        samples = (decimated / 32768.0).astype(np.float32)

        # Step 3: Normalize to target RMS (linear only, no soft-clip) — preserves speech-to-noise ratio
        samples = _normalize_audio(samples, target_rms=0.25)

        duration = len(samples) / 16000
        log.info("Whisper: input %.2fs (%d samples at 16kHz, peak=%.3f, rms=%.3f)",
                 duration, len(samples),
                 float(np.max(np.abs(samples))),
                 float(np.sqrt(np.mean(samples**2))))

        # Debug: save the resampled + normalized audio so we can verify what Whisper is hearing
        try:
            import wave as _wave
            dbg_path = "/opt/discord-bot/stt_debug_latest.wav"
            with _wave.open(dbg_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes((samples * 32767).astype(np.int16).tobytes())
            log.info("Whisper: debug WAV saved to %s (%.2fs)", dbg_path, duration)
        except Exception as _dbg_err:
            log.debug("Debug WAV save failed: %s", _dbg_err)

        # Step 4: Transcribe — NO VAD filter (it was removing all audio due to
        # resampling artifacts).  Whisper's own internal processing handles
        # silence/noise rejection via no_speech_threshold and log_prob_threshold.
        #
        #   initial_prompt: steers Whisper away from YouTube-style hallucinations
        #     ("Thanks for watching!" etc.) that appear when audio looks like noise.
        #     Providing a voice-chat context biases the token probabilities toward
        #     short, conversational speech.
        #
        #   no_speech_threshold: 0.6 (default) — standard Whisper sensitivity.
        #   log_prob_threshold: -2.0 (relaxed) — accept lower-probability text
        #     since Discord Opus audio has lower SNR than typical mic input.
        segments, info = model.transcribe(
            samples,
            language="en",
            beam_size=5,
            vad_filter=False,                    # Disabled — was removing all audio
            condition_on_previous_text=False,    # Avoid compounding errors across utterances
            temperature=0.0,                     # Greedy decoding (most deterministic, fewer hallucinations)
            compression_ratio_threshold=2.4,     # Raised from 2.0 — spectrally narrow audio can look "compressed"
            log_prob_threshold=-2.0,             # Relaxed — accept lower-probability text for quiet audio
            no_speech_threshold=0.6,             # Default — use Whisper's built-in no_speech detector
            initial_prompt="Voice chat transcription.",  # Steers away from YouTube hallucinations
        )
        segs = list(segments)
        # Log per-segment detail including no_speech_prob so we can see why Whisper discards segments
        for i, seg in enumerate(segs):
            nsp = getattr(seg, 'no_speech_prob', None)
            alp = getattr(seg, 'avg_logprob', None)
            log.info(
                "Whisper seg[%d]: text=%r no_speech_prob=%.3f avg_logprob=%.3f",
                i, seg.text[:80],
                nsp if nsp is not None else -1.0,
                alp if alp is not None else 0.0,
            )
        text = " ".join(seg.text for seg in segs).strip()
        log.info("Whisper: segs=%d lang_prob=%.2f text=%r",
                 len(segs), info.language_probability, text[:200])
        if not segs:
            log.info("Whisper: no segments returned — audio likely classified as non-speech (use !hear to diagnose)")
        if _is_whisper_hallucination(text):
            log.info("Whisper: hallucination detected — use !hear to play back what the bot captured")
            return "[Audio reception issue: the user said something but the bot's voice receiver only captured noise/silence. Use **!hear** in the text channel to play back what the bot recorded.]"
        return text or None
    except Exception as exc:
        log.warning("Whisper transcription error: %s", exc, exc_info=True)
        return None


async def _transcribe_audio(pcm_bytes: bytes) -> str | None:
    """Run in-process Whisper transcription in a thread executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _transcribe_pcm_sync, pcm_bytes)


async def _on_stt_transcript(guild_id: int, user_id: int, pcm_bytes: bytes) -> None:
    """Transcribe speech, call Claude, post to text channel, and speak the reply."""
    import time as _time

    # Prevent idle-disconnect while STT pipeline processes (Whisper + Claude ~8-11s)
    _voice_last_play[guild_id] = _time.monotonic()

    transcript = await _transcribe_audio(pcm_bytes)
    if not transcript:
        return

    log.info("STT (user=%s): %.120s", user_id, transcript)

    guild = bot.get_guild(guild_id)
    member = guild.get_member(user_id) if guild else None
    display_name = member.display_name if member else str(user_id)

    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if channel is None:
        log.warning("STT: default channel %s not found", DISCORD_CHANNEL_ID)
        return

    await send_with_retry(channel, f"🎤 **{display_name}:** {transcript}")

    loop = asyncio.get_running_loop()
    try:
        reply = await loop.run_in_executor(
            None, _run_claude_loop, transcript, None, channel.id, None
        )
    except Exception as exc:
        log.exception("LLM query failed for STT input")
        reply = f"Error processing request: {exc}"

    if not reply or not reply.strip():
        log.warning("LLM returned empty reply for STT input — sending fallback")
        reply = "Sorry, I wasn't able to generate a response. Please try again."

    for chunk in split_message(reply):
        if chunk.strip():
            await send_with_retry(channel, chunk)

    if ENABLE_TTS:
        asyncio.create_task(speak_response(guild_id, reply))


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences on . ! ? boundaries, stripping markdown noise."""
    # Strip code fences and Discord formatting that TTS shouldn't read aloud
    clean = re.sub(r"```[\s\S]*?```", "", text)
    clean = re.sub(r"`[^`]+`", "", clean)
    clean = re.sub(r"\*+([^*]+)\*+", r"\1", clean)
    clean = re.sub(r"_([^_]+)_", r"\1", clean)
    clean = re.sub(r"#+\s*", "", clean)
    parts = re.split(r"(?<=[.!?])\s+", clean.strip())
    return [s.strip() for s in parts if len(s.strip()) > 2]


async def _fetch_tts_audio(sentence: str) -> bytes | None:
    """POST a sentence to the Kokoro OpenAI-compatible endpoint; return raw mp3 bytes."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{TTS_URL}/v1/audio/speech",
                json={
                    "model": "kokoro",
                    "input": sentence,
                    "voice": TTS_VOICE,
                    "response_format": "mp3",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    return await resp.read()
                log.warning("TTS API %d for: %.60s", resp.status, sentence)
    except Exception as exc:
        log.warning("TTS fetch error: %s", exc)
    return None


async def speak_response(guild_id: int, text: str) -> None:
    """Synthesize *text* sentence-by-sentence and play it in the guild voice channel.

    All TTS fetches start concurrently so sentence N+1 is ready by the time
    sentence N finishes playing (pipeline / double-buffer effect).
    """
    import time as _time

    vc = _voice_clients.get(guild_id)
    if vc is None or not vc.is_connected():
        log.info("TTS: skipping guild=%s — voice client not connected (vc=%s)", guild_id, vc)
        return

    sentences = _split_sentences(text)
    if not sentences:
        return

    # Kick off all fetches immediately so synthesis overlaps playback
    fetch_tasks = [asyncio.create_task(_fetch_tts_audio(s)) for s in sentences]

    for task in fetch_tasks:
        audio_bytes = await task
        if not audio_bytes:
            continue

        vc = _voice_clients.get(guild_id)
        if vc is None or not vc.is_connected():
            log.info("TTS: aborting playback for guild=%s — voice disconnected mid-stream", guild_id)
            break

        # Wait if the voice client is still finishing the previous sentence
        while vc.is_playing():
            await asyncio.sleep(0.05)

        buf = io.BytesIO(audio_bytes)
        source = discord.FFmpegPCMAudio(buf, pipe=True)

        play_done: asyncio.Future = bot.loop.create_future()

        def _after(err, _f=play_done):
            if _f.done():
                return
            if err:
                bot.loop.call_soon_threadsafe(_f.set_exception, err)
            else:
                bot.loop.call_soon_threadsafe(_f.set_result, None)

        vc.play(source, after=_after)
        _voice_last_play[guild_id] = _time.monotonic()

        try:
            await asyncio.wait_for(asyncio.shield(play_done), timeout=60)
        except (asyncio.TimeoutError, Exception) as exc:
            log.warning("TTS playback error (guild %s): %s", guild_id, exc)
            break


def _run_claude_loop(
    user_message: str,
    history: list[dict] | None = None,
    channel_id: int | None = None,
    conversation_id: str | None = None,
    event_loop=None,
) -> str:
    """Synchronous LLM agentic loop (run in a thread executor).

    Prepends per-turn dynamic context (timestamp + variety seed) so the system
    prompt can stay byte-identical across calls — see CLAUDE.md "Caching strategy".
    """
    user_message = _build_turn_context_prefix() + user_message

    def _on_confirm(ch_id: int, tool_name: str, confirmed_inputs: dict) -> None:
        _confirmations.save(ch_id, tool_name, confirmed_inputs)

    def _execute_tool_with_banner(name: str, inputs: dict) -> str:
        if name == "message_bot":
            if not (event_loop and _CHANNEL_MAP):
                return "message_bot is not available right now."
            return send_to_bot_threadsafe(
                bot, event_loop, _CHANNEL_MAP,
                inputs.get("target", ""), inputs.get("request", ""),
                sender=_BOT_NAME,
            )
        result = execute_tool(name, inputs)
        if name == "switch_model" and event_loop and channel_id and not result.startswith("Unknown"):
            from pandabot_core.llm.provider import get_active_profile_name, get_provider
            from pandabot_core.discord_comms import model_switch_banner
            channel = bot.get_channel(channel_id)
            if channel:
                banner = model_switch_banner(get_active_profile_name(), get_provider().primary_model)
                asyncio.run_coroutine_threadsafe(
                    _send_with_retry(channel, banner), event_loop
                )
        return result

    return _run_claude_loop_core(
        user_message=user_message,
        history=history,
        tool_definitions=_TOOL_DEFINITIONS,
        execute_tool=_execute_tool_with_banner,
        system_prompt=_build_system_prompt(),
        channel_id=channel_id,
        conversation_id=conversation_id,
        on_confirm=_on_confirm,
    )


_FAMILY_PREFETCH_SKIP = frozenset({
    "a", "an", "the", "in", "on", "at", "to", "for", "of", "and", "or",
    "but", "is", "are", "was", "were", "be", "been", "being", "have",
    "has", "had", "do", "does", "did", "will", "would", "shall", "should",
    "may", "might", "must", "can", "could", "i", "you", "he", "she", "we",
    "they", "it", "me", "him", "her", "us", "them", "my", "your", "his",
    "their", "its", "our", "who", "what", "when", "where", "why", "how",
    "ok", "okay", "yes", "no", "not", "please", "thanks", "thank",
    "discord", "jenkins", "jellyfin", "panda", "pandabot", "claude",
    "sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "january", "february", "march", "april", "june", "july", "august",
    "september", "october", "november", "december",
})

def _prefetch_family_context(text: str) -> str | None:
    """
    Extract Title Case names from text, call query_family_info for each, and
    return any hits as a formatted context block. This lets weak local models
    answer family questions without needing to decide to call the tool themselves.
    """
    import re
    # Match 1- or 2-word Title Case sequences
    raw = re.findall(r'\b([A-Z][a-z]{1,}(?:\s+[A-Z][a-z]{1,})?)\b', text)
    seen: set[str] = set()
    results: list[str] = []
    for candidate in raw:
        key = candidate.lower()
        if key in _FAMILY_PREFETCH_SKIP or key in seen:
            continue
        seen.add(key)
        try:
            result = execute_tool("query_family_info", {"person": candidate})
        except Exception:
            continue
        if result and "No family info found" not in result and "not enabled" not in result and "not configured" not in result:
            results.append(result)
    return "\n\n".join(results) if results else None


# Per-turn context: timestamp and a random word pair injected as a leading
# annotation on the user message. Kept OUT of the system prompt so the system
# prompt stays byte-identical across calls and DeepSeek (and other providers)
# can cache the full ~1150-token prefix. See discord-bot/CLAUDE.md "Caching strategy".
_VARIETY_ADJECTIVES = (
    "brittle", "copper", "drowsy", "eldritch", "frosted", "glassy", "hollow",
    "indigo", "jagged", "kinetic", "languid", "marble", "nimble", "opal",
    "plush", "quartz", "rusted", "smoky", "tangled", "velvet",
)
_VARIETY_NOUNS = (
    "bramble", "cinder", "dune", "ember", "fjord", "grotto", "hearth", "ivy",
    "kelp", "lantern", "marsh", "nebula", "orchard", "prism", "quill",
    "rivulet", "snowdrift", "thicket", "umbra", "vellum",
)


def _build_turn_context_prefix() -> str:
    """Per-turn dynamic context. Format kept stable so tests/log greps work."""
    now = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    seed = f"{random.choice(_VARIETY_ADJECTIVES)} {random.choice(_VARIETY_NOUNS)}"
    return f"[Current time: {now}. Variety seed: {seed}.]\n\n"


async def handle_claude_query(user_message: str, message: discord.Message) -> str:
    """Fetch channel history, then dispatch the synchronous Claude loop to a thread."""
    history = await _build_history(message.channel, before=message)
    log.info("Sending %d history messages as context", len(history))
    conv_id = str(uuid.uuid4())

    # Pre-fetch family info for any person names detected in the message and inject
    # as context. This makes family queries reliable even with local models that are
    # weak at function calling — the model gets the data directly instead of needing
    # to decide to invoke the tool.
    if ENABLE_FAMILY and FAMILY_SPREADSHEET_ID:
        loop = asyncio.get_running_loop()
        family_ctx = await loop.run_in_executor(None, _prefetch_family_context, user_message)
        if family_ctx:
            log.info("Injecting pre-fetched family context (%d chars)", len(family_ctx))
            user_message = (
                f"{user_message}\n\n"
                f"[Family info retrieved for names mentioned above — use this data to answer, "
                f"do not call query_family_info again unless you need additional details:]\n"
                f"{family_ctx}"
            )

    # If the active profile is a local llama.cpp model, ensure the right model
    # is loaded before handing off to the LLM loop. The typing indicator is
    # already running, so the switch delay is invisible to the user.
    if ENABLE_LOCAL_LLM:
        active = llm_provider.get_active_profile_name()
        if llama_manager.is_local_profile(active):
            await llama_manager.ensure_model(active)

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _run_claude_loop, user_message, history, message.channel.id, conv_id, loop
    )


@bot.event
async def on_ready():
    log.info("Logged in as %s (id=%s)", bot.user, bot.user.id)


@bot.command(name="join")
async def cmd_join(ctx: commands.Context):
    """Join the voice channel the invoking user is currently in."""
    if ctx.guild is None:
        await ctx.send("Voice commands only work in a server, not DMs.")
        return
    if ctx.author.voice is None:
        await ctx.send("You need to be in a voice channel first.")
        return
    channel = ctx.author.voice.channel
    guild_id = ctx.guild.id
    existing = _voice_clients.get(guild_id)
    if existing and existing.is_connected():
        await existing.move_to(channel)
        vc = existing
    else:
        vc = await channel.connect(cls=_VoiceRecvClient if ENABLE_STT else discord.VoiceClient)
        _voice_clients[guild_id] = vc
    import time as _time
    _voice_last_play[guild_id] = _time.monotonic()
    _start_listening(vc, guild_id)
    await ctx.send(f"Joined **{channel.name}**. I'll speak responses here.")
    log.info("Joined voice channel %s in guild %s", channel.name, guild_id)


@bot.command(name="leave")
async def cmd_leave(ctx: commands.Context):
    """Disconnect from the current voice channel."""
    if ctx.guild is None:
        await ctx.send("Voice commands only work in a server, not DMs.")
        return
    guild_id = ctx.guild.id
    vc = _voice_clients.pop(guild_id, None)
    _voice_last_play.pop(guild_id, None)
    if vc and vc.is_connected():
        _stop_listening(vc)
        await vc.disconnect()
        await ctx.send("Disconnected from voice.")
        log.info("Left voice channel in guild %s", guild_id)
        if ENABLE_KOKORO_IDLE:
            asyncio.create_task(kokoro_manager.ensure_cpu_mode())
    else:
        await ctx.send("I'm not in a voice channel.")


@bot.command(name="test_audio")
async def cmd_test_audio(ctx: commands.Context):
    """Loopback test: play a known sine wave into voice and listen to what comes back.

    Generates a 1-second 440Hz sine wave, plays it into the current voice
    channel while simultaneously capturing it via STTSink (loopback mode).
    The captured WAV and packet manifest are saved for offline analysis.
    """
    if ctx.guild is None:
        await ctx.send("Voice commands only work in a server, not DMs.")
        return
    guild_id = ctx.guild.id
    vc = _voice_clients.get(guild_id)
    if vc is None or not vc.is_connected():
        await ctx.send("I'm not in a voice channel. Use `!join` first.")
        return

    await ctx.send("🔊 **Starting loopback test...** Generating 440Hz sine wave.")

    # --- Generate a 1-second 440Hz sine wave as a temp WAV file ---
    import wave as _wave
    import numpy as np
    import tempfile
    import time as _time
    import os as _os

    SAMPLE_RATE = 48000
    DURATION = 1.0          # seconds
    FREQ = 440.0            # A4 note
    AMPLITUDE = 0.5         # -6dBFS

    t = np.linspace(0, DURATION, int(SAMPLE_RATE * DURATION), endpoint=False)
    # Stereo interleaved 16-bit PCM (same format STTSink uses)
    samples_mono = (AMPLITUDE * np.sin(2 * np.pi * FREQ * t) * 32767).astype(np.int16)
    samples_stereo = np.repeat(samples_mono, 2)  # duplicate to both channels

    tmp_wav = _os.path.join(tempfile.gettempdir(), "panda_test_signal.wav")
    with _wave.open(tmp_wav, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(samples_stereo.tobytes())
    log.info("Test signal WAV saved to %s (%d samples)", tmp_wav, len(samples_stereo))

    # --- Stop existing listening, re-attach in loopback mode ---
    _stop_listening(vc)
    # Give a brief moment for the old sink to flush
    await asyncio.sleep(0.5)

    # Attach a fresh sink in loopback mode (captures bot's own audio, no transcription)
    _start_listening(vc, guild_id, loopback_mode=True, suppress_transcribe=True)

    # --- Play the test signal ---
    source = discord.FFmpegPCMAudio(tmp_wav)
    play_done: asyncio.Future = bot.loop.create_future()

    def _after(err, _f=play_done):
        if _f.done():
            return
        if err:
            bot.loop.call_soon_threadsafe(_f.set_exception, Exception(err))
        else:
            bot.loop.call_soon_threadsafe(_f.set_result, True)

    vc.play(source, after=_after)
    _voice_last_play[guild_id] = _time.monotonic()
    await ctx.send("▶️ Playing 1s 440Hz test tone into voice channel...")

    try:
        await asyncio.wait_for(asyncio.shield(play_done), timeout=30)
        log.info("Test audio playback finished")
    except Exception as exc:
        log.warning("Test audio playback error: %s", exc)
        await ctx.send(f"⚠️ Playback error: {exc}")
        # Still attempt to recover — the sink may have captured something
    finally:
        # Clean up temp file
        try:
            _os.remove(tmp_wav)
        except Exception:
            pass

    # --- Wait for the silence timer to fire and WAV to be saved ---
    await ctx.send("⏳ Waiting for silence detection and WAV save...")
    await asyncio.sleep(STT_SILENCE_TIMEOUT + 2.0)

    # --- Stop loopback listening, restore normal listening ---
    _stop_listening(vc)
    await asyncio.sleep(0.5)
    # Re-attach normal listening (no loopback)
    _start_listening(vc, guild_id)

    # Report results
    await ctx.send(
        "✅ **Loopback test complete!**\n"
        f"- Played: 1s 440Hz sine wave (stereo, -6dBFS)\n"
        f"- Captured WAV: `/opt/discord-bot/stt_raw_pcm.wav`\n"
        f"- Captured packets: `/opt/discord-bot/stt_packets/`\n"
        f"- Packet manifest: `/opt/discord-bot/stt_utterance_packets.json`\n\n"
        "Run `analyze_loopback.py` on the server to compare input vs captured output."
    )
    log.info("Loopback test complete for guild %s", guild_id)


@bot.command(name="hear")
async def cmd_hear(ctx: commands.Context):
    """Play back the last captured raw audio so you can hear what the bot recorded.

    This is the single most important diagnostic tool for the STT pipeline.
    The bot will play the last stt_raw_pcm.wav (48kHz stereo, exactly what
    Opus decoded before resampling) through the voice channel.  If the audio
    sounds like speech → Whisper configuration issue.  If it sounds like noise
    → reception / Discord processing issue.
    """
    if ctx.guild is None:
        await ctx.send("Voice commands only work in a server.")
        return
    guild_id = ctx.guild.id
    vc = _voice_clients.get(guild_id)
    if vc is None or not vc.is_connected():
        await ctx.send("I'm not in a voice channel. Use `!join` first.")
        return

    import os as _os
    raw_path = "/opt/discord-bot/stt_raw_pcm.wav"
    debug_path = "/opt/discord-bot/stt_debug_latest.wav"

    for path, label in [(raw_path, "raw 48kHz capture"), (debug_path, "16kHz Whisper input")]:
        if not _os.path.exists(path):
            continue
        size_kb = _os.path.getsize(path) // 1024
        if vc.is_playing():
            vc.stop()
            import asyncio as _asyncio
            await _asyncio.sleep(0.3)
        source = discord.FFmpegPCMAudio(path)
        play_done: "asyncio.Future[bool]" = bot.loop.create_future()

        def _after(err, _f=play_done):
            if _f.done():
                return
            if err:
                bot.loop.call_soon_threadsafe(_f.set_exception, Exception(err))
            else:
                bot.loop.call_soon_threadsafe(_f.set_result, True)

        vc.play(source, after=_after)
        _voice_last_play[guild_id] = __import__("time").monotonic()
        await ctx.send(f"▶️ Playing **{label}** ({size_kb} KB) — listen and tell me if this sounds like your voice.")
        try:
            import asyncio as _asyncio2
            await _asyncio2.wait_for(_asyncio2.shield(play_done), timeout=30)
        except Exception as exc:
            log.warning("Hear playback error (%s): %s", label, exc)
        import asyncio as _asyncio3
        await _asyncio3.sleep(0.5)

    await ctx.send(
        "Done. Does the audio sound like your voice? Reply with:\n"
        "- `yes` → Audio is speech, Whisper config needs tuning\n"
        "- `no`  → Audio is noise/garbled, reception pipeline still broken"
    )


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Auto-join TTS_AUTO_JOIN_CHANNEL_ID when a user enters; auto-leave when all users leave."""
    if not ENABLE_TTS or TTS_AUTO_JOIN_CHANNEL_ID is None:
        return
    if member.bot and member.id not in TTS_TRIGGER_BOT_IDS:
        return

    guild = member.guild
    guild_id = guild.id
    watch_channel = guild.get_channel(TTS_AUTO_JOIN_CHANNEL_ID)
    if watch_channel is None:
        return

    # A user joined the watched channel
    if after.channel and after.channel.id == TTS_AUTO_JOIN_CHANNEL_ID:
        vc = _voice_clients.get(guild_id)
        if vc is None or not vc.is_connected():
            import time as _time
            vc = await watch_channel.connect(cls=_VoiceRecvClient if ENABLE_STT else discord.VoiceClient)
            _voice_clients[guild_id] = vc
            _voice_last_play[guild_id] = _time.monotonic()
            _start_listening(vc, guild_id)
            log.info("Auto-joined voice channel %s in guild %s", watch_channel.name, guild_id)
            if ENABLE_KOKORO_IDLE:
                asyncio.create_task(_kokoro_warmup(guild_id))
        return

    # A user left the watched channel — disconnect if no humans remain
    if before.channel and before.channel.id == TTS_AUTO_JOIN_CHANNEL_ID:
        vc = _voice_clients.get(guild_id)
        if vc and vc.is_connected():
            human_count = sum(1 for m in before.channel.members if not m.bot)
            if human_count == 0:
                _stop_listening(vc)
                await vc.disconnect()
                _voice_clients.pop(guild_id, None)
                _voice_last_play.pop(guild_id, None)
                log.info("Auto-left voice channel %s in guild %s (no humans remain)", before.channel.name, guild_id)
                if ENABLE_KOKORO_IDLE:
                    asyncio.create_task(kokoro_manager.ensure_cpu_mode())


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot and message.author.id not in TRUSTED_BOT_IDS:
        return

    if message.channel.id != DISCORD_CHANNEL_ID:
        return

    # Strip the mention text if present, then respond to all messages
    content = message.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()
    if not content:
        await send_with_retry(message.channel, "Hey! Ask me anything about the server status.")
        return

    # Route !commands directly — bypasses the LLM entirely so switches are reliable
    # regardless of which model is currently active.
    if content.startswith("!"):
        cmd_name = content[1:].strip().split()[0].lower()

        # For local model switches, await model load BEFORE showing the banner so
        # the banner is a guarantee that the model is running, not just scheduled.
        # "local" is the cog alias for LLAMA_PROFILE_NAME; all other local profile
        # names (e.g. "gemma", "qwen") resolve to themselves.
        _local_cmd_aliases = {"local": LLAMA_PROFILE_NAME}
        resolved = _local_cmd_aliases.get(cmd_name, cmd_name)
        if ENABLE_LOCAL_LLM and llama_manager.is_local_profile(resolved):
            available = llm_provider.get_available_profiles()
            if resolved in available:
                llm_provider.set_active_profile(resolved)
                ready = await llama_manager.ensure_model(resolved)
                provider = llm_provider.get_provider()
                if ready:
                    await _send_with_retry(message.channel, model_switch_banner(resolved, provider.primary_model))
                else:
                    await _send_with_retry(message.channel, f"⚠️ Failed to load `{resolved}` — llama-server did not become ready.")
            return

        # All other ! commands: remote model switches, !model?, !commands/!help, etc.
        await bot.process_commands(message)
        # Fallback: handle !<profile> for remote profiles without a dedicated cog command
        registered = {c.name for c in bot.commands} | {a for c in bot.commands for a in c.aliases}
        if cmd_name not in registered:
            available = llm_provider.get_available_profiles()
            if cmd_name in available:
                llm_provider.set_active_profile(cmd_name)
                provider = llm_provider.get_provider()
                await _send_with_retry(message.channel, model_switch_banner(cmd_name, provider.primary_model))
        return

    # --- Pending-confirmation shortcut ---
    # If this looks like a "yes" reply to a destructive-action preview, execute
    # the tool directly instead of sending to Claude (which is unreliable here).
    channel_id = message.channel.id
    pending = _confirmations.consume(channel_id, content)
    if pending is not None:
        log.info("Executing pending confirmation: %s(%s)", pending["name"], pending["inputs"])
        loop = asyncio.get_running_loop()
        try:
            reply = await loop.run_in_executor(
                None, execute_tool, pending["name"], pending["inputs"]
            )
        except Exception as e:
            log.exception("Pending confirmation execution failed")
            reply = f"Error executing confirmed action: {e}"
        for chunk in split_message(reply):
            await send_with_retry(message.channel, chunk)
        await bot.process_commands(message)
        return

    typing_task = keep_typing(message.channel)
    try:
        reply = await handle_claude_query(content, message)
    except Exception as e:
        log.exception("LLM query failed")
        reply = f"Error processing request: {e}"
    finally:
        typing_task.cancel()

    if not reply or not reply.strip():
        log.warning("LLM returned empty reply — sending fallback")
        reply = "Sorry, I wasn't able to generate a response. Please try again."

    for chunk in split_message(reply):
        if chunk.strip():
            await _send_with_retry(message.channel, chunk)

    # If the LLM queued a confirmation, send interactive buttons so the user
    # can confirm with a click instead of (or in addition to) typing "yes".
    if _confirmations.peek(message.channel.id):
        ch_id = message.channel.id
        event_loop = asyncio.get_running_loop()

        async def _do_confirm() -> str:
            action = _confirmations.force_consume(ch_id)
            if not action:
                return "No pending action found (may have already been confirmed or cancelled)."
            try:
                return await event_loop.run_in_executor(
                    None, execute_tool, action["name"], action["inputs"]
                )
            except Exception as exc:
                log.exception("Button-confirmed action failed")
                return f"❌ Error: {exc}"

        def _do_cancel() -> None:
            _confirmations.clear(ch_id)

        view = make_confirmation_view(execute=_do_confirm, on_cancel=_do_cancel)
        msg = await message.channel.send(view=view)
        view.message = msg

    await bot.process_commands(message)


# ---------------------------------------------------------------------------
# Notification webhook (local only — Jenkins / scripts POST here)
# ---------------------------------------------------------------------------

_NOTIF_EMOJI_MAP = {
    "🔴": "Failure:", "✅": "Success:", "❌": "Error:",
    "⚠️": "Alert:", "🟢": "Success:", "🟡": "Warning:",
    "⏱️": "Timed out:", "🔄": "Pending:",
    "🎬": "", "🎵": "", "📦": "",
}

def _strip_discord_markdown(text: str) -> str:
    """Convert a Discord-formatted notification to plain spoken text for TTS."""
    for emoji, word in _NOTIF_EMOJI_MAP.items():
        text = text.replace(emoji, word)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    text = re.sub(r'<@!?\d+>', '', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'^>\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


async def _speak_via_gateway(text: str) -> None:
    """Fire-and-forget: push a spoken notification to connected Flutter clients."""
    if not VOICE_GATEWAY_TOKEN:
        return
    spoken = _strip_discord_markdown(text)
    if not spoken:
        return
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                f"{VOICE_GATEWAY_URL}/speak",
                json={"text": spoken, "voice": "am_santa"},
                headers={"Authorization": f"Bearer {VOICE_GATEWAY_TOKEN}"},
                timeout=aiohttp.ClientTimeout(total=15),
            )
    except Exception:
        pass


async def post_notification(text: str):
    """Send a notification to the configured Discord channel."""
    await post_notification_to(DISCORD_CHANNEL_ID, text)


async def post_notification_to(channel_id: int, text: str):
    """Send a notification to a specific channel, falling back to the default."""
    channel = bot.get_channel(channel_id) or bot.get_channel(DISCORD_CHANNEL_ID)
    if channel is None:
        log.error("Channel %s not found for notification", channel_id)
        return
    for chunk in split_message(text):
        await send_with_retry(channel, chunk)
    asyncio.create_task(_speak_via_gateway(text))


async def post_scheduled_notification(channel_id: int, text: str):
    """Send a scheduled task result, prepending an @ping if SCHEDULED_TASK_PING_USER_ID is set."""
    if SCHEDULED_TASK_PING_USER_ID:
        text = f"<@{SCHEDULED_TASK_PING_USER_ID}> {text}"
    await post_notification_to(channel_id, text)


async def handle_notify(request: web.Request) -> web.Response:
    """
    POST /notify
    JSON body:
      {
        "secret":       "...",          # must match WEBHOOK_SECRET if set
        "job_name":     "Login_Test",
        "status":       "FAILURE",      # SUCCESS / FAILURE / UNSTABLE / ABORTED
        "build_number": 42,
        "build_url":    "http://...",
        "message":      "optional extra info"
      }
    """
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="Bad JSON")

    # Validate secret
    if WEBHOOK_SECRET and data.get("secret") != WEBHOOK_SECRET:
        log.warning("Webhook received with wrong secret from %s", request.remote)
        return web.Response(status=403, text="Forbidden")

    job_name     = data.get("job_name", "Unknown job")
    status       = data.get("status", "UNKNOWN").upper()
    build_number = data.get("build_number", "?")
    build_url    = data.get("build_url", "")
    extra        = data.get("message", "")

    emoji = {
        "SUCCESS":  "🟢",
        "FAILURE":  "🔴",
        "UNSTABLE": "🟡",
        "ABORTED":  "⚪",
    }.get(status, "🔔")

    lines = [f"{emoji} **{job_name}** #{build_number} — **{status}**"]
    if extra:
        lines.append(f"> {extra}")
    if build_url:
        lines.append(build_url)

    text = "\n".join(lines)
    log.info("Notification: %s", text)

    asyncio.create_task(post_notification(text))
    return web.Response(text="OK")


async def start_webhook_server():
    app = web.Application()
    app.router.add_post("/notify", handle_notify)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    log.info("Webhook server listening on 0.0.0.0:%d/notify", WEBHOOK_PORT)


# ---------------------------------------------------------------------------
# Proactive background tasks
# ---------------------------------------------------------------------------

# Tracks whether an alert is already active — prevents repeated messages
# each polling cycle. Cleared when the condition resolves.
_alert_state: dict = {}


def _get_disk_pct(path: str) -> int | None:
    """Return used% for the filesystem containing `path`, or None on error."""
    try:
        import subprocess
        r = subprocess.run(["df", path], capture_output=True, text=True, timeout=10)
        # df output: Filesystem 1K-blocks Used Available Use% Mounted on
        lines = r.stdout.strip().splitlines()
        if len(lines) >= 2:
            pct_str = lines[1].split()[4].rstrip("%")
            return int(pct_str)
    except Exception:
        pass
    return None


async def task_disk_alert():
    """Post a Discord alert when media disk usage exceeds DISK_ALERT_THRESHOLD_PCT."""
    await bot.wait_until_ready()
    log.info("Disk alert task started (threshold=%d%%, path=%s)", DISK_ALERT_THRESHOLD_PCT, DISK_ALERT_PATH)
    while not bot.is_closed():
        try:
            loop = asyncio.get_running_loop()
            pct = await loop.run_in_executor(None, _get_disk_pct, DISK_ALERT_PATH)
            if pct is not None:
                key = f"disk_{DISK_ALERT_PATH}"
                if pct >= DISK_ALERT_THRESHOLD_PCT and not _alert_state.get(key):
                    _alert_state[key] = True
                    await post_notification(
                        f"⚠️ **Disk space alert** — `{DISK_ALERT_PATH}` is **{pct}% full** "
                        f"(threshold: {DISK_ALERT_THRESHOLD_PCT}%)"
                    )
                    log.warning("Disk alert fired: %s at %d%%", DISK_ALERT_PATH, pct)
                    _ai_event("AlertFired", alert_type="disk", path=DISK_ALERT_PATH,
                              pct=str(pct), threshold=str(DISK_ALERT_THRESHOLD_PCT))
                elif pct < DISK_ALERT_THRESHOLD_PCT and _alert_state.get(key):
                    _alert_state[key] = False
                    await post_notification(
                        f"✅ **Disk space recovered** — `{DISK_ALERT_PATH}` is now {pct}% full"
                    )
                    log.info("Disk alert cleared: %s at %d%%", DISK_ALERT_PATH, pct)
                    _ai_event("AlertCleared", alert_type="disk", path=DISK_ALERT_PATH, pct=str(pct))
        except Exception:
            log.exception("task_disk_alert error")
        await asyncio.sleep(4 * 3600)  # check every 4 hours


async def task_service_watchdog():
    """Alert when a watched service goes down, and again when it recovers."""
    await bot.wait_until_ready()
    log.info("Service watchdog started (watching: %s)", ", ".join(WATCHDOG_SERVICES))

    # Allow a short startup delay so services have time to come up after a reboot
    await asyncio.sleep(60)

    while not bot.is_closed():
        try:
            loop = asyncio.get_running_loop()
            for svc in WATCHDOG_SERVICES:
                from tools import get_service_status
                status_text = await loop.run_in_executor(None, get_service_status, svc)
                # Determine if the service is up — look for positive signals in the output
                is_up = any(word in status_text.lower() for word in ("up ", "active", "running"))
                key = f"svc_{svc}"
                was_down = _alert_state.get(key, False)

                if not is_up and not was_down:
                    _alert_state[key] = True
                    await post_notification(f"🔴 **{svc}** appears to be **down**\n> {status_text[:200]}")
                    log.warning("Watchdog: %s is down", svc)
                    _ai_event("AlertFired", alert_type="service_down", service=svc)
                elif is_up and was_down:
                    _alert_state[key] = False
                    await post_notification(f"✅ **{svc}** has **recovered**")
                    log.info("Watchdog: %s recovered", svc)
                    _ai_event("AlertCleared", alert_type="service_recovered", service=svc)
        except Exception:
            log.exception("task_service_watchdog error")
        await asyncio.sleep(10 * 60)  # check every 10 minutes


# ---------------------------------------------------------------------------
# Scheduler — poll SQLite, fire due tasks without an LLM call
# ---------------------------------------------------------------------------

def _render_results_template(template: str, results: list[str], combined: str) -> str:
    """Substitute {results} (full blob) and {results[N]} (Nth tool output) in a prompt.

    Indexed form is resolved first so the literal {results} pass doesn't eat
    the '{results' prefix and leave '[N]}' behind. Out-of-range indices are
    left untouched so the failure is visible to the operator.
    """
    import re as _re

    def _sub(m: "_re.Match[str]") -> str:
        idx = int(m.group(1))
        return results[idx] if 0 <= idx < len(results) else m.group(0)

    rendered = _re.sub(r"\{results\[(\d+)\]\}", _sub, template)
    return rendered.replace("{results}", combined)


async def fire_scheduled_task(task: dict) -> None:
    """Execute a single due task. Uses no LLM except when generative_prompt is set."""
    import re
    import json as _json

    task_id   = task["id"]
    task_type = task["task_type"]
    tool_calls: list = _json.loads(task["tool_calls"] or "[]")
    channel_id = task["channel_id"]
    attempt    = task["attempt"]
    max_att    = task["max_attempts"]
    interval   = task["check_interval_minutes"]

    log.info("Firing task #%d (%s): %s", task_id, task_type, task["description"])
    import time as _time
    t0 = _time.monotonic()
    loop = asyncio.get_running_loop()
    task_conv_id = str(uuid.uuid4())
    task_user_msg = f"[scheduled task #{task_id}: {task['description']}]"

    try:
        # --- Execute tool calls ---
        results = []
        for tc in tool_calls:
            r = await loop.run_in_executor(
                None, execute_tool, tc["tool"], tc.get("args", {})
            )
            results.append(r)
        combined = "\n\n".join(results)

        # --- Determine the message ---
        if task["static_message"]:
            # Pre-written at schedule time — zero LLM cost
            message = task["static_message"]

        elif task_type == "condition_check" and task["condition_pattern"]:
            met = bool(re.search(task["condition_pattern"], combined, re.IGNORECASE))
            new_attempt = attempt + 1

            if met:
                # generative_prompt takes priority over met_message when condition is satisfied
                if task["generative_prompt"]:
                    prompt = _render_results_template(task["generative_prompt"], results, combined)
                    _prov = get_provider()
                    _prov_name = get_provider_name()
                    _gen_msgs = [{"role": "user", "content": prompt}]
                    text, in_tok, out_tok = await loop.run_in_executor(
                        None, lambda: _prov.complete_simple(_gen_msgs, _prov.primary_model, 400)
                    )
                    llm_usage.log_call(
                        conversation_id=task_conv_id,
                        model=_prov.primary_model,
                        input_tokens=in_tok,
                        output_tokens=out_tok,
                        user_message=task_user_msg,
                        context="scheduled_generative",
                        provider=_prov_name,
                    )
                    message = text
                else:
                    message = task["met_message"] or f"✅ Done: {task['description']}"
                await loop.run_in_executor(None, scheduler.mark_done, task_id)
                _ai_event("ScheduledTaskFired", task_id=str(task_id), task_type=task_type,
                          description=task["description"][:100], outcome="condition_met",
                          attempt=str(new_attempt))
                await post_scheduled_notification(channel_id, message)
                return

            if new_attempt >= max_att:
                message = (
                    f"⏱️ **Gave up checking** after {max_att} attempts: "
                    f"_{task['description']}_"
                )
                await loop.run_in_executor(None, scheduler.mark_done, task_id)
                _ai_event("ScheduledTaskFired", task_id=str(task_id), task_type=task_type,
                          description=task["description"][:100], outcome="gave_up",
                          attempt=str(new_attempt))
                await post_scheduled_notification(channel_id, message)  # terminal — ping
            else:
                message = (
                    task["not_met_message"]
                    or f"🔄 Not yet: _{task['description']}_ — checking again in {interval} min"
                )
                next_utc = (
                    datetime.datetime.now(datetime.timezone.utc)
                    + datetime.timedelta(minutes=interval)
                ).isoformat()
                await loop.run_in_executor(None, scheduler.reschedule, task_id, next_utc, new_attempt)
                _ai_event("ScheduledTaskFired", task_id=str(task_id), task_type=task_type,
                          description=task["description"][:100], outcome="condition_pending",
                          attempt=str(new_attempt), next_check_min=str(interval))
                await post_notification_to(channel_id, message)  # still pending — no ping
            return

        elif task["generative_prompt"]:
            # One small LLM call for tasks that need fresh synthesis (one_shot / recurring)
            prompt = _render_results_template(task["generative_prompt"], results, combined)
            _prov = get_provider()
            _prov_name = get_provider_name()
            _gen_msgs = [{"role": "user", "content": prompt}]
            text, in_tok, out_tok = await loop.run_in_executor(
                None, lambda: _prov.complete_simple(_gen_msgs, _prov.primary_model, 800)
            )
            llm_usage.log_call(
                conversation_id=task_conv_id,
                model=_prov.primary_model,
                input_tokens=in_tok,
                output_tokens=out_tok,
                user_message=task_user_msg,
                context="scheduled_generative",
                provider=_prov_name,
            )
            message = text

        else:
            # Default: optional intro + tool results
            parts = []
            if task["intro_message"]:
                parts.append(task["intro_message"])
            if combined:
                parts.append(combined)
            message = "\n\n".join(parts) or f"📅 Scheduled: {task['description']}"

        # --- Wrap up ---
        if task_type == "recurring" and task["recurrence_rule"]:
            await loop.run_in_executor(None, scheduler.schedule_next_recurring, task)

        await loop.run_in_executor(None, scheduler.mark_done, task_id)
        _ai_event("ScheduledTaskFired", task_id=str(task_id), task_type=task_type,
                  description=task["description"][:100], outcome="success",
                  duration_ms=str(int((_time.monotonic() - t0) * 1000)))
        await post_scheduled_notification(channel_id, message)

    except Exception as exc:
        log.exception("fire_scheduled_task error for #%d", task_id)
        _ai_trace("Error", f"Scheduled task #{task_id} failed: {exc}",
                  task_id=str(task_id), description=task["description"][:100])
        await loop.run_in_executor(None, scheduler.mark_done, task_id)
        await post_scheduled_notification(
            channel_id, f"⚠️ Scheduled task #{task_id} failed — check bot logs"
        )


# Ids of tasks whose fire_scheduled_task coroutine is currently running. A task
# is only marked done() *after* its (possibly slow) tool calls + LLM generation
# finish, which can exceed the 60 s poll interval. Without this guard the next
# poll re-fetches the still-`done=0` row and fires it a second time — and each
# duplicate fire of a recurring task seeds another permanent future row, so the
# task multiplies week over week. Skipping ids already in flight closes that gap.
_inflight_task_ids: set[int] = set()


async def _fire_scheduled_task_guarded(task: dict) -> None:
    """Run fire_scheduled_task, releasing the in-flight guard when it finishes."""
    try:
        await fire_scheduled_task(task)
    finally:
        _inflight_task_ids.discard(task["id"])


async def _poll_and_fire_due() -> None:
    """One scheduler poll: fetch due tasks and fire any not already in flight."""
    loop = asyncio.get_running_loop()
    due = await loop.run_in_executor(None, scheduler.get_due_tasks)
    for task in due:
        task_id = task["id"]
        if task_id in _inflight_task_ids:
            continue  # already firing — don't double-fire before it marks done
        _inflight_task_ids.add(task_id)
        asyncio.create_task(_fire_scheduled_task_guarded(dict(task)))


async def task_scheduler() -> None:
    """Poll SQLite every 60 s and fire any due tasks."""
    await bot.wait_until_ready()
    scheduler.init_db()
    llm_usage.init_db()
    log.info("Scheduler started — polling every 60s")

    while not bot.is_closed():
        try:
            await _poll_and_fire_due()
        except Exception:
            log.exception("task_scheduler poll error")
        await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _kokoro_warmup(guild_id: int) -> None:
    """Switch Kokoro to GPU mode and speak a readiness phrase in voice."""
    log.info("Kokoro warmup started for guild %s", guild_id)
    ready = await kokoro_manager.ensure_gpu_mode()
    if ready:
        vc = _voice_clients.get(guild_id)
        if vc and vc.is_connected():
            await speak_response(guild_id, "Voice ready.")
            log.info("Kokoro GPU ready — announced in voice (guild %s)", guild_id)
    else:
        log.warning("Kokoro GPU warmup failed for guild %s", guild_id)


async def task_voice_idle_check() -> None:
    """Disconnect from voice channels idle longer than TTS_IDLE_TIMEOUT seconds."""
    import time as _time
    await bot.wait_until_ready()
    while not bot.is_closed():
        for guild_id in list(_voice_clients.keys()):
            vc = _voice_clients.get(guild_id)
            if vc and vc.is_connected() and not vc.is_playing():
                idle_secs = _time.monotonic() - _voice_last_play.get(guild_id, 0)
                if idle_secs > TTS_IDLE_TIMEOUT:
                    _stop_listening(vc)
                    await vc.disconnect()
                    _voice_clients.pop(guild_id, None)
                    _voice_last_play.pop(guild_id, None)
                    log.info("Auto-disconnected from voice in guild %s (idle %.0fs)", guild_id, idle_secs)
                    if ENABLE_KOKORO_IDLE:
                        asyncio.create_task(kokoro_manager.ensure_cpu_mode())
        await asyncio.sleep(60)


async def task_announce_startup():
    """Post a one-time startup message with the current version and changelog."""
    await bot.wait_until_ready()
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if channel:
        await _announce_startup(channel, os.path.dirname(__file__))
    log.info("Startup announced: v%d", BOT_VERSION)


async def task_llama_startup() -> None:
    """Ensure the correct local model is loaded at startup."""
    await bot.wait_until_ready()
    llama_manager.init()
    active = llm_provider.get_active_profile_name()
    if llama_manager.is_local_profile(active):
        log.info("Ensuring local model at startup (profile=%s)", active)
        await llama_manager.ensure_model(active)


async def main():
    from tools import _MODEL_ALIASES
    await bot.add_cog(_make_model_switch_cog(_MODEL_ALIASES))
    await bot.add_cog(_make_help_cog())
    await start_webhook_server()
    asyncio.create_task(task_disk_alert())
    asyncio.create_task(task_service_watchdog())
    asyncio.create_task(task_scheduler())
    asyncio.create_task(task_announce_startup())
    if ENABLE_TTS:
        asyncio.create_task(task_voice_idle_check())
    if ENABLE_LOCAL_LLM:
        asyncio.create_task(task_llama_startup())
    await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
