from __future__ import annotations

import logging
import os

import discord

log = logging.getLogger("pcpickem")


def env_id(name: str) -> int | None:
    value = os.getenv(name, "").strip().strip("'\"").strip()
    return int(value) if value.isdigit() else None


async def mod_log(bot: discord.Client, text: str) -> None:
    """Post to MOD_LOG_CHANNEL_ID if set; always write to the Render log."""
    log.info("[modlog] %s", text)
    channel_id = env_id("MOD_LOG_CHANNEL_ID")
    if not channel_id:
        return
    try:
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as e:
        log.warning("Couldn't post to mod log channel: %r", e)
