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
import csv
import datetime
import io
import logging
import os
import subprocess
import textwrap

import aiohttp
from aiohttp import web
import anthropic
import discord
from discord.ext import commands

from tools import TOOL_DEFINITIONS, execute_tool  # noqa: E402 (used in fire_scheduled_task too)

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

_VERSION_FILE = os.path.join(os.path.dirname(__file__), "VERSION")
BOT_VERSION = int(open(_VERSION_FILE).read().strip()) if os.path.exists(_VERSION_FILE) else 0

DISCORD_TOKEN              = os.environ["DISCORD_TOKEN"]
DISCORD_CHANNEL_ID         = int(os.environ["DISCORD_CHANNEL_ID"])
ANTHROPIC_API_KEY          = os.environ["ANTHROPIC_API_KEY"]
WEBHOOK_PORT               = int(os.environ.get("WEBHOOK_PORT", "8765"))
WEBHOOK_SECRET             = os.environ.get("WEBHOOK_SECRET", "")
TAILSCALE_IP               = os.environ.get("TAILSCALE_IP", "")
DISK_ALERT_THRESHOLD_PCT   = int(os.environ.get("DISK_ALERT_THRESHOLD_PCT", "85"))
DISK_ALERT_PATH            = os.environ.get("DISK_ALERT_PATH", "/mnt/media")
WATCHDOG_SERVICES          = [s.strip() for s in os.environ.get("WATCHDOG_SERVICES", "jellyfin,sunshine").split(",") if s.strip()]
WEEKLY_DIGEST_DAY          = int(os.environ.get("WEEKLY_DIGEST_DAY", "6"))   # 0=Mon … 6=Sun
WEEKLY_DIGEST_HOUR         = int(os.environ.get("WEEKLY_DIGEST_HOUR", "9"))  # server local time
AI_IKEY                    = os.environ.get("APPINSIGHTS_IKEY", "")
AI_ENDPOINT                = os.environ.get("APPINSIGHTS_ENDPOINT", "")

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
        "time": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
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
        "time": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
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
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M %Z")
    tailscale_line = f"  - Tailscale VPN (IP {TAILSCALE_IP})" if TAILSCALE_IP else "  - Tailscale VPN"
    return textwrap.dedent(f"""\
        You are Panda, a helpful assistant for a home Ubuntu Server 24.04 machine.
        Current server date/time: {now}.
        The server runs:
          - Jellyfin (Docker, port 8096) — media server with NVIDIA NVENC transcoding
          - Jenkins (Docker, port 8080) — CI server running these jobs:
              • Login_Test (hourly) — Playwright test of the Jellyfin login page
              • Process_Movies (midnight) — sorts and names ripped video files
              • Nightly_Convert (3 am) — re-encodes video to h264_nvenc
          - Sunshine (bare metal, systemd) — game streaming (Moonlight / Shield TV)
          - Cockpit (port 9090), Portainer (port 9000) — admin UIs
        {tailscale_line}
          - MakeMKV + abcde for disc ripping (udev auto-rip pipeline)

        Hardware: NVIDIA GTX 970 (4 GB VRAM), 2 TB NTFS HDD at /mnt/media.
        Server timezone: America/New_York (Eastern Time, EDT/EST). All timestamps
        from tools are already in local time. When reading raw log content, treat
        timestamps as Eastern Time — never label them UTC.

        You have read-only tools to check disk usage, log tails, service status,
        Jenkins build status, and system stats. You cannot execute arbitrary code
        or make any changes to the server.

        Always call a tool to answer questions about server state — never guess
        or infer from training knowledge. If a tool returns an error, relay the
        exact error text rather than paraphrasing it as a configuration problem.

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


async def build_history(channel: discord.abc.Messageable, before: discord.Message, limit: int = 10) -> list[dict]:
    """
    Return up to `limit` messages before `before` as Claude-formatted turns.

    Bot messages → assistant role.  All other messages → user role.
    Consecutive same-role messages are merged so the list always alternates,
    and any leading assistant turns are dropped (Claude requires user-first).
    """
    raw = []
    async for msg in channel.history(limit=limit, before=before):
        role = "assistant" if msg.author.bot else "user"
        raw.append((role, msg.content or ""))
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


def _run_claude_loop(user_message: str, history: list[dict] | None = None) -> str:
    """Synchronous Claude agentic loop (run in a thread executor)."""
    import time as _time
    messages = (history or []) + [{"role": "user", "content": user_message}]
    tools_called: list[str] = []
    t0 = _time.monotonic()

    system_prompt = _build_system_prompt()
    for _ in range(10):  # safety: max 10 tool-call rounds
        response = claude.messages.create(
            model="claude-haiku-4-5",
            max_tokens=2048,
            system=system_prompt,
            tools=TOOL_DEFINITIONS,
            messages=messages,
        )
        log.info("Claude stop_reason=%s", response.stop_reason)

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
            # Append assistant turn (may include thinking blocks + tool_use blocks)
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    log.info("Tool call: %s(%s)", block.name, block.input)
                    tools_called.append(block.name)
                    result = execute_tool(block.name, block.input)
                    log.debug("Tool result (%s): %.200s", block.name, result)
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
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run_claude_loop, user_message, history)


@bot.event
async def on_ready():
    log.info("Logged in as %s (id=%s)", bot.user, bot.user.id)


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
    site = web.TCPSite(runner, "127.0.0.1", WEBHOOK_PORT)
    await site.start()
    log.info("Webhook server listening on 127.0.0.1:%d/notify", WEBHOOK_PORT)


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
# Weekly digest helpers
# ---------------------------------------------------------------------------

def _seconds_until_weekly(day: int, hour: int) -> float:
    """Seconds until the next occurrence of weekday `day` at `hour` in server local time."""
    now = datetime.datetime.now()
    days_ahead = (day - now.weekday()) % 7
    target = (now + datetime.timedelta(days=days_ahead)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    if target <= now:
        target += datetime.timedelta(weeks=1)
    return (target - now).total_seconds()


def _collect_performance_week() -> dict:
    """Parse PCP pmlogger for 7-day CPU and memory peaks."""
    result = {}
    try:
        ncpu = int(subprocess.check_output(["nproc"], text=True).strip())
    except Exception:
        ncpu = 1

    # CPU — hourly samples over 7 days
    try:
        r = subprocess.run(
            ["pmrep", "-S", "-168hour", "-t", "1hour", "-o", "csv",
             "kernel.all.cpu.user", "kernel.all.cpu.sys"],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0 and r.stdout.strip():
            reader = csv.reader(io.StringIO(r.stdout))
            next(reader)  # skip header
            peaks = []
            for row in reader:
                try:
                    pct = (float(row[1]) + float(row[2])) / (ncpu * 10)
                    peaks.append((round(pct, 1), row[0]))
                except (IndexError, ValueError):
                    pass
            if peaks:
                peak_pct, peak_time = max(peaks, key=lambda x: x[0])
                result["cpu"] = {
                    "peak_pct":     peak_pct,
                    "peak_time":    peak_time,
                    "avg_pct":      round(sum(p for p, _ in peaks) / len(peaks), 1),
                    "hours_over80": sum(1 for p, _ in peaks if p > 80),
                    "samples":      len(peaks),
                }
    except Exception as e:
        result["cpu_error"] = str(e)

    # Memory — hourly samples
    try:
        r = subprocess.run(
            ["pmrep", "-S", "-168hour", "-t", "1hour", "-o", "csv", "mem.util.used"],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0 and r.stdout.strip():
            reader = csv.reader(io.StringIO(r.stdout))
            next(reader)
            values = []
            for row in reader:
                try:
                    values.append(float(row[1]))
                except (IndexError, ValueError):
                    pass
            if values:
                result["memory"] = {
                    "peak_gb": round(max(values) / 1e9, 1),
                    "avg_gb":  round(sum(values) / len(values) / 1e9, 1),
                }
    except Exception as e:
        result["mem_error"] = str(e)

    return result


def _collect_jellyfin_week() -> dict:
    """Items added to Jellyfin in the last 7 days, grouped by type."""
    import requests as req
    from tools import JELLYFIN_URL, JELLYFIN_TOKEN
    if not JELLYFIN_TOKEN:
        return {}
    headers = {"X-Emby-Token": JELLYFIN_TOKEN}
    since = (datetime.datetime.utcnow() - datetime.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")

    try:
        users = [u for u in req.get(f"{JELLYFIN_URL}/Users", headers=headers, timeout=10).json()
                 if u.get("Name", "").lower() != "automation"]
        if not users:
            return {}
        uid = users[0]["Id"]
        result = {}
        for item_type, key in [("Movie", "movies"), ("Series", "shows"), ("MusicAlbum", "albums")]:
            params = {
                "SortBy": "DateCreated", "SortOrder": "Descending",
                "Recursive": "true", "IncludeItemTypes": item_type,
                "Fields": "DateCreated,ProductionYear",
                "MinDateLastSaved": since,
            }
            items = req.get(f"{JELLYFIN_URL}/Users/{uid}/Items",
                            headers=headers, params=params, timeout=10).json().get("Items", [])
            if key == "albums":
                result[key] = [i["Name"] for i in items]
            else:
                result[key] = [f"{i['Name']} ({i.get('ProductionYear','?')})" for i in items]
        return result
    except Exception:
        return {}


def _collect_jenkins_week() -> dict:
    """Pass/fail counts for each Jenkins job over the last 7 days."""
    import requests as req
    from tools import JENKINS_URL, JENKINS_USER, JENKINS_TOKEN
    auth = (JENKINS_USER, JENKINS_TOKEN) if JENKINS_TOKEN else None
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=7)).timestamp() * 1000
    result = {}
    for job in ["Login_Test", "Process_Movies", "Nightly_Convert"]:
        try:
            url = f"{JENKINS_URL}/job/{job}/api/json?tree=builds[number,result,timestamp]{{0,100}}"
            builds = [b for b in req.get(url, auth=auth, timeout=10).json().get("builds", [])
                      if b.get("timestamp", 0) >= cutoff]
            result[job] = {
                "runs":    len(builds),
                "success": sum(1 for b in builds if b.get("result") == "SUCCESS"),
                "failure": sum(1 for b in builds if b.get("result") == "FAILURE"),
            }
        except Exception:
            result[job] = {"error": True}
    return result


def _collect_health_notes() -> list[str]:
    """Quick system health checks — returns a list of plain-text notes."""
    notes = []
    # Pending apt updates
    try:
        r = subprocess.run(["apt", "list", "--upgradable"],
                           capture_output=True, text=True, timeout=15)
        count = sum(1 for line in r.stdout.splitlines() if "/" in line)
        if count:
            notes.append(f"{count} pending apt update{'s' if count != 1 else ''}")
    except Exception:
        pass
    # Failed systemd units
    try:
        r = subprocess.run(["systemctl", "list-units", "--state=failed", "--no-legend"],
                           capture_output=True, text=True, timeout=10)
        failed = [line.split()[0] for line in r.stdout.strip().splitlines() if line.strip()]
        if failed:
            notes.append(f"failed systemd units: {', '.join(failed)}")
    except Exception:
        pass
    return notes


def _render_weekly_digest(perf: dict, jellyfin: dict, jenkins: dict,
                           disk: str, health: list[str]) -> str:
    """Feed gathered data to Claude for a natural-language weekly digest."""
    # --- build compact data block ---
    lines = []

    # Performance
    if "cpu" in perf:
        c = perf["cpu"]
        partial = f" ({c['samples']}/168 samples — PCP still building history)" if c["samples"] < 100 else ""
        lines.append(f"CPU: avg {c['avg_pct']}%, peak {c['peak_pct']}% at {c['peak_time']}, "
                     f"{c['hours_over80']} hour(s) above 80%{partial}")
    elif "cpu_error" in perf:
        lines.append(f"CPU: data unavailable ({perf['cpu_error']})")
    if "memory" in perf:
        m = perf["memory"]
        lines.append(f"Memory: avg {m['avg_gb']} GB used, peak {m['peak_gb']} GB")
    perf_block = "\n".join(lines) or "No performance data available yet"

    # Jellyfin
    jf_lines = []
    if jellyfin.get("movies"):
        jf_lines.append(f"Movies ({len(jellyfin['movies'])}): {', '.join(jellyfin['movies'])}")
    if jellyfin.get("shows"):
        jf_lines.append(f"Shows ({len(jellyfin['shows'])}): {', '.join(jellyfin['shows'])}")
    if jellyfin.get("albums"):
        jf_lines.append(f"Music albums ({len(jellyfin['albums'])}): {', '.join(jellyfin['albums'])}")
    jf_block = "\n".join(jf_lines) or "Nothing new added this week"

    # Jenkins
    jk_lines = []
    for job, d in jenkins.items():
        if d.get("error"):
            jk_lines.append(f"{job}: error fetching data")
        else:
            rate = f"{d['success']}/{d['runs']}" if d["runs"] else "no runs"
            fail_note = f" ⚠️ {d['failure']} failure(s)" if d.get("failure") else ""
            jk_lines.append(f"{job}: {rate} passed{fail_note}")
    jk_block = "\n".join(jk_lines) or "No Jenkins data"

    health_block = "\n".join(f"- {n}" for n in health) if health else "Nothing to flag"

    prompt = (
        "Write a concise weekly server digest for a home Ubuntu media server called Panda.\n"
        "Use Discord markdown. Be friendly but informative. Flag anything that needs attention.\n\n"
        f"PERFORMANCE (7-day):\n{perf_block}\n\n"
        f"JELLYFIN CONTENT ADDED:\n{jf_block}\n\n"
        f"JENKINS JOB HEALTH:\n{jk_block}\n\n"
        f"DISK USAGE:\n{disk}\n\n"
        f"HEALTH NOTES:\n{health_block}\n\n"
        "Format with these emoji section headers:\n"
        "🖥️ **Performance** — highlight peaks or concerns; say 'all normal' if nothing to flag\n"
        "🎬 **Content Added** — movies, shows, music; say 'nothing new' if empty\n"
        "⚙️ **System** — Jenkins pass rates, disk, pending updates, failed units\n\n"
        "Keep it tight — this is a Discord message, not a report."
    )

    response = claude.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


async def task_weekly_digest():
    """Post a weekly digest on the configured day and local hour."""
    await bot.wait_until_ready()
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    log.info(
        "Weekly digest task started — every %s at %02d:00 local",
        day_names[WEEKLY_DIGEST_DAY], WEEKLY_DIGEST_HOUR
    )

    while not bot.is_closed():
        secs = _seconds_until_weekly(WEEKLY_DIGEST_DAY, WEEKLY_DIGEST_HOUR)
        log.info("Weekly digest sleeping %.1fh", secs / 3600)
        await asyncio.sleep(secs)

        try:
            log.info("Generating weekly digest…")
            loop = asyncio.get_running_loop()

            # Gather all data concurrently
            perf, jellyfin, jenkins = await asyncio.gather(
                loop.run_in_executor(None, _collect_performance_week),
                loop.run_in_executor(None, _collect_jellyfin_week),
                loop.run_in_executor(None, _collect_jenkins_week),
            )
            from tools import get_disk_usage
            disk   = await loop.run_in_executor(None, get_disk_usage)
            health = await loop.run_in_executor(None, _collect_health_notes)

            # Claude writes the narrative
            text = await loop.run_in_executor(
                None, _render_weekly_digest, perf, jellyfin, jenkins, disk, health
            )

            week_of = datetime.datetime.utcnow().strftime("%b %d")
            await post_notification(f"📊 **Weekly Digest — {week_of}**\n\n{text}")
            log.info("Weekly digest posted")

        except Exception:
            log.exception("task_weekly_digest error")
            await post_notification("⚠️ Weekly digest failed — check bot logs")


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
            message = resp.content[0].text

        elif task_type == "condition_check" and task["condition_pattern"]:
            met = bool(re.search(task["condition_pattern"], combined, re.IGNORECASE))
            new_attempt = attempt + 1

            if met:
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

async def task_announce_startup():
    """Post a one-time startup message with the current version."""
    await bot.wait_until_ready()
    await post_notification(f"🐼 **PandaBot v{BOT_VERSION}** online")
    log.info("Startup announced: v%d", BOT_VERSION)


async def main():
    await start_webhook_server()
    asyncio.create_task(task_disk_alert())
    asyncio.create_task(task_service_watchdog())
    asyncio.create_task(task_weekly_digest())
    asyncio.create_task(task_scheduler())
    asyncio.create_task(task_announce_startup())
    await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
