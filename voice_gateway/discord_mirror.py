"""
Discord mirroring for voice gateway turns.

Posts each voice conversation turn to a Discord channel so the conversation
is visible alongside regular bot messages.

Required env vars:
    DISCORD_BOT_TOKEN         — bot token
    DISCORD_VOICE_CHANNEL_ID  — preferred channel; falls back to DISCORD_CHANNEL_ID
"""

from __future__ import annotations

import logging
import os

import aiohttp

logger = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"

# Module-level reference to the shared aiohttp session; set by main.py at startup.
_http_session: aiohttp.ClientSession | None = None


def set_session(session: aiohttp.ClientSession) -> None:
    global _http_session
    _http_session = session


def get_session() -> aiohttp.ClientSession:
    if _http_session is None:
        raise RuntimeError("aiohttp ClientSession has not been initialised. Call set_session() first.")
    return _http_session


def _get_channel_id() -> str | None:
    return os.environ.get("DISCORD_VOICE_CHANNEL_ID") or os.environ.get("DISCORD_CHANNEL_ID")


def _get_bot_token() -> str | None:
    return os.environ.get("DISCORD_BOT_TOKEN")


async def post_turn(
    user_text: str,
    assistant_text: str,
    client_session: aiohttp.ClientSession | None = None,
) -> None:
    """
    Post a voice conversation turn to Discord.

    Catches and logs all exceptions — never raises.
    """
    try:
        channel_id = _get_channel_id()
        bot_token = _get_bot_token()

        if not channel_id:
            logger.warning("DISCORD_VOICE_CHANNEL_ID / DISCORD_CHANNEL_ID not set; skipping mirror")
            return
        if not bot_token:
            logger.warning("DISCORD_BOT_TOKEN not set; skipping mirror")
            return

        session = client_session or get_session()
        url = f"{DISCORD_API}/channels/{channel_id}/messages"
        content = f"\U0001f3a4 **[Voice]** {user_text}\n\U0001f916 **Pandabot:** {assistant_text}"

        headers = {
            "Authorization": f"Bot {bot_token}",
            "Content-Type": "application/json",
        }

        async with session.post(url, json={"content": content}, headers=headers) as resp:
            if resp.status not in (200, 201):
                body = await resp.text()
                logger.error("Discord mirror returned HTTP %d: %s", resp.status, body[:200])
            else:
                logger.debug("Voice turn mirrored to Discord channel %s", channel_id)

    except Exception:
        logger.exception("Unexpected error mirroring voice turn to Discord")
