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
import logging
import os
import textwrap

import aiohttp
from aiohttp import web
import anthropic
import discord
from discord.ext import commands

from tools import TOOL_DEFINITIONS, execute_tool

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

DISCORD_TOKEN      = os.environ["DISCORD_TOKEN"]
DISCORD_CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
WEBHOOK_PORT       = int(os.environ.get("WEBHOOK_PORT", "8765"))
WEBHOOK_SECRET     = os.environ.get("WEBHOOK_SECRET", "")

SYSTEM_PROMPT = textwrap.dedent("""\
    You are Panda, a helpful assistant for a home Ubuntu Server 24.04 machine.
    The server runs:
      - Jellyfin (Docker, port 8096) — media server with NVIDIA NVENC transcoding
      - Jenkins (Docker, port 8080) — CI server running these jobs:
          • Login_Test (hourly) — Playwright test of the Jellyfin login page
          • Process_Movies (midnight) — sorts and names ripped video files
          • Nightly_Convert (3 am) — re-encodes video to h264_nvenc
      - Sunshine (bare metal, systemd) — game streaming (Moonlight / Shield TV)
      - Cockpit (port 9090), Portainer (port 9000) — admin UIs
      - Tailscale VPN (IP 100.65.72.102)
      - MakeMKV + abcde for disc ripping (udev auto-rip pipeline)

    Hardware: NVIDIA GTX 970 (4 GB VRAM), 2 TB NTFS HDD at /mnt/media.

    You have read-only tools to check disk usage, log tails, service status,
    Jenkins build status, and system stats. You cannot execute arbitrary code
    or make any changes to the server.

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
    messages = (history or []) + [{"role": "user", "content": user_message}]

    for _ in range(10):  # safety: max 10 tool-call rounds
        response = claude.messages.create(
            model="claude-opus-4-6",
            max_tokens=4096,
            thinking={"type": "adaptive"},
            system=SYSTEM_PROMPT,
            tools=TOOL_DEFINITIONS,
            messages=messages,
        )
        log.info("Claude stop_reason=%s", response.stop_reason)

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text
            return "(no text response)"

        if response.stop_reason == "tool_use":
            # Append assistant turn (may include thinking blocks + tool_use blocks)
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    log.info("Tool call: %s(%s)", block.name, block.input)
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

    async with message.channel.typing():
        try:
            reply = await handle_claude_query(content, message)
        except Exception as e:
            log.exception("Claude query failed")
            reply = f"Error talking to Claude: {e}"

    for chunk in split_message(reply):
        await message.channel.send(chunk)

    await bot.process_commands(message)


# ---------------------------------------------------------------------------
# Notification webhook (local only — Jenkins / scripts POST here)
# ---------------------------------------------------------------------------

async def post_notification(text: str):
    """Send a notification to the configured Discord channel."""
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if channel is None:
        log.error("Notification channel %s not found", DISCORD_CHANNEL_ID)
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
# Entry point
# ---------------------------------------------------------------------------

async def main():
    await start_webhook_server()
    await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
