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
import datetime
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
# Pending-confirmation state
# ---------------------------------------------------------------------------
# Maps channel_id → {"name": tool_name, "inputs": {..., "confirmed": True}}
# Set when Claude shows a manage_files/set_jenkins_schedule preview.
# Consumed (and cleared) when the user replies with an affirmative.
_pending_confirmations: dict[int, dict] = {}

_AFFIRMATIVES = {"yes", "y", "yep", "yeah", "yup", "confirm", "ok", "okay", "sure", "do it"}

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
AI_IKEY                    = os.environ.get("APPINSIGHTS_IKEY", "")

# Bot identity + server description
BOT_NAME             = os.environ.get("BOT_NAME",   "Panda")
BOT_EMOJI            = os.environ.get("BOT_EMOJI",  "🐼")
TZ_NAME              = os.environ.get("TZ_NAME",    "America/New_York (Eastern Time, EDT/EST)")
SERVER_DESCRIPTION   = os.environ.get("SERVER_DESCRIPTION",  "")
HARDWARE_DESCRIPTION = os.environ.get("HARDWARE_DESCRIPTION",
                           "NVIDIA GTX 970 (4 GB VRAM), 2 TB NTFS HDD at /mnt/media")
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


def _run_claude_loop(user_message: str, history: list[dict] | None = None, channel_id: int | None = None) -> str:
    """Synchronous Claude agentic loop (run in a thread executor)."""
    import time as _time
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
                log.info("Sonnet stop_reason=%s", response.stop_reason)

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
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _run_claude_loop, user_message, history, message.channel.id
    )


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
                # generative_prompt takes priority over met_message when condition is satisfied
                if task["generative_prompt"]:
                    prompt = task["generative_prompt"].replace("{results}", combined)
                    resp = await loop.run_in_executor(None, lambda: claude.messages.create(
                        model="claude-haiku-4-5",
                        max_tokens=400,
                        messages=[{"role": "user", "content": prompt}],
                    ))
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
    await post_notification(f"{BOT_EMOJI} **{BOT_NAME} v{BOT_VERSION}** online")
    log.info("Startup announced: v%d", BOT_VERSION)


async def main():
    await start_webhook_server()
    asyncio.create_task(task_disk_alert())
    asyncio.create_task(task_service_watchdog())
    asyncio.create_task(task_scheduler())
    asyncio.create_task(task_announce_startup())
    await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
