"""
Panda Discord bot — server status assistant + Jenkins failure notifier.

Responds to @mentions (and DMs) by querying Claude with a curated set of
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
import re
import struct
import subprocess
import textwrap
import threading
import uuid
import wave

import aiohttp
from aiohttp import web
import anthropic
import discord
from discord.ext import commands

import llm_usage
from tools import TOOL_DEFINITIONS, execute_tool  # noqa: E402 (used in fire_scheduled_task too)

# ---------------------------------------------------------------------------
# Pending-confirmation state
# ---------------------------------------------------------------------------
# Maps channel_id → {"name": tool_name, "inputs": {..., "confirmed": True}}
# Set when Claude shows a manage_files/set_jenkins_schedule preview.
# Consumed (and cleared) when the user replies with an affirmative.
_pending_confirmations: dict[int, dict] = {}

_AFFIRMATIVES = {"yes", "y", "yep", "yeah", "yup", "confirm", "ok", "okay", "sure", "do it"}

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

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_VERSION_FILE   = os.path.join(os.path.dirname(__file__), "VERSION")
_CHANGELOG_FILE = os.path.join(os.path.dirname(__file__), "CHANGELOG.md")
BOT_VERSION = int(open(_VERSION_FILE).read().strip()) if os.path.exists(_VERSION_FILE) else 0


def _read_changelog_entry(version: int) -> str:
    """Return bullet lines for *version* from CHANGELOG.md, or '' if not found."""
    if not os.path.exists(_CHANGELOG_FILE):
        return ""
    bullets: list[str] = []
    in_section = False
    with open(_CHANGELOG_FILE) as fh:
        for line in fh:
            if line.startswith(f"## v{version}"):
                in_section = True
                continue
            if in_section:
                if line.startswith("## "):
                    break
                stripped = line.strip()
                if stripped.startswith("- "):
                    bullets.append("• " + stripped[2:])
    return "\n".join(bullets)

DISCORD_TOKEN              = os.environ["DISCORD_TOKEN"]
DISCORD_CHANNEL_ID         = int(os.environ["DISCORD_CHANNEL_ID"])
ANTHROPIC_API_KEY          = os.environ["ANTHROPIC_API_KEY"]
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
AI_ENDPOINT                = os.environ.get("APPINSIGHTS_ENDPOINT", "")

# TTS
ENABLE_TTS               = os.environ.get("ENABLE_TTS", "false").lower() == "true"
TTS_URL                  = os.environ.get("TTS_URL", "http://localhost:8880")
TTS_VOICE                = os.environ.get("TTS_VOICE", "af_heart")
TTS_IDLE_TIMEOUT         = int(os.environ.get("TTS_IDLE_TIMEOUT_SECS", "300"))
TTS_AUTO_JOIN_CHANNEL_ID = int(os.environ["TTS_AUTO_JOIN_CHANNEL_ID"]) if os.environ.get("TTS_AUTO_JOIN_CHANNEL_ID") else None

ENABLE_STT          = os.environ.get("ENABLE_STT", "false").lower() == "true"
STT_URL             = os.environ.get("STT_URL", "http://localhost:8001")
STT_MODEL           = os.environ.get("STT_MODEL", "medium")
STT_SILENCE_TIMEOUT = float(os.environ.get("STT_SILENCE_TIMEOUT_SECS", "1.5"))
STT_RMS_THRESHOLD   = int(os.environ.get("STT_RMS_THRESHOLD", "500"))

# ---------------------------------------------------------------------------
# App Insights telemetry helpers — fire-and-forget, never raise
# ---------------------------------------------------------------------------

def _ai_event(name: str, **props: str) -> None:
    """Send a custom event to App Insights in a daemon thread."""
    if not AI_IKEY or not AI_ENDPOINT:
        return
    import threading, json as _json, urllib.request
    payload = _json.dumps([{
        "name": "Microsoft.ApplicationInsights.Event",
        "time": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "iKey": AI_IKEY,
        "tags": {"ai.cloud.roleName": "pandabot", "ai.device.type": "Other"},
        "data": {"baseType": "EventData", "baseData": {
            "ver": 2, "name": name,
            "properties": {k: str(v) for k, v in props.items()},
        }},
    }]).encode()
    def _send():
        try:
            urllib.request.urlopen(
                urllib.request.Request(AI_ENDPOINT, payload, {"Content-Type": "application/json"}),
                timeout=5,
            )
        except Exception:
            pass
    threading.Thread(target=_send, daemon=True).start()


def _ai_trace(severity: str, message: str, **props: str) -> None:
    """Send a trace message to App Insights. severity: Verbose|Information|Warning|Error|Critical"""
    if not AI_IKEY or not AI_ENDPOINT:
        return
    import threading, json as _json, urllib.request
    level = {"verbose": 0, "information": 1, "warning": 2, "error": 3, "critical": 4}.get(
        severity.lower(), 1
    )
    payload = _json.dumps([{
        "name": "Microsoft.ApplicationInsights.Message",
        "time": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "iKey": AI_IKEY,
        "tags": {"ai.cloud.roleName": "pandabot", "ai.device.type": "Other"},
        "data": {"baseType": "MessageData", "baseData": {
            "ver": 2, "message": message, "severityLevel": level,
            "properties": {k: str(v) for k, v in props.items()},
        }},
    }]).encode()
    def _send():
        try:
            urllib.request.urlopen(
                urllib.request.Request(AI_ENDPOINT, payload, {"Content-Type": "application/json"}),
                timeout=5,
            )
        except Exception:
            pass
    threading.Thread(target=_send, daemon=True).start()

def _build_system_prompt() -> str:
    from tools import (
        ENABLE_JELLYFIN, ENABLE_JENKINS, ENABLE_RIPPING,
        JENKINS_JOBS, ALLOWED_SYSTEMD_SERVICES,
    )
    now = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")

    # --- Services block ---
    if SERVER_DESCRIPTION:
        # Deployer provided a free-form description — use it verbatim.
        # Supports literal \n sequences in the .env value for multi-line output.
        services_block = SERVER_DESCRIPTION.strip().replace("\\n", "\n")
    else:
        # Auto-build from feature flags
        svc_lines = ["The server runs:"]
        if ENABLE_JELLYFIN:
            svc_lines.append("  - Jellyfin (Docker, port 8096) — media server")
        if ENABLE_JENKINS:
            jobs_fmt = ", ".join(JENKINS_JOBS)
            svc_lines.append(f"  - Jenkins (Docker, port 8080) — CI/CD server (jobs: {jobs_fmt})")
        for svc in sorted(ALLOWED_SYSTEMD_SERVICES):
            svc_lines.append(f"  - {svc} (systemd)")
        if TAILSCALE_IP:
            svc_lines.append(f"  - Tailscale VPN (IP {TAILSCALE_IP})")
        else:
            svc_lines.append("  - Tailscale VPN")
        if ENABLE_RIPPING:
            svc_lines.append("  - MakeMKV + abcde for disc ripping (udev auto-rip pipeline)")
        services_block = "\n".join(svc_lines)

    # --- Jenkins triggering instructions (only when enabled) ---
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
             generative_prompt: summarise the result in 1–2 sentences from {{results}}
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

    return textwrap.dedent(f"""\
        You are {BOT_NAME}, a helpful assistant for a home Ubuntu Server machine.
        Current server date/time: {now}.
        {services_block}

        Hardware: {HARDWARE_DESCRIPTION}.
        Server timezone: {TZ_NAME}.
        Timestamps in structured tool responses are already converted to server local time.
        App Insights data is returned in UTC — convert to local time when reporting to the user.

        Always call a tool to answer questions about server state — never guess
        or infer from training knowledge. If a tool returns an error, relay the
        exact error text rather than paraphrasing it as a configuration problem.

        For any question about what movies are in the library — including genre
        or mood recommendations (stoner, horror, 80s, feel-good, etc.) — call
        query_jellyfin(search_movies). It returns Jellyfin metadata: genres,
        ratings, and plot summaries for every movie. Only use query_media_library
        when the user specifically needs filesystem details like file size, codec,
        or bitrate.
        {jenkins_instructions}
        When the user asks for something at a future time, on a condition, or on a
        recurring schedule, call manage_schedule(action='create') rather than
        answering immediately. Decide at schedule time which tools to run and what
        message to post — the task fires mechanically with no LLM unless you set
        generative_prompt. Use static_message for pre-written content like jokes.

        Be concise. When reporting log extracts, summarise rather than quoting
        everything unless the user asks for raw output.
    """)

DISCORD_MSG_LIMIT = 1900  # leave headroom below the 2000-char limit

# ---------------------------------------------------------------------------
# Discord bot
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

if ENABLE_TTS or ENABLE_STT:
    try:
        discord.opus.load_opus("libopus.so.0")
        logging.getLogger("panda-bot").info("libopus loaded")
    except Exception as _opus_err:
        logging.getLogger("panda-bot").warning("Could not load libopus: %s", _opus_err)


def split_message(text: str) -> list[str]:
    """Split a long response into ≤1900-char chunks on line boundaries."""
    if len(text) <= DISCORD_MSG_LIMIT:
        return [text]
    chunks, current = [], []
    current_len = 0
    for line in text.splitlines(keepends=True):
        if current_len + len(line) > DISCORD_MSG_LIMIT and current:
            chunks.append("".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += len(line)
    if current:
        chunks.append("".join(current))
    return chunks


def _calc_rms(data: bytes) -> float:
    """Return RMS amplitude of raw 16-bit LE PCM bytes (0–32767 scale)."""
    n = len(data) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack(f"<{n}h", data[: n * 2])
    return (sum(s * s for s in samples) / n) ** 0.5


def _normalize_audio(samples: "np.ndarray", target_rms: float = 0.12) -> "np.ndarray":
    """Normalize audio to a target RMS level, then soft-clip to prevent harsh peaks.

    RMS-based normalization preserves the speech-to-noise ratio better than
    peak-based normalization.  When audio is mostly quiet with occasional loud
    transients (like Discord Opus output), peak normalization amplifies the
    noise floor along with everything else, making Whisper's job harder.

    The two-stage approach:
      1. Scale so RMS == *target_rms* (brings average level into Whisper's sweet spot)
      2. Soft-clip any samples exceeding 0.95 to avoid harsh digital clipping

    Silent audio (RMS < 0.001) is returned as-is.
    """
    import numpy as np
    rms = float(np.sqrt(np.mean(samples ** 2)))
    if rms < 0.001:
        return samples
    gain = target_rms / rms
    scaled = samples * gain
    # Soft-clip: tanh provides a smooth knee, avoiding the harshness of hard clipping
    return np.tanh(scaled * 1.5) / 1.5


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

    def __init__(self, guild_id: int):
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

    def wants_opus(self) -> bool:
        # Must be True — voice_recv's internal decoder crashes on first bad Opus packet,
        # killing the router thread permanently. We decode per-packet ourselves instead.
        return True

    def write(self, user, data) -> None:
        if user is None:
            return
        uid = user.id if hasattr(user, "id") else int(user)
        if bot.user and uid == bot.user.id:
            return

        # data.opus may be pre-decryption bytes; data.packet.decrypted_data is the real Opus payload
        packet     = getattr(data, "packet", None)
        opus_bytes = getattr(packet, "decrypted_data", None) or getattr(data, "opus", None)

        # --- Per-packet debug logging ---
        # Determine packet type and extract metadata for diagnosing clicking-sound issue
        pkt_type = "UNKNOWN"
        seq = -1
        ts = -1
        if packet is not None:
            pkt_cls = type(packet).__name__
            if pkt_cls == "SilencePacket":
                pkt_type = "SILENCE"
            elif pkt_cls == "FakePacket":
                pkt_type = "FAKE"
            elif pkt_cls == "RTPPacket":
                pkt_type = "RTP"
                # RTP header: first 12 bytes; seq=bytes 2-3, timestamp=bytes 4-7
                hdr = getattr(packet, "header", None)
                if hdr and len(hdr) >= 12:
                    seq = (hdr[2] << 8) | hdr[3]
                    ts  = (hdr[4] << 24) | (hdr[5] << 16) | (hdr[6] << 8) | hdr[7]
            else:
                pkt_type = pkt_cls

        # Log first packet once for diagnosis
        if not hasattr(self, "_logged_first"):
            self._logged_first = True
            raw_opus = getattr(data, "opus", None)
            dec_data = getattr(packet, "decrypted_data", None) if packet else None
            # Log extended bit from RTP header (bit 4 of first byte)
            ext_bit = "?"
            if hdr is not None and len(hdr) >= 1:
                ext_bit = "1" if (hdr[0] & 0b00010000) else "0"
            # Try to get encryption mode from the voice source
            src = getattr(data, "source", None)
            enc_mode = getattr(src, "mode", "n/a") if src is not None else "n/a"
            log.info(
                "STT first packet: type=%s seq=%d ts=%d ext=%s mode=%s data.opus=%s decrypted_data=%s",
                pkt_type, seq, ts, ext_bit, enc_mode,
                raw_opus[:16].hex() if raw_opus else None,
                dec_data[:16].hex() if dec_data else None,
            )

        if not opus_bytes:
            return

        # --- Save first 10 raw packets for offline analysis ---
        if not hasattr(self, '_packet_save_count'):
            self._packet_save_count = 0
        if self._packet_save_count < 10:
            import os as _os
            _pkt_dir = '/opt/discord-bot/stt_packets'
            _os.makedirs(_pkt_dir, exist_ok=True)
            _seq = seq
            _ts = ts
            _pkt_name = f'pkt_{uid}_{_seq}_{_ts}.bin'
            with open(_os.path.join(_pkt_dir, _pkt_name), 'wb') as _f:
                _f.write(opus_bytes)
            with open(_os.path.join(_pkt_dir, f'pkt_{uid}_{_seq}_{_ts}_info.txt'), 'w') as _f:
                _f.write(f'uid={uid} seq={_seq} ts={_ts} pkt_type={pkt_type}\n')
                _f.write(f'opus_len={len(opus_bytes)}\n')
                _f.write(f'toc_byte={opus_bytes[0]:02x} opus_bits={bin(opus_bytes[0])[2:].zfill(8)}\n')
            self._packet_save_count += 1

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

        # --- Per-packet PCM stats ---
        # Log every Nth packet to avoid log spam; log first 50 packets of each type
        _pkt_count = getattr(self, "_pkt_count", 0) + 1
        self._pkt_count = _pkt_count
        _log_this = _pkt_count <= 50 or (_pkt_count % 100 == 0)

        if _log_this:
            # Compute PCM stats
            pcm_len = len(pcm)
            pcm_samples = pcm_len // 2  # 16-bit samples
            # Count zeros and flat runs
            zero_count = 0
            max_flat_run = 0
            cur_flat_run = 0
            prev_val = None
            min_val = 32767
            max_val = -32768
            for i in range(0, pcm_len, 2):
                if i + 1 >= pcm_len:
                    break
                val = (pcm[i+1] << 8) | pcm[i]
                # Sign-extend 16-bit
                if val >= 32768:
                    val -= 65536
                if val == 0:
                    zero_count += 1
                if val < min_val:
                    min_val = val
                if val > max_val:
                    max_val = val
                if prev_val is not None and val == prev_val:
                    cur_flat_run += 1
                else:
                    cur_flat_run = 0
                if cur_flat_run > max_flat_run:
                    max_flat_run = cur_flat_run
                prev_val = val

            log.info(
                "STT pkt #%d: type=%s seq=%d ts=%d opus=%dB pcm=%dB(%d samples) "
                "rms=%.0f min=%d max=%d zeros=%d flat_run=%d",
                _pkt_count, pkt_type, seq, ts,
                len(opus_bytes) if opus_bytes else 0,
                pcm_len, pcm_samples,
                rms, min_val, max_val, zero_count, max_flat_run,
            )

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
        if not buf:
            return
        min_bytes = int(self.MIN_SECS * self.SAMPLE_RATE * self.CHANNELS * self.SAMPLE_WIDTH)
        if len(buf) < min_bytes:
            return

        duration = len(buf) / (self.SAMPLE_RATE * self.CHANNELS * self.SAMPLE_WIDTH)
        if stats and stats["frames"] > 0:
            avg_rms = stats["total_rms"] / stats["frames"]
            log.info(
                "STT: silence for user %s — %.2fs, %d bytes, %d frames, "
                "peak_rms=%.0f avg_rms=%.0f noise_floor=%.0f — transcribing",
                user_id, duration, len(buf), stats["frames"],
                stats["peak_rms"], avg_rms,
                self._noise_floor.get(user_id, 0.0),
            )
        else:
            log.info("STT: silence for user %s, %.2fs, %d bytes — transcribing",
                     user_id, duration, len(buf))

        # DEBUG: Save raw decoded PCM as WAV so we can hear what the Opus decoder actually produces
        try:
            import wave as _wave
            raw_path = "/opt/discord-bot/stt_raw_pcm.wav"
            with _wave.open(raw_path, "wb") as wf:
                wf.setnchannels(2)
                wf.setsampwidth(2)
                wf.setframerate(48000)
                wf.writeframes(bytes(buf))
            log.info("STT: raw PCM WAV saved to %s (%.2fs, %d bytes)", raw_path, duration, len(buf))
        except Exception as _raw_err:
            log.debug("Raw PCM WAV save failed: %s", _raw_err)

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
            for uid in uids:
                timer = self._timers.pop(uid, None)
                if timer:
                    timer.cancel()
                stats = self._utt_stats.pop(uid, None)
        for uid, buf in bufs.items():
            if not buf:
                continue
            min_bytes = int(self.MIN_SECS * self.SAMPLE_RATE * self.CHANNELS * self.SAMPLE_WIDTH)
            if len(buf) < min_bytes:
                continue
            duration = len(buf) / (self.SAMPLE_RATE * self.CHANNELS * self.SAMPLE_WIDTH)
            log.info("STT: flushing %d bytes (%.2fs) for user %s", len(buf), duration, uid)

            # DEBUG: Save raw PCM WAV on flush too
            try:
                import wave as _wave
                raw_path = "/opt/discord-bot/stt_raw_pcm.wav"
                with _wave.open(raw_path, "wb") as wf:
                    wf.setnchannels(2)
                    wf.setsampwidth(2)
                    wf.setframerate(48000)
                    wf.writeframes(bytes(buf))
                log.info("STT: raw PCM WAV saved to %s (%.2fs, %d bytes)", raw_path, duration, len(buf))
            except Exception as _raw_err:
                log.debug("Raw PCM WAV save failed: %s", _raw_err)

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


def _start_listening(vc: discord.VoiceClient, guild_id: int) -> None:
    """Attach an STTSink and begin receiving audio."""
    if not ENABLE_STT or _vr is None:
        return
    try:
        vc.listen(STTSink(guild_id))
        log.info("STT listening started in guild %s", guild_id)
    except Exception as exc:
        log.warning("Could not start STT: %s", exc, exc_info=True)


def _stop_listening(vc: discord.VoiceClient) -> None:
    """Stop receiving audio and clean up the sink."""
    if not ENABLE_STT or _vr is None:
        return
    try:
        vc.stop_listening()
    except Exception as exc:
        log.warning("Could not stop STT: %s", exc)


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
      1. audioop.tomono  → 48kHz mono s16
      2. audioop.ratecv  → 16kHz mono s16  (linear-interp resample — adequate without VAD)
      3. np conversion   → float32 [-1, 1]
      4. Normalization   → RMS ≈ 0.12 (RMS-based, preserves speech-to-noise ratio)
      5. Whisper         → transcription (VAD disabled; relaxed thresholds)
    """
    import audioop
    import numpy as np
    model = _get_whisper_model()
    try:
        # Step 1: Stereo 48kHz 16-bit → mono 48kHz 16-bit
        mono_pcm = audioop.tomono(pcm_bytes, 2, 0.5, 0.5)

        # Step 2: 48kHz → 16kHz using audioop.ratecv (linear interpolation)
        # NOTE: audioop.ratecv introduces mild stair-step artifacts, but Whisper
        # can handle them fine WITHOUT VAD.  The VAD model was the problem — it
        # classified the resampled audio as noise and removed everything.
        resampled, _ = audioop.ratecv(mono_pcm, 2, 1, 48000, 16000, None)

        # Step 3: s16 → float32 [-1, 1]
        samples = np.frombuffer(resampled, dtype=np.int16).astype(np.float32) / 32768.0

        # Step 3b: Pre-emphasis — y[n] = x[n] - 0.97*x[n-1]
        # Discord's audio processing attenuates high frequencies (sibilance ~0.9% of energy).
        # Pre-emphasis compensates by boosting above ~300Hz, which is standard in ASR pipelines.
        samples[1:] -= 0.97 * samples[:-1]

        # Step 4: Normalize to target RMS (not peak) — preserves speech-to-noise ratio
        samples = _normalize_audio(samples, target_rms=0.12)

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

        # Step 5: Transcribe — NO VAD filter (it was removing all audio due to
        # resampling artifacts).  Whisper's own internal processing handles
        # silence/noise rejection via no_speech_threshold and log_prob_threshold.
        #
        # Thresholds relaxed from defaults because Discord Opus audio at gain=8
        # still has a lower SNR than typical microphone input:
        #   no_speech_threshold: 0.6 → 0.3  (less aggressive at discarding "no speech")
        #   log_prob_threshold: -1.0 → -2.0 (accept lower-probability text)
        segments, info = model.transcribe(
            samples,
            language="en",
            beam_size=5,
            vad_filter=False,                    # Disabled — was removing all audio
            condition_on_previous_text=False,    # Avoid compounding errors across utterances
            temperature=0.0,                     # Greedy decoding (most deterministic, fewer hallucinations)
            compression_ratio_threshold=2.0,     # Default — reject overly compressed audio
            log_prob_threshold=-2.0,             # Relaxed — accept lower-probability text for quiet audio
            no_speech_threshold=0.3,             # Relaxed — less aggressive at discarding "no speech"
        )
        segs = list(segments)
        text = " ".join(seg.text for seg in segs).strip()
        log.info("Whisper: segs=%d lang_prob=%.2f text=%r",
                 len(segs), info.language_probability, text[:200])
        if _is_whisper_hallucination(text):
            log.info("Whisper: hallucination detected, discarding")
            return None
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

    await channel.send(f"🎤 **{display_name}:** {transcript}")

    loop = asyncio.get_running_loop()
    try:
        reply = await loop.run_in_executor(
            None, _run_claude_loop, transcript, None, channel.id, None
        )
    except Exception as exc:
        log.exception("Claude query failed for STT input")
        reply = f"Error talking to Claude: {exc}"

    for chunk in split_message(reply):
        await channel.send(chunk)

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


async def build_history(channel: discord.abc.Messageable, before: discord.Message, limit: int = 10) -> list[dict]:
    """
    Return up to `limit` messages before `before` as Claude-formatted turns.

    Bot messages → assistant role.  All other messages → user role.
    Consecutive same-role messages are merged so the list always alternates,
    and any leading assistant turns are dropped (Claude requires user-first).
    """
    raw = []
    async for msg in channel.history(limit=limit, before=before):
        if not msg.content:
            continue
        role = "assistant" if msg.author.bot else "user"
        raw.append((role, msg.content))
    raw.reverse()  # oldest first

    # Merge consecutive same-role messages
    merged: list[dict] = []
    for role, content in raw:
        if merged and merged[-1]["role"] == role:
            merged[-1]["content"] += "\n" + content
        else:
            merged.append({"role": role, "content": content})

    # Claude requires the first message to be from the user
    while merged and merged[0]["role"] == "assistant":
        merged.pop(0)

    return merged


def _run_claude_loop(
    user_message: str,
    history: list[dict] | None = None,
    channel_id: int | None = None,
    conversation_id: str | None = None,
) -> str:
    """Synchronous Claude agentic loop (run in a thread executor)."""
    import time as _time
    conv_id = conversation_id or str(uuid.uuid4())
    messages = (history or []) + [{"role": "user", "content": user_message}]
    tools_called: list[str] = []
    t0 = _time.monotonic()

    system_prompt = _build_system_prompt()
    for _ in range(10):  # safety: max 10 tool-call rounds
        response = claude.messages.create(
            model="claude-haiku-4-5",
            max_tokens=4096,
            system=system_prompt,
            tools=TOOL_DEFINITIONS,
            messages=messages,
        )
        llm_usage.log_call(
            conversation_id=conv_id,
            model="claude-haiku-4-5",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            user_message=user_message,
            context="main",
        )
        log.info(
            "Claude stop_reason=%s in=%d out=%d cost=$%.5f",
            response.stop_reason,
            response.usage.input_tokens,
            response.usage.output_tokens,
            llm_usage.cost_usd("claude-haiku-4-5", response.usage.input_tokens, response.usage.output_tokens),
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    _ai_event(
                        "BotQuery",
                        message=user_message[:200],
                        tools=",".join(tools_called) or "none",
                        response_ms=str(int((_time.monotonic() - t0) * 1000)),
                    )
                    return block.text
            return "(no text response)"

        if response.stop_reason == "tool_use":
            # manage_schedule has a complex schema — upgrade to Sonnet to fill parameters
            # accurately. Haiku already decided to call it; Sonnet re-issues with same context.
            if any(b.type == "tool_use" and b.name == "manage_schedule" for b in response.content):
                log.info("manage_schedule detected — upgrading to Sonnet for parameter accuracy")
                response = claude.messages.create(
                    model="claude-sonnet-4-5",
                    max_tokens=4096,
                    system=system_prompt,
                    tools=TOOL_DEFINITIONS,
                    messages=messages,
                )
                llm_usage.log_call(
                    conversation_id=conv_id,
                    model="claude-sonnet-4-5",
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    user_message=user_message,
                    context="sonnet_upgrade",
                )
                log.info(
                    "Sonnet stop_reason=%s in=%d out=%d cost=$%.5f",
                    response.stop_reason,
                    response.usage.input_tokens,
                    response.usage.output_tokens,
                    llm_usage.cost_usd("claude-sonnet-4-5", response.usage.input_tokens, response.usage.output_tokens),
                )

            # Append assistant turn (may include thinking blocks + tool_use blocks)
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    log.info("Tool call: %s(%s)", block.name, block.input)
                    tools_called.append(block.name)
                    result = execute_tool(block.name, block.input)
                    _write_tools = {"manage_files", "set_jenkins_schedule", "trigger_jenkins_job"}
                    if block.name in _write_tools:
                        log.info("Tool result (%s): %.400s", block.name, result)
                    else:
                        log.debug("Tool result (%s): %.200s", block.name, result)
                    # Save pending confirmation when a destructive preview is shown.
                    # The bot will execute confirmed=True directly when the user says yes,
                    # bypassing Claude (which is unreliable at this step).
                    _confirm_tools = {"manage_files", "set_jenkins_schedule"}
                    if (
                        channel_id is not None
                        and block.name in _confirm_tools
                        and not block.input.get("confirmed", False)
                        and ("Reply **yes** to confirm" in result or "⚠️" in result)
                    ):
                        confirmed_inputs = {**block.input, "confirmed": True}
                        _pending_confirmations[channel_id] = {
                            "name": block.name,
                            "inputs": confirmed_inputs,
                        }
                        log.info("Pending confirmation saved for channel %s: %s", channel_id, block.name)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": tool_results})
        else:
            return f"(unexpected stop_reason: {response.stop_reason})"

    return "Sorry, I hit the tool-call limit without finishing. Try a more specific question."


async def handle_claude_query(user_message: str, message: discord.Message) -> str:
    """Fetch channel history, then dispatch the synchronous Claude loop to a thread."""
    history = await build_history(message.channel, before=message)
    log.info("Sending %d history messages as context", len(history))
    conv_id = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _run_claude_loop, user_message, history, message.channel.id, conv_id
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
    else:
        await ctx.send("I'm not in a voice channel.")


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Auto-join TTS_AUTO_JOIN_CHANNEL_ID when a user enters; auto-leave when all users leave."""
    if not ENABLE_TTS or TTS_AUTO_JOIN_CHANNEL_ID is None:
        return
    if member.bot:
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


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mention = bot.user in message.mentions

    if not (is_dm or is_mention):
        await bot.process_commands(message)
        return

    # Strip the mention text
    content = message.content
    if is_mention:
        content = content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()
    if not content:
        await message.channel.send("Hey! Ask me anything about the server status.")
        return

    # --- Pending-confirmation shortcut ---
    # If this looks like a "yes" reply to a destructive-action preview, execute
    # the tool directly instead of sending to Claude (which is unreliable here).
    channel_id = message.channel.id
    if content.lower().strip() in _AFFIRMATIVES and channel_id in _pending_confirmations:
        pending = _pending_confirmations.pop(channel_id)
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
            await message.channel.send(chunk)
        await bot.process_commands(message)
        return

    async def _keep_typing():
        """Re-trigger the typing indicator every 8s so it stays visible for long queries."""
        try:
            while True:
                await message.channel.typing()
                await asyncio.sleep(8)
        except asyncio.CancelledError:
            pass

    typing_task = asyncio.create_task(_keep_typing())
    try:
        reply = await handle_claude_query(content, message)
    except Exception as e:
        log.exception("Claude query failed")
        reply = f"Error talking to Claude: {e}"
    finally:
        typing_task.cancel()

    for chunk in split_message(reply):
        await message.channel.send(chunk)

    if ENABLE_TTS and message.guild is not None:
        asyncio.create_task(speak_response(message.guild.id, reply))

    await bot.process_commands(message)


# ---------------------------------------------------------------------------
# Notification webhook (local only — Jenkins / scripts POST here)
# ---------------------------------------------------------------------------

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
        await channel.send(chunk)


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

async def fire_scheduled_task(task: dict) -> None:
    """Execute a single due task. Uses no LLM except when generative_prompt is set."""
    import re
    import json as _json
    import scheduler as sched

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

        elif task["generative_prompt"]:
            # One small Haiku call for tasks that need fresh synthesis
            prompt = task["generative_prompt"].replace("{results}", combined)
            resp = await loop.run_in_executor(None, lambda: claude.messages.create(
                model="claude-haiku-4-5",
                max_tokens=800,
                messages=[{"role": "user", "content": prompt}],
            ))
            llm_usage.log_call(
                conversation_id=task_conv_id,
                model="claude-haiku-4-5",
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
                user_message=task_user_msg,
                context="scheduled_generative",
            )
            message = resp.content[0].text

        elif task_type == "condition_check" and task["condition_pattern"]:
            met = bool(re.search(task["condition_pattern"], combined, re.IGNORECASE))
            new_attempt = attempt + 1

            if met:
                # generative_prompt takes priority over met_message when condition is satisfied
                if task["generative_prompt"]:
                    prompt = task["generative_prompt"].replace("{results}", combined)
                    resp = await loop.run_in_executor(None, lambda: claude.messages.create(
                        model="claude-haiku-4-5",
                        max_tokens=400,
                        messages=[{"role": "user", "content": prompt}],
                    ))
                    llm_usage.log_call(
                        conversation_id=task_conv_id,
                        model="claude-haiku-4-5",
                        input_tokens=resp.usage.input_tokens,
                        output_tokens=resp.usage.output_tokens,
                        user_message=task_user_msg,
                        context="scheduled_generative",
                    )
                    message = resp.content[0].text
                else:
                    message = task["met_message"] or f"✅ Done: {task['description']}"
                await loop.run_in_executor(None, sched.mark_done, task_id)
                _ai_event("ScheduledTaskFired", task_id=str(task_id), task_type=task_type,
                          description=task["description"][:100], outcome="condition_met",
                          attempt=str(new_attempt))
                await post_notification_to(channel_id, message)
                return

            if new_attempt >= max_att:
                message = (
                    f"⏱️ **Gave up checking** after {max_att} attempts: "
                    f"_{task['description']}_"
                )
                await loop.run_in_executor(None, sched.mark_done, task_id)
                _ai_event("ScheduledTaskFired", task_id=str(task_id), task_type=task_type,
                          description=task["description"][:100], outcome="gave_up",
                          attempt=str(new_attempt))
            else:
                message = (
                    task["not_met_message"]
                    or f"🔄 Not yet: _{task['description']}_ — checking again in {interval} min"
                )
                next_utc = (
                    datetime.datetime.now(datetime.timezone.utc)
                    + datetime.timedelta(minutes=interval)
                ).isoformat()
                await loop.run_in_executor(None, sched.reschedule, task_id, next_utc, new_attempt)
                _ai_event("ScheduledTaskFired", task_id=str(task_id), task_type=task_type,
                          description=task["description"][:100], outcome="condition_pending",
                          attempt=str(new_attempt), next_check_min=str(interval))

            await post_notification_to(channel_id, message)
            return

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
            await loop.run_in_executor(None, sched.schedule_next_recurring, task)

        await loop.run_in_executor(None, sched.mark_done, task_id)
        _ai_event("ScheduledTaskFired", task_id=str(task_id), task_type=task_type,
                  description=task["description"][:100], outcome="success",
                  duration_ms=str(int((_time.monotonic() - t0) * 1000)))
        await post_notification_to(channel_id, message)

    except Exception as exc:
        log.exception("fire_scheduled_task error for #%d", task_id)
        _ai_trace("Error", f"Scheduled task #{task_id} failed: {exc}",
                  task_id=str(task_id), description=task["description"][:100])
        await loop.run_in_executor(None, sched.mark_done, task_id)
        await post_notification_to(
            channel_id, f"⚠️ Scheduled task #{task_id} failed — check bot logs"
        )


async def task_scheduler() -> None:
    """Poll SQLite every 60 s and fire any due tasks."""
    import scheduler as sched
    await bot.wait_until_ready()
    sched.init_db()
    llm_usage.init_db()
    log.info("Scheduler started — polling every 60s (db: %s)", sched.DB_PATH)

    while not bot.is_closed():
        try:
            loop = asyncio.get_running_loop()
            due = await loop.run_in_executor(None, sched.get_due_tasks)
            for task in due:
                asyncio.create_task(fire_scheduled_task(dict(task)))
        except Exception:
            log.exception("task_scheduler poll error")
        await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

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
        await asyncio.sleep(60)


async def task_announce_startup():
    """Post a one-time startup message with the current version and changelog."""
    await bot.wait_until_ready()
    msg = f"{BOT_EMOJI} **{BOT_NAME} v{BOT_VERSION}** online"
    changelog = _read_changelog_entry(BOT_VERSION)
    if changelog:
        msg += f"\n{changelog}"
    await post_notification(msg)
    log.info("Startup announced: v%d", BOT_VERSION)


async def main():
    await start_webhook_server()
    asyncio.create_task(task_disk_alert())
    asyncio.create_task(task_service_watchdog())
    asyncio.create_task(task_scheduler())
    asyncio.create_task(task_announce_startup())
    if ENABLE_TTS:
        asyncio.create_task(task_voice_idle_check())
    await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
