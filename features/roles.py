"""Reaction roles: react on the roles message to get a role, unreact to lose it."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from . import roles_config as cfg
from .common import log


class ReactionRoles(commands.Cog):
    def __init__(self, bot: commands.Bot, guild_id: int) -> None:
        self.bot = bot
        self.guild_id = guild_id
        self.message_id = cfg.DEFAULT_MESSAGE_ID

    async def cog_load(self) -> None:
        saved = await self.bot.db.get_setting("roles_message_id")
        if saved:
            self.message_id = int(saved)
        log.info("Reaction roles on message %s", self.message_id)

    @app_commands.command(name="setuproles", description="Post the reaction roles message in this channel")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def setuproles(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(title=cfg.EMBED_TITLE, description=cfg.EMBED_DESCRIPTION, color=cfg.EMBED_COLOR)
        msg = await interaction.channel.send(embed=embed)
        for emoji in cfg.REACTION_ROLES:
            await msg.add_reaction(emoji)
        self.message_id = msg.id
        await self.bot.db.set_setting("roles_message_id", str(msg.id))
        await interaction.response.send_message(f"✅ Done! Roles message posted (ID `{msg.id}`).", ephemeral=True)

    async def _update(self, payload: discord.RawReactionActionEvent, add: bool) -> None:
        if payload.guild_id != self.guild_id or payload.message_id != self.message_id:
            return
        if payload.user_id == self.bot.user.id:
            return
        role_id = cfg.REACTION_ROLES.get(str(payload.emoji))
        if not role_id:
            return
        guild = self.bot.get_guild(payload.guild_id)
        role = guild and guild.get_role(role_id)
        if not role:
            log.warning("Reaction role %s not found in the server", role_id)
            return
        try:
            member = payload.member or guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
            if member.bot:
                return
            if add:
                await member.add_roles(role, reason="Reaction roles")
            else:
                await member.remove_roles(role, reason="Reaction roles")
        except discord.HTTPException as e:
            log.warning("Couldn't %s role %s (does the bot have Manage Roles, and is its role above %s?): %r",
                        "add" if add else "remove", role.name, role.name, e)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self._update(payload, add=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self._update(payload, add=False)
