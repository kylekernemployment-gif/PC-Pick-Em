"""Delete obvious scam messages, and links from brand-new members."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

from .common import log, mod_log

SCAM_PATTERNS = re.compile(
    r"free\s*nitro|nitro\s*(?:for\s*)?free|discord\.gift/|gift\s*nitro"
    r"|d[il1]sc[o0]r[cd]l?\.(?!com\b|gg\b|gift\b)\w+|dlscord|disc0rd|discorcl"
    r"|steam\s*(?:gift|free)|free\s*steam|stea?mcomm?un[il1]ty|steamcornmunity|stearncommunity"
    r"|@everyone.{0,200}https?://|https?://\S+.{0,200}@everyone",
    re.IGNORECASE | re.DOTALL,
)
LINK = re.compile(r"https?://|discord\.gg/|discord(?:app)?\.com/invite/", re.IGNORECASE)


def is_enabled() -> bool:
    return os.getenv("ANTI_SCAM", "true").strip().lower() not in ("0", "false", "no", "off")


class AntiScam(commands.Cog):
    def __init__(self, bot: commands.Bot, guild_id: int, mod_role: str) -> None:
        self.bot = bot
        self.guild_id = guild_id
        self.mod_role = mod_role
        self.new_member_hours = float(os.getenv("NEW_MEMBER_LINK_HOURS", "24"))

    def trusted(self, member: discord.Member) -> bool:
        p = member.guild_permissions
        return p.manage_messages or p.administrator or any(r.name == self.mod_role for r in member.roles)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        member = message.author
        if (message.guild is None or message.guild.id != self.guild_id or member.bot
                or not isinstance(member, discord.Member) or self.trusted(member)):
            return
        text = message.content
        if SCAM_PATTERNS.search(text):
            await self._delete(message)
            try:
                await member.timeout(timedelta(hours=1), reason="Scam message")
            except discord.HTTPException as e:
                log.warning("Couldn't time out %s (needs Moderate Members): %r", member, e)
            await mod_log(self.bot, f"🚫 Deleted a likely scam from {member} (`{member.id}`) in "
                                    f"{message.channel.mention} and timed them out for 1 hour:\n>>> {text[:500]}")
            return
        joined = member.joined_at or datetime.now(timezone.utc)
        if self.new_member_hours and LINK.search(text) and \
                datetime.now(timezone.utc) - joined < timedelta(hours=self.new_member_hours):
            await self._delete(message)
            try:
                await message.channel.send(f"{member.mention} new members can't post links yet — "
                                           f"try again after {self.new_member_hours:g} hours.", delete_after=10)
            except discord.HTTPException:
                pass
            await mod_log(self.bot, f"🔗 Deleted a link from new member {member} (`{member.id}`) in "
                                    f"{message.channel.mention}:\n>>> {text[:500]}")

    async def _delete(self, message: discord.Message) -> None:
        try:
            await message.delete()
        except discord.HTTPException as e:
            log.warning("Couldn't delete message (needs Manage Messages): %r", e)
